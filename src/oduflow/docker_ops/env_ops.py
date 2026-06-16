from __future__ import annotations

import contextlib
import datetime
import json
import logging
import os
import pathlib
import shutil
import subprocess
import time
from collections.abc import Iterator
from typing import Any

import docker
from docker import DockerClient

from oduflow.docker_ops.client import chown_recursive, get_client, get_odoo_uid_gid
from oduflow.docker_ops.system_ops import (
    _copy_file_to_container,
    _create_pg_role,
    _db_exists,
    _drop_pg_role,
    _exec_sql,
    _resolve_instance_conf,
)
from oduflow.env_credentials import create_credentials, load_credentials
from oduflow.errors import (
    ConflictError,
    ExternalCommandError,
    NotFoundError,
    PrerequisiteNotMetError,
    ProtectedError,
)
from oduflow.git_ops import RepoAuthError, git_env_for_team
from oduflow.naming import (
    get_db_name,
    get_env_hostname,
    get_filestore_paths,
    get_repo_path,
    get_resource_name,
    get_template_db_name,
    get_workspace_path,
    sanitize_repo_url,
    slugify_branch,
)
from oduflow.port_registry import allocate_port, release_port
from oduflow import settings
from oduflow import activity
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")


def _trace(msg: str, *args: object) -> None:
    if settings.TRACE:
        logger.info("[TRACE] " + msg, *args)


_PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _normalize_extra_addons(raw_addons) -> dict[str, str]:
    """Convert old list format or new dict format to {name: branch} dict."""
    if isinstance(raw_addons, dict):
        return raw_addons
    if isinstance(raw_addons, list):
        logger.warning(
            "Legacy list format for extra_addons (no branch info), skipping: %s",
            raw_addons,
        )
        return {}
    return {}


def _get_used_ports(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    exclude_env: str = "",
) -> set[int]:
    """Collect host ports currently bound by managed containers (excluding a specific env)."""
    used: set[int] = set()
    filters = {
        "label": [
            f"{settings.managed_label}=true",
            f"{settings.team_label}={team.team_id}",
        ]
    }
    for c in client.containers.list(all=True, filters=filters):
        if not c.name.startswith(settings.prefix):
            continue
        env = c.labels.get(settings.branch_label, "")
        if env == exclude_env:
            continue
        ports = c.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
        for mappings in ports.values():
            if mappings:
                for m in mappings:
                    try:
                        used.add(int(m["HostPort"]))
                    except (KeyError, ValueError, TypeError):
                        pass
    return used


def _ensure_system_ready(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    template_name: str | None = None,
) -> None:
    try:
        db_container = client.containers.get(settings.shared_db_container)
        if db_container.status != "running":
            raise PrerequisiteNotMetError(
                f"{settings.shared_db_container} is not running. System not initialized. Restart oduflow."
            )
    except docker.errors.NotFound:
        raise PrerequisiteNotMetError(
            f"{settings.shared_db_container} not found. System not initialized. Restart oduflow."
        )

    if template_name is not None:
        tpl_db = get_template_db_name(template_name, team.team_id)
        if not _db_exists(client, settings, tpl_db):
            raise PrerequisiteNotMetError(
                f"Template database '{tpl_db}' not found. Run init_template first."
            )

    if settings.routing_mode == "traefik":
        try:
            t = client.containers.get(settings.traefik_container)
            if t.status != "running":
                raise PrerequisiteNotMetError(
                    f"{settings.traefik_container} is not running. System not initialized. Restart oduflow."
                )
        except docker.errors.NotFound:
            raise PrerequisiteNotMetError(
                f"{settings.traefik_container} not found. System not initialized. Restart oduflow."
            )


def _dir_size_mb(path: str) -> float:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            try:
                total += os.path.getsize(fp)
            except OSError:
                pass
    return total / (1024 * 1024)


def _mount_filestore(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    env_db: str,
    odoo_image: str,
    odoo_volumes: dict,
    *,
    template_name: str,
) -> None:
    template_filestore = team.get_template_filestore_path(template_name)
    if not template_filestore or not os.path.isdir(template_filestore):
        logger.debug(
            "Dump filestore not found at %s, skipping overlay mount", template_filestore
        )
        return

    paths = get_filestore_paths(env_name, team.workspaces_dir)

    # Read use_overlay flag from template metadata (avoids slow filestore scan)
    use_overlay = None
    metadata_path = team.get_template_metadata_path(template_name)
    if os.path.isfile(metadata_path):
        try:
            with open(metadata_path) as f:
                metadata = json.load(f)
            use_overlay = metadata.get("use_overlay")
        except (json.JSONDecodeError, OSError):
            pass

    if use_overlay is None:
        # Fallback for old templates without the flag
        size_mb = _dir_size_mb(template_filestore)
        use_overlay = size_mb >= settings.overlay_threshold_mb

    if not use_overlay:
        logger.info("Template use_overlay=False, using copy")
        merged = paths["merged"]
        if os.path.exists(merged):
            shutil.rmtree(merged)
        shutil.copytree(template_filestore, merged)
        odoo_uid_gid = get_odoo_uid_gid(client, odoo_image)
        uid_str, gid_str = odoo_uid_gid.split(":")
        chown_recursive(merged, int(uid_str), int(gid_str), client, odoo_image)
        odoo_volumes[merged] = {
            "bind": f"/var/lib/odoo/.local/share/Odoo/filestore/{env_db}",
            "mode": "rw",
        }
        return

    logger.info("Template use_overlay=True, using overlay")
    for d in (paths["upper"], paths["work"], paths["merged"]):
        os.makedirs(d, mode=0o777, exist_ok=True)
        os.chmod(d, 0o777)

    odoo_uid_gid = get_odoo_uid_gid(client, odoo_image)
    uid_str, gid_str = odoo_uid_gid.split(":")
    for d in (paths["upper"], paths["work"], paths["merged"]):
        chown_recursive(d, int(uid_str), int(gid_str), client, odoo_image)

    if not shutil.which("fuse-overlayfs"):
        raise PrerequisiteNotMetError(
            "fuse-overlayfs is not installed. "
            "Install it with: sudo apt install fuse-overlayfs"
        )

    result = subprocess.run(
        [
            "fuse-overlayfs",
            "-o",
            f"lowerdir={template_filestore},upperdir={paths['upper']},workdir={paths['work']},allow_other",
            paths["merged"],
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        error_msg = (
            result.stderr.decode("utf-8", errors="replace").strip()
            if result.stderr
            else ""
        )
        hint = ""
        if "allow_other" in error_msg or "permission" in error_msg.lower():
            hint = " Hint: uncomment 'user_allow_other' in /etc/fuse.conf"
        raise PrerequisiteNotMetError(
            f"Failed to mount filestore overlay: {error_msg}.{hint}"
        )

    deadline = time.time() + 2.0
    while time.time() < deadline:
        if os.path.ismount(paths["merged"]):
            try:
                os.listdir(paths["merged"])
                break
            except OSError:
                pass
        time.sleep(0.05)

    if not os.path.ismount(paths["merged"]):
        raise PrerequisiteNotMetError(
            f"Filestore overlay mount at {paths['merged']} did not become ready"
        )

    odoo_volumes[paths["merged"]] = {
        "bind": f"/var/lib/odoo/.local/share/Odoo/filestore/{env_db}",
        "mode": "rw",
    }
    logger.info("Filestore overlay mounted", extra={"env_name": env_name})


def _unmount_filestore(env_name: str, team: TeamSettings) -> None:
    paths = get_filestore_paths(env_name, team.workspaces_dir)
    merged = paths["merged"]
    if not os.path.isdir(merged) or not os.path.ismount(merged):
        return

    for cmd in (
        ["fusermount", "-u", merged],
        ["umount", "-l", merged],
    ):
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            logger.info(
                "Filestore overlay unmounted (%s)",
                cmd[-2],
                extra={"env_name": env_name},
            )
            return
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue

    logger.warning("Could not unmount filestore overlay at %s", merged)


def _wait_unmounted(merged: str, timeout: float = 3.0) -> None:
    """Poll until ``merged`` is no longer a mount point (best-effort)."""
    deadline = time.time() + timeout
    while time.time() < deadline and os.path.ismount(merged):
        time.sleep(0.1)


class RemountResult:
    """Outcome of :func:`remount_template_overlays`."""

    def __init__(self, affected: list[str]) -> None:
        self.affected = affected
        self.failures: list[tuple[str, str]] = []


@contextlib.contextmanager
def remount_template_overlays(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    template_name: str,
    *,
    reset_upper: bool = False,
    exclude_envs: tuple[str, ...] = (),
) -> Iterator[RemountResult]:
    """Safely mutate a template's lower filestore layer under live overlay envs.

    Non-destructive template update (see issue #2): fuse-overlayfs keeps each
    environment's changes in a separate ``upper`` layer, so we can unmount an
    overlay while preserving its ``upper``/``work`` dirs, swap the read-only
    lower layer (the template filestore), and remount against the new lower with
    the same ``upper`` — keeping the environment's data.

    Usage::

        with remount_template_overlays(client, settings, team, name) as r:
            # ... mutate team.get_template_filestore_path(name) ...
        # r.affected / r.failures now describe what happened

    On enter: for every overlay-mounted environment that uses ``template_name``
    (excluding ``exclude_envs``), stop its Odoo container and unmount the
    overlay, keeping ``upper``/``work``. The caller mutates the template
    filestore inside the ``with`` block. On exit (always — even if the block
    raised, so envs are never left without a lower layer): remount each
    environment against the new lower reusing its preserved ``upper`` (unless
    ``reset_upper=True``) and restart the container if it had been running.

    Copy-mode environments (``use_overlay=False``) are not mounted, so they are
    skipped — updating the lower layer does not affect their independent copy.
    """
    exclude = set(exclude_envs)
    affected: list[dict[str, Any]] = []

    for env in list_environments(settings, team):
        env_name = env["env_name"]
        if env_name in exclude:
            continue
        if env.get("template_name") != template_name:
            continue
        merged = get_filestore_paths(env_name, team.workspaces_dir)["merged"]
        if not os.path.ismount(merged):
            continue

        container_name = get_resource_name(env_name, "odoo", settings.prefix)
        image = env.get("odoo_image") or "odoo:19.0"
        was_running = False
        try:
            container = client.containers.get(container_name)
            was_running = container.status == "running"
            if container.image.tags:
                image = container.image.tags[0]
        except docker.errors.NotFound:
            pass

        affected.append(
            {
                "env_name": env_name,
                "container_name": container_name,
                "image": image,
                "env_db": get_db_name(env_name, team.team_id),
                "was_running": was_running,
            }
        )

    result = RemountResult([a["env_name"] for a in affected])

    # Stop containers and unmount overlays (keeping upper/work).
    for a in affected:
        try:
            try:
                client.containers.get(a["container_name"]).stop(timeout=10)
            except docker.errors.NotFound:
                pass
            _unmount_filestore(a["env_name"], team)
            _wait_unmounted(
                get_filestore_paths(a["env_name"], team.workspaces_dir)["merged"]
            )
            logger.info("Unmounted overlay for env %s", a["env_name"])
        except Exception as exc:  # noqa: BLE001 - best-effort, reported via result
            logger.warning("Could not unmount overlay for %s: %s", a["env_name"], exc)
            result.failures.append((a["env_name"], f"unmount: {exc}"))

    try:
        yield result
    finally:
        for a in affected:
            env_name = a["env_name"]
            paths = get_filestore_paths(env_name, team.workspaces_dir)
            try:
                if os.path.ismount(paths["merged"]):
                    # Unmount failed earlier — don't stack a second mount.
                    result.failures.append((env_name, "still mounted, skipped remount"))
                else:
                    if reset_upper:
                        for key in ("upper", "work"):
                            d = paths[key]
                            if os.path.isdir(d):
                                shutil.rmtree(d)
                                os.makedirs(d, mode=0o777, exist_ok=True)
                    _mount_filestore(
                        client,
                        settings,
                        team,
                        env_name,
                        a["env_db"],
                        a["image"],
                        {},
                        template_name=template_name,
                    )
                    logger.info("Remounted overlay for env %s", env_name)
                if a["was_running"]:
                    try:
                        client.containers.get(a["container_name"]).start()
                    except docker.errors.NotFound:
                        pass
                    except docker.errors.APIError:
                        pass  # already running
            except Exception as exc:  # noqa: BLE001 - best-effort, reported via result
                logger.warning("Could not remount overlay for %s: %s", env_name, exc)
                result.failures.append((env_name, f"remount: {exc}"))


def _install_apt_packages(container, repo_path: str) -> str:
    """Install apt packages. Returns a human-readable log of what happened."""
    apt_file = os.path.join(repo_path, ".oduflow", "apt_packages.txt")
    if not os.path.isfile(apt_file):
        logger.debug("No .oduflow/apt_packages.txt in repo, skipping apt install")
        return ""

    with open(apt_file) as f:
        packages = [
            line.strip() for line in f if line.strip() and not line.startswith("#")
        ]
    if not packages:
        return ""

    logger.info("Updating apt and installing packages: %s", " ".join(packages))
    exit_code, output = container.exec_run("apt-get update", user="root")
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)
    if exit_code != 0:
        logger.warning("apt-get update failed (exit %d): %s", exit_code, output_str)
        return f"[APT] apt-get update FAILED (exit {exit_code}): {output_str}"

    cmd = "apt-get install -y " + " ".join(packages)
    exit_code, output = container.exec_run(cmd, user="root")
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)
    if exit_code != 0:
        logger.warning("apt install failed (exit %d): %s", exit_code, output_str)
        return f"[APT] install FAILED (exit {exit_code}): {output_str}"
    else:
        logger.info("apt packages installed")
        return f"[APT] Installed: {' '.join(packages)}"


def _ensure_user_site_packages(container) -> None:
    """Create the user site-packages directory for the odoo user and fix ownership.

    This allows ``pip install --user`` to work inside containers where
    ``/var/lib/odoo/.local`` may not exist or may be owned by root.
    """
    container.exec_run(
        "mkdir -p /var/lib/odoo/.local/lib",
        user="root",
    )
    container.exec_run(
        "chown -R odoo:odoo /var/lib/odoo/.local",
        user="root",
    )
    logger.debug("Ensured /var/lib/odoo/.local is owned by odoo")


def _install_pip_requirements(
    container, repo_path: str, *, restart: bool = True
) -> tuple[bool, str]:
    """Install pip requirements from repo.

    Returns (installed, log) — *installed* is True when packages were
    installed, *log* is a human-readable summary of what happened.

    When *restart* is False the caller is responsible for restarting the
    container after all setup steps are done.
    """
    # Prefer .oduflow/requirements.txt; fall back to the repo root for
    # compatibility with conventions used elsewhere (e.g. odoo.sh).
    oduflow_req = os.path.join(repo_path, ".oduflow", "requirements.txt")
    root_req = os.path.join(repo_path, "requirements.txt")
    if os.path.isfile(oduflow_req):
        container_req = "/mnt/extra-addons/.oduflow/requirements.txt"
    elif os.path.isfile(root_req):
        container_req = "/mnt/extra-addons/requirements.txt"
    else:
        logger.debug("No requirements.txt in repo, skipping pip install")
        return False, ""

    # Ensure the odoo user can write to its local site-packages
    _ensure_user_site_packages(container)

    cmd = f"pip3 install --user --break-system-packages -r {container_req}"
    logger.info("Installing pip requirements from %s", container_req)
    exit_code, output = container.exec_run(cmd, user="odoo")
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)
    if exit_code != 0 and "no such option" in output_str.lower():
        logger.info("--break-system-packages not supported, retrying without it")
        cmd = f"pip3 install --user -r {container_req}"
        exit_code, output = container.exec_run(cmd, user="odoo")
        output_str = (
            output.decode("utf-8") if isinstance(output, bytes) else str(output)
        )
    if exit_code != 0:
        logger.warning("pip install failed (exit %d): %s", exit_code, output_str)
        return False, f"[PIP] install FAILED (exit {exit_code}):\n{output_str}"
    else:
        logger.info("pip requirements installed")
        if restart:
            container.restart()
            logger.info("Container restarted after pip install")
        return True, f"[PIP] Requirements installed successfully:\n{output_str}"


def _cleanup_old_environment(
    client: "DockerClient",
    settings: Settings,
    team: TeamSettings,
    env_name: str,
) -> None:
    odoo_container_name = get_resource_name(env_name, "odoo", settings.prefix)
    try:
        old = client.containers.get(odoo_container_name)
        old.stop()
        old.remove(v=True)
        logger.info("Removed old container %s", odoo_container_name)
    except docker.errors.NotFound:
        pass
    except docker.errors.APIError:
        try:
            old.remove(v=True, force=True)
        except Exception:
            pass

    env_db = get_db_name(env_name, team.team_id)

    try:
        creds = load_credentials(
            env_name, team.workspaces_dir, settings.db_user, settings.db_password
        )
        _drop_pg_role(client, settings, creds["pg_user"])
    except Exception:
        pass

    if _db_exists(client, settings, env_db):
        try:
            _exec_sql(
                client, settings, f'DROP DATABASE IF EXISTS "{env_db}" WITH (FORCE);'
            )
            logger.info("Dropped old database %s", env_db)
        except Exception:
            pass

    workspace_path = get_workspace_path(env_name, team.workspaces_dir)
    if os.path.exists(workspace_path):
        _unmount_filestore(env_name, team)
        shutil.rmtree(workspace_path)


def _init_empty_database(
    client: DockerClient,
    settings: Settings,
    odoo_image: str,
    env_db: str,
    odoo_env: dict,
    odoo_volumes: dict,
    env_name: str,
) -> str:
    """Initialize a fresh empty database with ``-i base`` in an isolated,
    short-lived container, then remove it. Returns a setup-log line.

    This runs *before* the long-running serving container exists, so the init
    is the only Odoo process touching the database. It avoids the registry
    signaling race that occurs when the serving PID1 (``odoo -d <db> --dev=xml``)
    and an in-container ``-i base`` exec set up the registry concurrently and
    collide on ``CREATE TABLE orm_signaling_registry`` (``UniqueViolation`` in
    the pg catalog). ``-i base`` is explicit, so it works regardless of whether
    the image build auto-initializes an empty DB on plain ``-d``.
    """
    init_name = get_resource_name(env_name, "odoo-init", settings.prefix)
    try:
        client.containers.get(init_name).remove(force=True)
    except docker.errors.NotFound:
        pass

    init_container = client.containers.run(
        image=odoo_image,
        name=init_name,
        detach=True,
        network=settings.shared_network,
        environment=odoo_env,
        volumes=odoo_volumes,
        command=f"odoo -d {env_db} -i base --stop-after-init --no-http",
    )
    exit_code: int = -1
    logs = ""
    try:
        result = init_container.wait(timeout=600)
        exit_code = (
            result.get("StatusCode", -1) if isinstance(result, dict) else int(result)
        )
        try:
            logs = init_container.logs().decode("utf-8", errors="replace")
        except Exception:
            logs = ""
    finally:
        try:
            init_container.remove(force=True)
        except docker.errors.APIError:
            pass

    if exit_code != 0:
        logger.error(
            "Base init failed (exit %s)", exit_code, extra={"env_name": env_name}
        )
        return f"[INIT] odoo -i base FAILED (exit {exit_code}):\n{logs[-4000:]}"
    logger.info("Base init completed", extra={"env_name": env_name})
    return "[INIT] odoo -i base completed successfully"


def create_environment(
    settings: Settings,
    team: TeamSettings,
    branch: str,
    repo_url: str,
    odoo_image: str,
    env_name: str = "",
    template_name: str | None = None,
    extra_addons: dict[str, str] | None = None,
    git_user: str = "",
    sanitize: bool = True,
    auto_install_modules: list[str] | None = None,
    env_vars: dict[str, str] | None = None,
    local_path: str = "",
) -> dict[str, str]:
    env_name = env_name or branch
    start_time = time.time()
    try:
        client = get_client()
    except Exception as e:
        raise PrerequisiteNotMetError(
            f"Failed to connect to Docker daemon: {e}. Ensure Docker is running."
        )

    if template_name is not None:
        tpl_db = get_template_db_name(template_name, team.team_id)
        if not _db_exists(client, settings, tpl_db):
            logger.warning(
                "Template DB '%s' not found, falling back to init from scratch",
                tpl_db,
            )
            template_name = None

    _ensure_system_ready(client, settings, team, template_name)

    odoo_container_name = get_resource_name(env_name, "odoo", settings.prefix)
    try:
        existing = client.containers.get(odoo_container_name)
        if existing.status == "running":
            existing.reload()
            if settings.routing_mode == "traefik":
                url = f"https://{get_env_hostname(env_name, team.hostname)}"
            else:
                ports = existing.ports.get("8069/tcp")
                host_port = ports[0]["HostPort"] if ports else "?"
                url = f"http://{team.hostname}:{host_port}"
            raise ConflictError(
                f"Environment '{env_name}' already exists and is running at {url}."
            )
        raise ConflictError(
            f"Environment '{env_name}' already exists (status: {existing.status})."
        )
    except docker.errors.NotFound:
        pass

    _cleanup_old_environment(client, settings, team, env_name)
    workspace_path = get_workspace_path(env_name, team.workspaces_dir)
    # Live-mount mode: bind-mount the agent's own checkout
    # directly instead of cloning. The repo lives OUTSIDE the managed
    # workspace, so it is never touched by cleanup/delete.
    local_mount = bool(local_path)
    repo_path = (
        os.path.abspath(local_path)
        if local_mount
        else get_repo_path(env_name, team.workspaces_dir)
    )
    if local_mount and not settings.allow_local_path:
        raise PrerequisiteNotMetError(
            "local_path (live-mount) is disabled. Set allow_local_path = true "
            "in oduflow.toml [server] to enable it."
        )
    env_db = get_db_name(env_name, team.team_id)

    labels = {
        settings.managed_label: "true",
        settings.team_label: team.team_id,
        settings.branch_label: env_name,
        settings.repo_label: repo_url,
        settings.image_label: odoo_image,
        "oduflow.template": template_name if template_name is not None else "none",
        "oduflow.git_branch": branch,
        "oduflow.created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    if extra_addons:
        labels["oduflow.extra_addons"] = json.dumps(extra_addons)
    if git_user:
        labels["oduflow.git_user"] = git_user
    if local_mount:
        labels["oduflow.local_path"] = repo_path
    if auto_install_modules:
        labels["oduflow.auto_install_modules"] = ",".join(auto_install_modules)
    if env_vars:
        labels["oduflow.env_vars"] = json.dumps(env_vars)

    if settings.routing_mode == "traefik":
        slug = slugify_branch(env_name)
        traefik_router = f"oduflow-{slug}"
        traefik_host = get_env_hostname(env_name, team.hostname)
        labels.update(
            {
                "traefik.enable": "true",
                f"traefik.http.routers.{traefik_router}.rule": f"Host(`{traefik_host}`)",
                f"traefik.http.routers.{traefik_router}.entrypoints": "websecure",
                f"traefik.http.routers.{traefik_router}.tls": "true",
                f"traefik.http.routers.{traefik_router}.tls.certresolver": "letsencrypt",
                f"traefik.http.services.{traefik_router}.loadbalancer.server.port": "8069",
            }
        )

    logger.info(
        "Creating environment",
        extra={
            "env_name": env_name,
            "branch": branch,
            "repo": sanitize_repo_url(repo_url),
            "image": odoo_image,
            "prefix": settings.prefix,
            "routing_mode": settings.routing_mode,
            "hostname": team.hostname,
            "workspaces_dir": team.workspaces_dir,
        },
    )

    os.makedirs(workspace_path, exist_ok=True)

    if local_mount:
        # No clone: the agent's checkout is bind-mounted live. The directory
        # must already exist on the host (validated in the tool layer too).
        if not os.path.isdir(repo_path):
            raise PrerequisiteNotMetError(
                f"local_path does not exist or is not a directory: {repo_path}"
            )
        logger.info("Live-mount mode: using local checkout at %s", repo_path)
        # Baseline for local change detection: first pull_and_apply diffs
        # against this snapshot. Git is deliberately ignored in live-mount mode.
        _write_local_snapshot(repo_path, env_name, team)
    else:
        git_env = git_env_for_team(team.git_credentials_file())

        from oduflow.git_ops import inject_credential_user

        clone_url = inject_credential_user(repo_url, git_user)

        auth_keywords = (
            "Authentication failed",
            "could not read Username",
            "Permission denied",
            "Repository not found",
            "terminal prompts disabled",
            "Invalid username or password",
        )

        try:
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--branch",
                    branch,
                    "--depth",
                    "1",
                    clone_url,
                    repo_path,
                ],
                check=True,
                capture_output=True,
                timeout=60,
                env=git_env,
            )
        except subprocess.CalledProcessError as e:
            error_msg = e.stderr.decode("utf-8") if e.stderr else str(e)
            if any(kw.lower() in error_msg.lower() for kw in auth_keywords):
                raise RepoAuthError(
                    f"Git authentication failed for {sanitize_repo_url(repo_url)}. "
                    f"Call 'setup_repo_auth' first to cache credentials."
                )
            raise ExternalCommandError("git clone", e.returncode, error_msg)
        except subprocess.TimeoutExpired:
            raise ExternalCommandError(
                "git clone",
                -1,
                "Repository clone timed out (60s). Repository may be too large or network is slow.",
            )

    # --- Extra addons worktrees ---
    extra_mount_paths = []
    if extra_addons:
        from oduflow.extra_addons import create_worktree

        extra_dir = os.path.join(workspace_path, "extra")
        os.makedirs(extra_dir, exist_ok=True)
        for repo_name, addon_branch in extra_addons.items():
            wt_path = os.path.join(extra_dir, repo_name)
            create_worktree(team, repo_name, addon_branch, wt_path)
            container_path = f"/mnt/extra-addons-{repo_name}"
            extra_mount_paths.append((wt_path, container_path))

    if template_name is not None:
        tpl_db = get_template_db_name(template_name, team.team_id)
        _exec_sql(
            client,
            settings,
            f'CREATE DATABASE "{env_db}" TEMPLATE "{tpl_db}";',
        )
    else:
        _exec_sql(
            client,
            settings,
            f'CREATE DATABASE "{env_db}";',
        )

    env_creds = create_credentials(env_name, team.team_id, team.workspaces_dir)
    _create_pg_role(
        client, settings, env_creds["pg_user"], env_creds["pg_password"], env_db
    )

    if template_name is not None:
        # Transfer ownership of every object in the public schema to the
        # new per-environment role.  DDL operations (ALTER TABLE) require
        # ownership — GRANT ALL only covers DML.  We cannot use
        # REASSIGN OWNED BY "<superuser>" because PostgreSQL refuses to
        # reassign system objects owned by that role.  Instead we iterate
        # over tables, sequences, and views individually.
        new_user = env_creds["pg_user"]
        _exec_sql(
            client,
            settings,
            f'ALTER SCHEMA public OWNER TO "{new_user}";',
            db=env_db,
        )
        _exec_sql(
            client,
            settings,
            "DO $$ DECLARE r RECORD; BEGIN "
            "FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' LOOP "
            f"EXECUTE 'ALTER TABLE public.' || quote_ident(r.tablename) || ' OWNER TO \"{new_user}\"'; "
            "END LOOP; "
            "FOR r IN SELECT sequence_name FROM information_schema.sequences WHERE sequence_schema = 'public' LOOP "
            f"EXECUTE 'ALTER SEQUENCE public.' || quote_ident(r.sequence_name) || ' OWNER TO \"{new_user}\"'; "
            "END LOOP; "
            "FOR r IN SELECT viewname FROM pg_views WHERE schemaname = 'public' LOOP "
            f"EXECUTE 'ALTER VIEW public.' || quote_ident(r.viewname) || ' OWNER TO \"{new_user}\"'; "
            "END LOOP; "
            "END $$;",
            db=env_db,
        )

        # Drop Odoo signaling sequences carried over from the template DB.
        # Odoo re-creates them on first startup (CREATE SEQUENCE without
        # IF NOT EXISTS), so leftover sequences cause DuplicateTable errors.
        _exec_sql(
            client,
            settings,
            "DO $$ DECLARE r RECORD; BEGIN "
            "FOR r IN SELECT c.relname FROM pg_class c "
            "WHERE c.relkind = 'S' "
            "AND (c.relname LIKE 'base_registry_signaling%' "
            "OR c.relname LIKE 'base_cache_signaling%') "
            "LOOP EXECUTE 'DROP SEQUENCE IF EXISTS ' || quote_ident(r.relname); "
            "END LOOP; END $$;",
            db=env_db,
        )
        logger.info(
            "Post-clone fixup done for '%s': ownership transferred, signaling sequences dropped",
            env_db,
        )

    odoo_env = {
        "HOST": settings.shared_db_container,
        "USER": env_creds["pg_user"],
        "PASSWORD": env_creds["pg_password"],
        **(env_vars or {}),
    }
    odoo_volumes = {repo_path: {"bind": "/mnt/extra-addons", "mode": "rw"}}

    for host_path, container_path in extra_mount_paths:
        odoo_volumes[host_path] = {"bind": container_path, "mode": "ro"}

    repo_odoo_conf = os.path.join(repo_path, ".oduflow", "odoo.conf")
    if os.path.isfile(repo_odoo_conf):
        base_conf_path = repo_odoo_conf
        logger.info("Using odoo.conf from repository")
    elif _resolve_instance_conf("odoo.conf", team.data_dir).exists():
        base_conf_path = str(_resolve_instance_conf("odoo.conf", team.data_dir))
    else:
        base_conf_path = None

    odoo_conf_to_copy: str | None = None
    if base_conf_path:
        from oduflow.extra_addons import generate_odoo_conf

        generated_conf = os.path.join(workspace_path, "odoo.conf")
        extra_container_paths = (
            [cp for _, cp in extra_mount_paths] if extra_mount_paths else []
        )
        generate_odoo_conf(base_conf_path, generated_conf, extra_container_paths)
        odoo_conf_to_copy = generated_conf

    if template_name is not None:
        _mount_filestore(
            client,
            settings,
            team,
            env_name,
            env_db,
            odoo_image,
            odoo_volumes,
            template_name=template_name,
        )
    else:
        # No template — create a plain filestore directory on the host so data
        # survives container restarts (no overlay needed).
        filestore_path = os.path.join(workspace_path, "filestore")
        os.makedirs(filestore_path, mode=0o777, exist_ok=True)
        os.chmod(filestore_path, 0o777)
        _uid, _gid = get_odoo_uid_gid(client, odoo_image).split(":")
        chown_recursive(filestore_path, int(_uid), int(_gid), client, odoo_image)
        odoo_volumes[filestore_path] = {
            "bind": f"/var/lib/odoo/.local/share/Odoo/filestore/{env_db}",
            "mode": "rw",
        }

    host_port: int | None = None
    if settings.routing_mode == "port":
        used_ports = _get_used_ports(client, settings, team, exclude_env=env_name)
        host_port = allocate_port(
            team.port_registry_path,
            env_name,
            team.port_range_start,
            team.port_range_end,
            used_ports=used_ports,
        )

    sessions_path = os.path.join(workspace_path, "sessions")
    os.makedirs(sessions_path, mode=0o777, exist_ok=True)
    os.chmod(sessions_path, 0o777)
    uid_str, gid_str = get_odoo_uid_gid(client, odoo_image).split(":")
    chown_recursive(sessions_path, int(uid_str), int(gid_str), client, odoo_image)
    odoo_volumes[sessions_path] = {
        "bind": "/var/lib/odoo/.local/share/Odoo/sessions",
        "mode": "rw",
    }

    run_kwargs: dict = dict(
        image=odoo_image,
        name=odoo_container_name,
        detach=True,
        network=settings.shared_network,
        environment=odoo_env,
        labels=labels,
        volumes=odoo_volumes,
        restart_policy={"Name": "unless-stopped"},
        command=f"odoo -d {env_db} --dev=xml",
    )
    if settings.routing_mode == "port":
        run_kwargs["ports"] = {"8069/tcp": host_port}

    try:
        logger.info("Pulling image %s", odoo_image)
        client.images.pull(odoo_image)
    except Exception as exc:
        logger.warning("Could not pull image %s, using local copy: %s", odoo_image, exc)

    setup_logs: list[str] = []

    # Greenfield (no template): initialize the empty DB with `-i base` in an
    # isolated, short-lived container BEFORE the serving container exists, so
    # the init is the only Odoo process touching the DB. This avoids the
    # registry-signaling race between the serving PID1 and an in-container
    # `-i base` exec (UniqueViolation on orm_signaling_registry).
    if template_name is None:
        logger.info(
            "No template — initialising Odoo with -i base (isolated container)",
            extra={"env_name": env_name},
        )
        setup_logs.append(
            _init_empty_database(
                client, settings, odoo_image, env_db, odoo_env, odoo_volumes, env_name
            )
        )

    container = client.containers.run(**run_kwargs)

    if odoo_conf_to_copy:
        _copy_file_to_container(container, odoo_conf_to_copy, "/etc/odoo")

    # Install repo apt/pip dependencies onto the serving container (both paths).
    # base needs no custom deps, so this runs after the DB is ready.
    apt_log = _install_apt_packages(container, repo_path)
    if apt_log:
        setup_logs.append(apt_log)
    _, pip_log = _install_pip_requirements(container, repo_path)
    if pip_log:
        setup_logs.append(pip_log)

    # --- Sanitize environment database ---
    if sanitize and template_name is not None:
        from oduflow.sanitizer import sanitize_environment

        sanitize_logs = sanitize_environment(client, settings, team, env_name)
        setup_logs.extend(sanitize_logs)

    # --- Auto-install modules ---
    if auto_install_modules:
        modules_str = ",".join(auto_install_modules)
        logger.info(
            "Auto-installing modules: %s",
            modules_str,
            extra={"env_name": env_name},
        )
        install_cmd = (
            f"/entrypoint.sh odoo -d {env_db} -i {modules_str}"
            f" --stop-after-init --no-http"
        )
        exit_code, output = container.exec_run(install_cmd)
        output_str = (
            output.decode("utf-8") if isinstance(output, bytes) else str(output)
        )
        if exit_code != 0:
            logger.error(
                "Auto-install modules failed (exit %d): %s",
                exit_code,
                output_str,
                extra={"env_name": env_name},
            )
            setup_logs.append(
                f"[AUTO-INSTALL] odoo -i {modules_str} FAILED (exit {exit_code}):\n{output_str}"
            )
        else:
            setup_logs.append(
                f"[AUTO-INSTALL] odoo -i {modules_str} completed successfully"
            )
        container.restart()

    if settings.routing_mode == "traefik":
        url = f"https://{get_env_hostname(env_name, team.hostname)}"
    else:
        url = f"http://{team.hostname}:{host_port}"
    logger.info(
        "Environment created",
        extra={"env_name": env_name, "url": url, "container": odoo_container_name},
    )

    result = {
        "url": url,
        "odoo_container": odoo_container_name,
        "database": env_db,
        "workspace": workspace_path,
        "setup_logs": setup_logs,
    }
    result["extra_addons"] = extra_addons or {}
    result["local_path"] = repo_path if local_mount else ""
    result["elapsed_seconds"] = round(time.time() - start_time, 1)
    activity.touch(team, env_name)
    return result


def is_protected(settings: Settings, team: TeamSettings, env_name: str) -> bool:
    workspace_path = get_workspace_path(env_name, team.workspaces_dir)
    return os.path.exists(os.path.join(workspace_path, ".protected"))


def _get_note_text(env_name: str, workspaces_dir: str) -> str:
    note_path = os.path.join(get_workspace_path(env_name, workspaces_dir), ".note")
    if os.path.exists(note_path):
        try:
            with open(note_path) as f:
                return f.read().strip()
        except OSError:
            return ""
    return ""


def protect_environment(
    settings: Settings, team: TeamSettings, env_name: str
) -> dict[str, Any]:
    """Mark environment as protected by creating .protected marker file."""
    client = get_client()
    container_name = get_resource_name(env_name, "odoo", settings.prefix)
    try:
        client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Environment '{env_name}' does not exist.")
    workspace_path = get_workspace_path(env_name, team.workspaces_dir)
    marker = os.path.join(workspace_path, ".protected")
    open(marker, "w").close()
    logger.info("Environment protected", extra={"env_name": env_name})
    return {"env_name": env_name, "protected": True}


def unprotect_environment(
    settings: Settings, team: TeamSettings, env_name: str
) -> dict[str, Any]:
    """Remove protection from environment by deleting .protected marker file."""
    client = get_client()
    container_name = get_resource_name(env_name, "odoo", settings.prefix)
    try:
        client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Environment '{env_name}' does not exist.")
    workspace_path = get_workspace_path(env_name, team.workspaces_dir)
    marker = os.path.join(workspace_path, ".protected")
    if os.path.exists(marker):
        os.remove(marker)
    logger.info("Environment unprotected", extra={"env_name": env_name})
    return {"env_name": env_name, "protected": False}


def get_note(settings: Settings, team: TeamSettings, env_name: str) -> str:
    return _get_note_text(env_name, team.workspaces_dir)


def set_note(
    settings: Settings, team: TeamSettings, env_name: str, note: str
) -> dict[str, Any]:
    """Set or clear a note for an environment."""
    client = get_client()
    container_name = get_resource_name(env_name, "odoo", settings.prefix)
    try:
        client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Environment '{env_name}' does not exist.")
    workspace_path = get_workspace_path(env_name, team.workspaces_dir)
    note_path = os.path.join(workspace_path, ".note")
    note = note.strip()
    if note:
        with open(note_path, "w") as f:
            f.write(note)
    elif os.path.exists(note_path):
        os.remove(note_path)
    logger.info("Environment note updated", extra={"env_name": env_name, "note": note})
    return {"env_name": env_name, "note": note}


def delete_environment(
    settings: Settings, team: TeamSettings, env_name: str
) -> list[str]:
    if is_protected(settings, team, env_name):
        raise ProtectedError(
            f"Environment '{env_name}' is protected. Unprotect it before deleting."
        )
    client = get_client()
    odoo_container_name = get_resource_name(env_name, "odoo", settings.prefix)
    env_db = get_db_name(env_name, team.team_id)
    workspace_path = get_workspace_path(env_name, team.workspaces_dir)

    container_exists = True
    try:
        client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        container_exists = False

    if not container_exists and not os.path.exists(workspace_path):
        raise NotFoundError(f"Environment '{env_name}' does not exist.")

    warnings: list[str] = []

    logger.info("Deleting environment", extra={"env_name": env_name})

    if settings.routing_mode == "port":
        release_port(team.port_registry_path, env_name)

    if container_exists:
        try:
            container = client.containers.get(odoo_container_name)
            container.stop()
            container.remove(v=True)
        except docker.errors.NotFound:
            pass

    try:
        _exec_sql(
            client,
            settings,
            f'DROP DATABASE IF EXISTS "{env_db}" WITH (FORCE);',
        )
    except Exception as exc:
        msg = f'Failed to drop database "{env_db}": {exc}'
        logger.warning(msg, extra={"env_name": env_name})
        warnings.append(msg)

    creds = load_credentials(
        env_name, team.workspaces_dir, settings.db_user, settings.db_password
    )
    try:
        _drop_pg_role(client, settings, creds["pg_user"])
    except Exception as exc:
        msg = f'Failed to drop PG role "{creds["pg_user"]}": {exc}'
        logger.warning(msg, extra={"env_name": env_name})
        warnings.append(msg)

    if os.path.exists(workspace_path):
        _unmount_filestore(env_name, team)
        extra_dir = os.path.join(workspace_path, "extra")
        if os.path.isdir(extra_dir):
            from oduflow.extra_addons import remove_worktree

            for repo_name in os.listdir(extra_dir):
                wt_path = os.path.join(extra_dir, repo_name)
                if os.path.isdir(wt_path):
                    remove_worktree(team, repo_name, wt_path)
        shutil.rmtree(workspace_path)

    activity.remove(team, env_name)
    logger.info("Environment deleted", extra={"env_name": env_name})
    return warnings


def list_environments(settings: Settings, team: TeamSettings) -> list[dict[str, Any]]:
    client = get_client()
    filters = {
        "label": [
            f"{settings.managed_label}=true",
            f"{settings.team_label}={team.team_id}",
        ]
    }
    containers = client.containers.list(all=True, filters=filters)

    envs: dict[str, dict[str, Any]] = {}
    for container in containers:
        if not container.name.startswith(settings.prefix):
            continue
        env_name = container.labels.get(settings.branch_label)
        if not env_name:
            continue

        if env_name not in envs:
            git_branch = container.labels.get("oduflow.git_branch", env_name)
            envs[env_name] = {
                "env_name": env_name,
                "branch": env_name,  # backward compat alias
                "git_branch": git_branch,
                "containers": [],
                "status": "running",
                "url": None,
                "odoo_image": container.labels.get(settings.image_label, ""),
                "repo_url": sanitize_repo_url(
                    container.labels.get(settings.repo_label, "")
                ),
                "local_path": container.labels.get("oduflow.local_path", ""),
                "template_name": container.labels.get("oduflow.template", ""),
                "extra_addons": _normalize_extra_addons(
                    json.loads(container.labels.get("oduflow.extra_addons", "{}")),
                ),
                "db_name": get_db_name(env_name, team.team_id),
                "protected": is_protected(settings, team, env_name),
                "auto_install_modules": container.labels.get(
                    "oduflow.auto_install_modules", ""
                ),
                "created_at": container.labels.get("oduflow.created_at", "")
                or container.attrs.get("Created", ""),
                "note": _get_note_text(env_name, team.workspaces_dir),
            }

        try:
            image_name = container.image.tags[0] if container.image.tags else "unknown"
        except Exception:
            image_name = container.attrs.get("Config", {}).get("Image", "unknown")

        container_info = {
            "name": container.name,
            "status": container.status,
            "image": image_name,
        }

        if "-odoo" in container.name:
            if settings.routing_mode == "traefik":
                envs[env_name]["url"] = (
                    f"https://{get_env_hostname(env_name, team.hostname)}/web?debug=1"
                )
            else:
                ports = container.attrs.get("NetworkSettings", {}).get("Ports", {})
                if ports:
                    mappings = ports.get("8069/tcp")
                    if mappings:
                        host_port = mappings[0].get("HostPort")
                        if host_port:
                            envs[env_name]["url"] = (
                                f"http://{team.hostname}:{host_port}/web?debug=1"
                            )

        envs[env_name]["containers"].append(container_info)

    records = activity.get_all(team)
    for env_name, env in envs.items():
        total = len(env["containers"])
        running = sum(1 for c in env["containers"] if c["status"] == "running")
        if total and running == 0:
            env["status"] = "stopped"
        elif running < total:
            env["status"] = "partial"
        else:
            env["status"] = "running"
        rec = records.get(env_name, {})
        env["last_activity"] = rec.get("last_activity", "")
        env["stopped_at"] = rec.get("stopped_at", "")
        env["auto_stopped"] = rec.get("stopped_by") == "auto"

    return list(envs.values())


def wait_for_odoo_ready(
    settings: Settings, team: TeamSettings, env_name: str, timeout: int = 120
) -> bool:
    """Poll Odoo /web/health until it responds 200 or timeout."""
    import time
    import urllib.request
    import urllib.error

    info = get_environment_info(settings, team, env_name)
    base_url = info.get("url", "")
    if not base_url:
        return False

    url = f"{base_url}/web/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def ensure_running(settings: Settings, env_name: str) -> bool:
    """Start the environment's Odoo container if it is stopped.

    Returns True when a start was needed (the caller may want to tell the
    agent the environment was woken up), False when it was already running.
    """
    client = get_client()
    odoo_container_name = get_resource_name(env_name, "odoo", settings.prefix)
    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )
    if container.status == "running":
        return False
    start_environment(settings, env_name)
    return True


def restart_environment(settings: Settings, env_name: str) -> dict[str, str]:
    client = get_client()
    odoo_container_name = get_resource_name(env_name, "odoo", settings.prefix)

    try:
        odoo_container = client.containers.get(odoo_container_name)
        odoo_container.restart()
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )

    logger.info("Environment restarted", extra={"env_name": env_name})
    return {"odoo_container": odoo_container_name}


def stop_environment(
    settings: Settings, team: TeamSettings, env_name: str
) -> dict[str, str]:
    if is_protected(settings, team, env_name):
        raise ProtectedError(
            f"Environment '{env_name}' is protected. Unprotect it before stopping."
        )
    client = get_client()
    odoo_container_name = get_resource_name(env_name, "odoo", settings.prefix)

    try:
        odoo_container = client.containers.get(odoo_container_name)
        odoo_container.stop()
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )

    activity.mark_stopped(team, env_name, by="manual")
    logger.info("Environment stopped", extra={"env_name": env_name})
    return {"odoo_container": odoo_container_name, "stopped": [odoo_container_name]}


def start_environment(settings: Settings, env_name: str) -> dict[str, str]:
    client = get_client()
    odoo_container_name = get_resource_name(env_name, "odoo", settings.prefix)

    try:
        db_container = client.containers.get(settings.shared_db_container)
        if db_container.status != "running":
            db_container.start()
    except docker.errors.NotFound:
        raise PrerequisiteNotMetError(
            f"{settings.shared_db_container} not found. System not initialized. Restart oduflow."
        )

    started = [settings.shared_db_container]

    try:
        odoo_container = client.containers.get(odoo_container_name)
        odoo_container.start()
        started.append(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )

    logger.info("Environment started", extra={"env_name": env_name})
    return {"odoo_container": odoo_container_name, "started": started}


def get_environment_info(
    settings: Settings, team: TeamSettings, env_name: str
) -> dict[str, Any]:
    from oduflow.docker_ops.stats import _get_one_container_stats

    client = get_client()
    odoo_container_name = get_resource_name(env_name, "odoo", settings.prefix)

    result: dict[str, Any] = {
        "env_name": env_name,
        "branch": env_name,  # backward compat alias
        "db_name": get_db_name(env_name, team.team_id),
        "workspace": get_workspace_path(env_name, team.workspaces_dir),
        "odoo": {"name": odoo_container_name, "running": False, "status": "not found"},
        "db": {
            "name": settings.shared_db_container,
            "running": False,
            "status": "not found",
        },
    }

    try:
        odoo_container = client.containers.get(odoo_container_name)
        result["odoo"]["status"] = odoo_container.status
        result["odoo"]["running"] = odoo_container.status == "running"

        labels = odoo_container.labels
        result["template_name"] = labels.get("oduflow.template", "none")
        result["repo_url"] = sanitize_repo_url(labels.get(settings.repo_label, ""))
        result["odoo_image"] = labels.get(settings.image_label, "")
        result["git_branch"] = labels.get("oduflow.git_branch", env_name)
        result["git_user"] = labels.get("oduflow.git_user", "")
        result["extra_addons"] = _normalize_extra_addons(
            json.loads(labels.get("oduflow.extra_addons", "{}")),
        )
        result["auto_install_modules"] = labels.get("oduflow.auto_install_modules", "")
        result["env_vars"] = json.loads(labels.get("oduflow.env_vars", "{}"))
        result["created_at"] = labels.get(
            "oduflow.created_at", ""
        ) or odoo_container.attrs.get("Created", "")
        result["note"] = _get_note_text(env_name, team.workspaces_dir)

        if settings.routing_mode == "traefik":
            result["url"] = (
                f"https://{get_env_hostname(env_name, team.hostname)}/web?debug=1"
            )
        else:
            ports = odoo_container.attrs.get("NetworkSettings", {}).get("Ports", {})
            if ports:
                mappings = ports.get("8069/tcp")
                if mappings:
                    host_port = mappings[0].get("HostPort")
                    if host_port:
                        result["url"] = (
                            f"http://{team.hostname}:{host_port}/web?debug=1"
                        )

        stats = _get_one_container_stats(odoo_container)
        if stats:
            result["odoo"]["cpu_percent"] = stats["cpu_percent"]
            result["odoo"]["mem_usage_mb"] = stats["mem_usage_mb"]
            result["odoo"]["mem_percent"] = stats["mem_percent"]
    except docker.errors.NotFound:
        pass

    try:
        db_container = client.containers.get(settings.shared_db_container)
        result["db"]["status"] = db_container.status
        result["db"]["running"] = db_container.status == "running"
        stats = _get_one_container_stats(db_container)
        if stats:
            result["db"]["cpu_percent"] = stats["cpu_percent"]
            result["db"]["mem_usage_mb"] = stats["mem_usage_mb"]
            result["db"]["mem_percent"] = stats["mem_percent"]
    except docker.errors.NotFound:
        pass

    creds = load_credentials(
        env_name, team.workspaces_dir, settings.db_user, settings.db_password
    )
    result["db_user"] = creds["pg_user"]

    result["all_running"] = result["odoo"]["running"] and result["db"]["running"]
    return result


_LOCAL_SNAPSHOT_SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    ".idea",
    ".vscode",
    "node_modules",
}
_LOCAL_SNAPSHOT_SKIP_SUFFIXES = (".pyc", ".pyo")


def _scan_local_snapshot(root: str) -> dict[str, dict[str, int]]:
    """Map relative paths to cheap file fingerprints for live-mount mode."""
    out: dict[str, dict[str, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _LOCAL_SNAPSHOT_SKIP_DIRS]
        for f in filenames:
            if f.endswith(_LOCAL_SNAPSHOT_SKIP_SUFFIXES):
                continue
            fp = os.path.join(dirpath, f)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            out[os.path.relpath(fp, root)] = {
                "size": int(st.st_size),
                "mtime_ns": int(st.st_mtime_ns),
            }
    return out


def _local_snapshot_path(env_name: str, team: TeamSettings) -> str:
    return os.path.join(
        get_workspace_path(env_name, team.workspaces_dir), ".oduflow_local_state.json"
    )


def _load_local_snapshot(env_name: str, team: TeamSettings) -> dict[str, dict[str, int]]:
    snap_path = _local_snapshot_path(env_name, team)
    if not os.path.isfile(snap_path):
        return {}
    try:
        with open(snap_path) as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

    if not isinstance(raw, dict):
        return {}

    # Current format stores {"version": 1, "files": {...}}. Older dev builds
    # stored the file map directly; accept both so existing environments recover.
    files = raw.get("files") if "files" in raw else raw
    if not isinstance(files, dict):
        return {}
    return files


def _write_local_snapshot(repo_path: str, env_name: str, team: TeamSettings) -> None:
    snap_path = _local_snapshot_path(env_name, team)
    try:
        os.makedirs(os.path.dirname(snap_path), exist_ok=True)
        with open(snap_path, "w") as f:
            json.dump(
                {
                    "version": 1,
                    "repo_path": repo_path,
                    "files": _scan_local_snapshot(repo_path),
                },
                f,
                sort_keys=True,
            )
    except OSError:
        logger.warning(
            "Could not write live-mount snapshot for %s", env_name, exc_info=True
        )


def _detect_local_changes(
    repo_path: str, env_name: str, team: TeamSettings
) -> tuple[str | None, list[str]]:
    """Return ``(base_ref, changed_files)`` for a live-mounted checkout.

    Live-mount mode is independent of Git: commits are the user's choice, and
    Oduflow tracks only what has been successfully applied to Odoo. The diff is
    computed from a per-env file snapshot and the snapshot is advanced only
    after a successful apply.
    """
    current = _scan_local_snapshot(repo_path)
    old = _load_local_snapshot(env_name, team)
    changed = sorted(
        {p for p, fingerprint in current.items() if old.get(p) != fingerprint}
        | {p for p in old if p not in current}
    )
    return None, changed


def _apply_actions(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    odoo_container_name: str,
    *,
    to_install: list[str],
    to_upgrade: list[str],
    do_restart: bool,
    changed_files: list[str],
) -> dict[str, Any]:
    """Run install/upgrade/restart and build the result dict (shared by the
    explicit and auto paths of :func:`pull_environment`)."""
    from oduflow.docker_ops.odoo_ops import (
        install_odoo_modules,
        upgrade_odoo_modules,
    )

    if to_install or to_upgrade:
        messages: list[str] = []
        odoo_output_parts: list[str] = []
        last_exit_code = 0
        if to_install:
            res = install_odoo_modules(settings, team, env_name, *to_install)
            last_exit_code = res["exit_code"]
            messages.append(f"Installed modules: {','.join(to_install)}")
            if res.get("output"):
                odoo_output_parts.append(res["output"])
        if to_upgrade:
            res = upgrade_odoo_modules(settings, team, env_name, *to_upgrade)
            last_exit_code = res["exit_code"]
            messages.append(f"Upgraded modules: {','.join(to_upgrade)}")
            if res.get("output"):
                odoo_output_parts.append(res["output"])
        client.containers.get(odoo_container_name).restart()
        messages.append("Container restarted.")
        logger.info(
            "Container restarted after module update", extra={"env_name": env_name}
        )
        return {
            "action": "install" if to_install else "upgrade",
            "modules_installed": to_install,
            "modules_upgraded": to_upgrade,
            "exit_code": last_exit_code,
            "changed_files": changed_files,
            "message": " ".join(messages),
            "output": "\n".join(odoo_output_parts),
        }

    if do_restart:
        client.containers.get(odoo_container_name).restart()
        logger.info("Container restarted", extra={"env_name": env_name})
        return {
            "action": "restart",
            "changed_files": changed_files,
            "message": "Container restarted.",
        }

    if changed_files:
        return {
            "action": "refresh",
            "changed_files": changed_files,
            "message": (
                "Only XML/JS changes detected. Refresh your browser "
                "(--dev=xml is active)."
            ),
        }
    return {"action": "none", "message": "No changes detected."}


def pull_environment(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    *,
    install: list[str] | None = None,
    upgrade: list[str] | None = None,
    restart: bool = False,
    strict: bool = False,
) -> dict[str, Any]:
    """Sync code into an environment and apply the right Odoo action.

    Code-delivery mode is chosen from the env's labels:

    * **git** (default) — ``git pull`` the branch (and extra-addon worktrees)
      into the managed clone, which is bind-mounted into the container.
    * **live-mount** (``oduflow.local_path`` label, gated by allow_local_path) — the
      agent's checkout is already bind-mounted, so there is nothing to pull;
      changes are detected from Oduflow's last successful local snapshot.

    Action selection:

    * **explicit** — when any of ``install`` / ``upgrade`` / ``restart`` is
      given, do exactly that. A guardrail compares the request against what the
      changed files suggest and returns non-blocking ``warnings`` (or, with
      ``strict=True``, refuses with ``action="blocked"``).
    * **auto** — otherwise fall back to ``classify_changes`` in git mode or
      path-only ``shallow_classify`` in live-mount mode.
    """
    from oduflow.git_analysis import guardrail_warnings, recommend
    from oduflow.git_ops import pull_repo

    client = get_client()
    odoo_container_name = get_resource_name(env_name, "odoo", settings.prefix)

    try:
        container_obj = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )

    local_path = container_obj.labels.get("oduflow.local_path", "")
    is_local = bool(local_path)
    repo_path = local_path or get_repo_path(env_name, team.workspaces_dir)

    if not os.path.isdir(repo_path):
        raise NotFoundError(
            f"Repository for environment '{env_name}' not found at {repo_path}"
        )

    # --- 1. Sync code + detect changed files and a diff base ---
    if is_local:
        _trace("pull_environment(%s): live-mount, detecting local changes", env_name)
        base_ref, all_changed = _detect_local_changes(repo_path, env_name, team)
    else:
        _trace("pull_environment(%s): git pull started", env_name)
        git_branch = container_obj.labels.get("oduflow.git_branch", env_name)
        old_head, changed_files = pull_repo(
            repo_path, git_branch, cred_file=team.git_credentials_file()
        )
        base_ref = old_head

        extra_changed_files: list[str] = []
        try:
            extra_addons = json.loads(
                container_obj.labels.get("oduflow.extra_addons", "{}")
            )
        except (json.JSONDecodeError, TypeError):
            extra_addons = {}
        if extra_addons:
            from oduflow.extra_addons import pull_extra_worktree

            extra_dir = os.path.join(
                get_workspace_path(env_name, team.workspaces_dir), "extra"
            )
            for repo_name, branch in extra_addons.items():
                wt_path = os.path.join(extra_dir, repo_name)
                if not os.path.isdir(wt_path):
                    continue
                _extra_old, extra_files = pull_extra_worktree(
                    team, repo_name, branch, wt_path
                )
                extra_changed_files.extend(extra_files)
        all_changed = changed_files + extra_changed_files

    _trace(
        "pull_environment(%s): %d changed files: %s",
        env_name,
        len(all_changed),
        all_changed,
    )

    # --- 2. Determine actions: explicit (agent-driven) vs auto (classify) ---
    explicit = bool(install) or bool(upgrade) or restart
    recommended = (
        recommend(all_changed, repo_path, base_ref)
        if all_changed
        else {"action": "none", "modules_to_install": [], "modules_to_upgrade": []}
    )

    warnings: list[str] = []
    if explicit:
        to_install = list(install or [])
        to_upgrade = list(upgrade or [])
        do_restart = bool(restart)
        if all_changed:
            warnings = guardrail_warnings(
                recommended, to_install, to_upgrade, do_restart
            )
        if strict and warnings:
            return {
                "action": "blocked",
                "warnings": warnings,
                "changed_files": all_changed,
                "message": (
                    "Guardrail (strict) blocked apply: the requested action looks "
                    "incomplete for the detected changes. Re-call with the suggested "
                    "install/upgrade, or pass strict=False to apply anyway."
                ),
            }
    else:
        if not all_changed:
            return {
                "action": "none",
                "message": (
                    "No local changes detected." if is_local else "Already up to date."
                ),
            }
        to_install = list(recommended.get("modules_to_install", []))
        to_upgrade = list(recommended.get("modules_to_upgrade", []))
        do_restart = recommended.get("action") == "restart"

    # --- 3. Execute ---
    result = _apply_actions(
        client,
        settings,
        team,
        env_name,
        odoo_container_name,
        to_install=to_install,
        to_upgrade=to_upgrade,
        do_restart=do_restart,
        changed_files=all_changed,
    )
    if warnings:
        result["warnings"] = warnings
    if is_local and int(result.get("exit_code", 0)) == 0:
        _write_local_snapshot(repo_path, env_name, team)
    return result


def update_environment(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    *,
    env_override: dict[str, str] | None = None,
    image_override: str | None = None,
) -> dict[str, Any]:
    """Re-create an environment's container, preserving DB, repo and filestore.

    With no overrides this simply rebuilds the container from its current image
    and configuration (useful when the container is broken). Optionally pulls a
    different ``image_override`` and/or fully replaces the user-supplied
    environment variables with ``env_override`` (an explicit dict; an empty dict
    clears them). The PostgreSQL connection variables (HOST/USER/PASSWORD) are
    always re-derived from the environment credentials, and image-baked env
    comes from the image itself.
    """
    client = get_client()
    odoo_container_name = get_resource_name(env_name, "odoo", settings.prefix)

    # ------------------------------------------------------------------
    # 1. Look up existing container and extract its configuration
    # ------------------------------------------------------------------
    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )

    # Image (current → target). Capture the current digest so we can report
    # whether the running image actually changed after the pull.
    try:
        current_image = container.image.tags[0]
    except (IndexError, Exception):
        current_image = container.attrs["Config"]["Image"]
    odoo_image = image_override or current_image
    try:
        old_digest = container.image.id
    except Exception:
        old_digest = container.attrs.get("Image", "")

    # Labels
    labels = dict(container.labels)

    # User-supplied environment variables: an explicit override wins (full
    # replace), otherwise restore from the persisted label. The DB connection
    # variables are added back from the credentials below.
    if env_override is not None:
        user_env = dict(env_override)
    else:
        user_env = json.loads(labels.get("oduflow.env_vars", "{}"))

    # Volumes / bind mounts – parse "host:container:mode" strings
    raw_binds = container.attrs.get("HostConfig", {}).get("Binds") or []
    volumes: dict[str, dict[str, str]] = {}
    for bind in raw_binds:
        parts = bind.split(":")
        if len(parts) >= 3:
            volumes[parts[0]] = {"bind": parts[1], "mode": parts[2]}
        elif len(parts) == 2:
            volumes[parts[0]] = {"bind": parts[1], "mode": "rw"}

    # Command
    raw_cmd = container.attrs["Config"].get("Cmd") or []
    command = " ".join(raw_cmd) if raw_cmd else None

    # Port bindings (only relevant in port mode)
    host_port: int | None = None
    if settings.routing_mode == "port":
        port_bindings = container.attrs.get("HostConfig", {}).get("PortBindings") or {}
        tcp_bindings = port_bindings.get("8069/tcp")
        if tcp_bindings:
            try:
                host_port = int(tcp_bindings[0]["HostPort"])
            except (KeyError, IndexError, ValueError, TypeError):
                pass

    logger.info(
        "Updating environment – stopping old container",
        extra={"env_name": env_name, "container": odoo_container_name},
    )

    # ------------------------------------------------------------------
    # 2. Stop and remove ONLY the container
    # ------------------------------------------------------------------
    try:
        container.stop()
    except Exception:
        pass
    try:
        container.remove(v=True)
    except docker.errors.APIError:
        try:
            container.remove(v=True, force=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 3. Re-mount filestore overlay if needed (only for overlay-mode envs)
    # ------------------------------------------------------------------
    fs_paths = get_filestore_paths(env_name, team.workspaces_dir)
    merged = fs_paths["merged"]
    has_overlay_dirs = os.path.isdir(fs_paths["upper"])
    if has_overlay_dirs and os.path.isdir(merged) and not os.path.ismount(merged):
        env_db = get_db_name(env_name)
        template_name = labels.get("oduflow.template", "none")
        if template_name and template_name != "none":
            try:
                _tmp_vols: dict = {}
                _mount_filestore(
                    client,
                    settings,
                    team,
                    env_name,
                    env_db,
                    odoo_image,
                    _tmp_vols,
                    template_name=template_name,
                )
            except Exception as exc:
                logger.warning("Could not re-mount filestore overlay: %s", exc)

    # Re-create PG role in case PG container was recreated
    creds = load_credentials(
        env_name, team.workspaces_dir, settings.db_user, settings.db_password
    )
    if creds["pg_user"] != settings.db_user:
        env_db = get_db_name(env_name, team.team_id)
        try:
            _create_pg_role(
                client, settings, creds["pg_user"], creds["pg_password"], env_db
            )
        except Exception as exc:
            logger.warning("Could not re-create PG role: %s", exc)

    # Build the container environment from fresh credentials + user env, and
    # keep the persisted labels (image + env vars) in sync with the new config.
    env_dict = {
        "HOST": settings.shared_db_container,
        "USER": creds["pg_user"],
        "PASSWORD": creds["pg_password"],
        **user_env,
    }
    labels[settings.image_label] = odoo_image
    if user_env:
        labels["oduflow.env_vars"] = json.dumps(user_env)
    else:
        labels.pop("oduflow.env_vars", None)

    # Verify extra addons worktrees are intact
    extra_addons_json = labels.get("oduflow.extra_addons", "")
    if extra_addons_json:
        parsed = json.loads(extra_addons_json)
        extra_dict = _normalize_extra_addons(parsed)
        extra_dir = os.path.join(
            get_workspace_path(env_name, team.workspaces_dir), "extra"
        )
        for rn in extra_dict:
            wt = os.path.join(extra_dir, rn)
            if not os.path.isdir(wt):
                logger.warning("Extra addons worktree missing: %s", wt)

    # ------------------------------------------------------------------
    # 4. Re-create the container with the same settings
    # ------------------------------------------------------------------
    run_kwargs: dict = dict(
        image=odoo_image,
        name=odoo_container_name,
        detach=True,
        network=settings.shared_network,
        environment=env_dict,
        labels=labels,
        volumes=volumes,
        restart_policy={"Name": "unless-stopped"},
    )
    if command:
        run_kwargs["command"] = command
    if settings.routing_mode == "port" and host_port is not None:
        run_kwargs["ports"] = {"8069/tcp": host_port}

    image_updated = False
    try:
        logger.info("Pulling image %s", odoo_image)
        new_image_obj = client.images.pull(odoo_image)
        image_updated = bool(old_digest) and new_image_obj.id != old_digest
    except Exception as exc:
        logger.warning("Could not pull image %s, using local copy: %s", odoo_image, exc)

    new_container = client.containers.run(**run_kwargs)

    # Copy odoo.conf into the container (same resolution logic as create)
    repo_path = get_repo_path(env_name, team.workspaces_dir)
    workspace_path = get_workspace_path(env_name, team.workspaces_dir)
    repo_odoo_conf = os.path.join(repo_path, ".oduflow", "odoo.conf")
    if os.path.isfile(repo_odoo_conf):
        base_conf_path: str | None = repo_odoo_conf
    elif _resolve_instance_conf("odoo.conf", team.data_dir).exists():
        base_conf_path = str(_resolve_instance_conf("odoo.conf", team.data_dir))
    else:
        base_conf_path = None

    if base_conf_path:
        from oduflow.extra_addons import generate_odoo_conf

        extra_addons_json = labels.get("oduflow.extra_addons", "")
        extra_container_paths: list[str] = []
        if extra_addons_json:
            parsed = json.loads(extra_addons_json)
            extra_dict = _normalize_extra_addons(parsed)
            extra_container_paths = [f"/mnt/extra-addons-{rn}" for rn in extra_dict]
        generated_conf = os.path.join(workspace_path, "odoo.conf")
        generate_odoo_conf(base_conf_path, generated_conf, extra_container_paths)
        _copy_file_to_container(new_container, generated_conf, "/etc/odoo")

    # ------------------------------------------------------------------
    # 5. Re-install apt packages and pip requirements
    # ------------------------------------------------------------------
    setup_logs: list[str] = []
    apt_log = _install_apt_packages(new_container, repo_path)
    if apt_log:
        setup_logs.append(apt_log)
    _, pip_log = _install_pip_requirements(new_container, repo_path)
    if pip_log:
        setup_logs.append(pip_log)

    # ------------------------------------------------------------------
    # 6. Build URL and return result
    # ------------------------------------------------------------------
    if settings.routing_mode == "traefik":
        url = f"https://{get_env_hostname(env_name, team.hostname)}"
    else:
        url = f"http://{team.hostname}:{host_port}"

    env_db = get_db_name(env_name)
    workspace = get_workspace_path(env_name, team.workspaces_dir)
    logger.info(
        "Environment updated",
        extra={"env_name": env_name, "url": url, "container": odoo_container_name},
    )

    return {
        "url": url,
        "odoo_container": odoo_container_name,
        "database": env_db,
        "workspace": workspace,
        "image": odoo_image,
        "image_updated": image_updated,
        "env_vars": user_env,
        "setup_logs": setup_logs,
    }
