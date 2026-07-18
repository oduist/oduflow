from __future__ import annotations

import contextlib
import datetime
import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from typing import Any

import docker
from docker import DockerClient
from oduflow import activity, settings, usage
from oduflow.docker_ops.client import chown_recursive, get_client, get_odoo_uid_gid
from oduflow.docker_ops.stats import default_env_limits
from oduflow.docker_ops.system_ops import (
    _copy_file_to_container,
    _create_pg_role,
    _db_exists,
    _drop_pg_role,
    _exec_sql,
    _resolve_instance_conf,
    check_db_quota,
    drop_signaling_sequences,
    ensure_team_network,
    ensure_team_tablespace,
    reassign_db_ownership,
)
from oduflow.env_credentials import create_credentials, load_credentials
from oduflow.env_tokens import MCP_TOKEN_LABEL, generate_token, invalidate_cache
from oduflow.errors import (
    ConflictError,
    ExternalCommandError,
    NotFoundError,
    PrerequisiteNotMetError,
    ProtectedError,
)
from oduflow.git_ops import RepoAuthError, git_env_for_team
from oduflow.naming import (
    get_agent_checkout_dir,
    get_agent_container_name,
    get_agent_home_volume_name,
    get_agent_upload_dir,
    get_agent_workspace_volume_name,
    get_db_name,
    get_env_hostname,
    get_filestore_paths,
    get_repo_path,
    get_resource_name,
    get_team_network_name,
    get_template_db_name,
    get_workspace_path,
    redact_url_credentials,
    sanitize_repo_url,
    slugify_branch,
)
from oduflow.port_registry import allocate_port, release_port
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")

AGENT_USER = "agent"
AGENT_HOME = "/home/agent"


def _trace(msg: str, *args: object) -> None:
    if settings.TRACE:
        logger.info("[TRACE] " + msg, *args)


_PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _normalize_extra_addons(raw_addons: object) -> dict[str, str]:
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


def _extra_checkout_sources(
    container: Any, repo_names: Iterator[str]
) -> dict[str, str]:
    """Map extra-repo names to their current host bind sources."""
    targets = {f"/mnt/extra-addons-{name}": name for name in repo_names}
    sources: dict[str, str] = {}
    for bind in container.attrs.get("HostConfig", {}).get("Binds") or []:
        parts = bind.rsplit(":", 2)
        if len(parts) < 2:
            continue
        host_path, container_path = parts[0], parts[1]
        repo_name = targets.get(container_path)
        if repo_name:
            sources[repo_name] = host_path
    return sources


def _replace_extra_checkout_mounts(
    volumes: dict[str, dict[str, str]],
    labels: dict[str, str],
    checkouts: dict[str, str],
    revisions: dict[str, str],
) -> None:
    """Replace extra-addons bind sources while keeping container paths stable."""
    targets = {f"/mnt/extra-addons-{name}" for name in checkouts}
    for source, spec in list(volumes.items()):
        if spec.get("bind") in targets:
            del volumes[source]
    for repo_name, source in checkouts.items():
        volumes[source] = {
            "bind": f"/mnt/extra-addons-{repo_name}",
            "mode": "ro",
        }
    labels["oduflow.extra_addons_revisions"] = json.dumps(revisions)


def _cleanup_legacy_extra_worktrees(
    team: TeamSettings, env_name: str, repo_names: Iterator[str]
) -> None:
    """Remove old per-environment worktrees after their mounts were migrated."""
    extra_dir = os.path.join(get_workspace_path(env_name, team.workspaces_dir), "extra")
    if not os.path.isdir(extra_dir):
        return
    from oduflow.extra_addons import remove_worktree

    for repo_name in repo_names:
        path = os.path.join(extra_dir, repo_name)
        if os.path.isdir(path):
            remove_worktree(team, repo_name, path)
    with contextlib.suppress(OSError):
        os.rmdir(extra_dir)


def _redact_repo_urls(text: str, *urls: str) -> str:
    """Remove embedded credentials from known repository URLs in output."""
    redacted = text
    for url in urls:
        if url:
            redacted = redacted.replace(url, sanitize_repo_url(url))
    return redacted


def _move_inline_repo_credentials(
    repo_url: str, cred_file: str, git_user: str
) -> tuple[str, str]:
    """Store inline credentials and make their username authoritative."""
    from oduflow.git_ops import extract_and_store_inline_credentials

    clean_url, extracted_user = extract_and_store_inline_credentials(
        repo_url, cred_file
    )
    if clean_url != repo_url:
        # An explicit credential-bearing URL overrides template metadata. This
        # also clears a stale username for token-as-username URLs, whose secret
        # must not be copied into the Docker label.
        git_user = extracted_user
    return clean_url, git_user


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


def _read_template_metadata(team: TeamSettings, template_name: str) -> dict[str, Any]:
    """Template metadata.json, or an empty dict when it is absent/unreadable."""
    metadata_path = team.get_template_metadata_path(template_name)
    if not os.path.isfile(metadata_path):
        return {}
    try:
        with open(metadata_path) as f:
            loaded: dict[str, Any] = json.load(f)
            return loaded
    except (json.JSONDecodeError, OSError):
        return {}


def _template_code_lineage(
    team: TeamSettings,
    template_name: str | None,
    repo_path: str,
    *,
    current_branch: str = "",
    fetch_missing_history: bool = False,
) -> dict[str, Any]:
    """Drift between this checkout and the commit the template DB came from.

    Advisory only: templates predating provenance (and imported ones, which have
    no commit of ours behind them) yield status ``unknown`` and say nothing.
    """
    empty: dict[str, Any] = {
        "status": "unknown",
        "message": "",
        "modules_to_install": [],
        "modules_to_upgrade": [],
    }
    if template_name is None:
        return empty
    from oduflow import git_analysis, git_ops

    metadata = _read_template_metadata(team, template_name)
    try:
        template_commit = str(metadata.get("source_commit", ""))
        source_branch = str(metadata.get("source_branch", ""))
        if template_commit and fetch_missing_history:
            git_ops.ensure_lineage_history(
                repo_path,
                template_commit,
                current_branch,
                source_branch,
                team.git_credentials_file(),
            )
        return git_analysis.template_lineage(
            repo_path,
            template_commit,
            source_branch,
        )
    except Exception:  # noqa: BLE001 - never fail creation over an advisory check
        logger.warning("Template lineage check failed", exc_info=True)
        return empty


def _mount_filestore(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    env_db: str,
    odoo_image: str,
    odoo_volumes: dict[str, Any],
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

    # fuse-overlayfs is a Linux/FUSE binary; it cannot run on macOS. Off Linux,
    # always fall back to a plain filestore copy instead of failing.
    if use_overlay and not sys.platform.startswith("linux"):
        logger.info(
            "Non-Linux platform (%s): using filestore copy instead of overlay",
            sys.platform,
        )
        use_overlay = False

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
            hint = (
                " Hint: Oduflow normally runs as root, where 'allow_other' needs "
                "no extra config. If you run it as a non-root user, uncomment "
                "'user_allow_other' in /etc/fuse.conf."
            )
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

    # Prefer a direct, clean ``umount``: Oduflow runs as root, and umount(2) is
    # not mediated by the AppArmor ``fusermount3`` profile shipped on Ubuntu
    # 24.04+ (which otherwise DENIES the setuid ``fusermount`` helper, forcing
    # the lazy fallback). ``fusermount3``/``fusermount`` stay in the chain for
    # unprivileged/rootless deployments where umount(2) needs the helper. Lazy
    # ``umount -l`` is the last resort only — it detaches even a busy mount,
    # which can leave a still-bound container with a broken filestore.
    for cmd in (
        ["umount", merged],
        ["fusermount3", "-u", merged],
        ["fusermount", "-u", merged],
        ["umount", "-l", merged],
    ):
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            logger.info(
                "Filestore overlay unmounted via '%s'",
                " ".join(cmd[:-1]),
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

        container_name = get_resource_name(
            env_name, "odoo", settings.prefix, team.team_id
        )
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


def _install_apt_packages(container: Any, repo_path: str) -> str:
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


def _ensure_user_site_packages(container: Any) -> None:
    """Create the user site-packages directory for the odoo user and fix ownership.

    This allows ``pip install --user`` to work inside containers where
    ``/var/lib/odoo/.local`` may not exist or may be owned by root.

    Only the pip target dirs are chowned, never recursively into
    ``.local/share``: the Odoo filestore is bind-mounted at
    ``.local/share/Odoo/filestore/<db>`` from an overlay's read-only lower
    layer, so ``chown -R /var/lib/odoo/.local`` would change ownership on every
    filestore file and force fuse-overlayfs to copy the entire template
    filestore up into this environment's upper layer — defeating the overlay
    (each env would duplicate the full filestore instead of sharing the
    template's lower layer).
    """
    container.exec_run(
        "mkdir -p /var/lib/odoo/.local/lib /var/lib/odoo/.local/bin",
        user="root",
    )
    # Non-recursive on .local so .local/share (the bind-mounted filestore) is
    # never touched; recursive only on the pip dirs, which never hold it.
    container.exec_run(
        "chown odoo:odoo /var/lib/odoo/.local",
        user="root",
    )
    container.exec_run(
        "chown -R odoo:odoo /var/lib/odoo/.local/lib /var/lib/odoo/.local/bin",
        user="root",
    )
    logger.debug("Ensured pip user dirs under /var/lib/odoo/.local are owned by odoo")


def _install_pip_requirements(
    container: Any, repo_path: str, *, restart: bool = True
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


def _odoo_registry_probe(env_db: str) -> list[str]:
    """Build an in-container probe that forces the target registry to load."""
    script = (
        "import http.cookiejar,sys,urllib.parse,urllib.request;"
        f"db={env_db!r};"
        "jar=http.cookiejar.CookieJar();"
        "opener=urllib.request.build_opener("
        "urllib.request.HTTPCookieProcessor(jar));"
        "url='http://127.0.0.1:8069/web/login?'+urllib.parse.urlencode({'db':db});"
        "response=opener.open(url,timeout=5);"
        "sys.exit(0 if response.status==200 else 1)"
    )
    return ["python3", "-c", script]


def _wait_for_container_odoo_ready(
    container: Any, env_db: str, timeout: int = 120
) -> bool:
    """Wait until the serving Odoo process has finished registry startup.

    The probe runs inside the container so readiness does not depend on public
    DNS, TLS, Traefik, or a published host port. It requests ``/web/login`` with
    a cookie-aware opener: Odoo's first response selects ``env_db`` and the
    redirected request must pass through that database's registry. A secondary
    Odoo command must not start before this succeeds: on cloned Odoo 15
    databases both processes can otherwise race while re-creating the registry
    signaling sequences. ``/web/health`` is not sufficient here because it is
    database-independent and can respond before registry preloading completes.
    """
    probe = _odoo_registry_probe(env_db)
    deadline = time.monotonic() + timeout
    while True:
        try:
            if container.exec_run(probe)[0] == 0:
                return True
        except docker.errors.APIError:
            # The container may briefly reject execs while restarting after a
            # dependency install. Treat that exactly like a failed probe.
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(2)


def _auto_install_modules(
    container: Any, env_db: str, modules: list[str], env_name: str = ""
) -> str:
    """Install modules after the serving registry is ready and return its log."""
    modules_str = ",".join(modules)
    logger.info(
        "Waiting for serving Odoo before auto-installing modules",
        extra={"env_name": env_name},
    )
    if not _wait_for_container_odoo_ready(container, env_db):
        message = (
            f"[AUTO-INSTALL] odoo -i {modules_str} FAILED: serving Odoo did not "
            "become ready within 120 seconds; installation was not attempted"
        )
        logger.error(message, extra={"env_name": env_name})
        return message

    install_cmd = (
        f"/entrypoint.sh odoo -d {env_db} -i {modules_str} --stop-after-init --no-http"
    )
    exit_code, output = container.exec_run(install_cmd)
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)
    if exit_code != 0:
        logger.error(
            "Auto-install modules failed (exit %d): %s",
            exit_code,
            output_str,
            extra={"env_name": env_name},
        )
        return (
            f"[AUTO-INSTALL] odoo -i {modules_str} FAILED (exit {exit_code}):\n"
            f"{output_str}"
        )
    return f"[AUTO-INSTALL] odoo -i {modules_str} completed successfully"


def _cleanup_old_environment(
    client: "DockerClient",
    settings: Settings,
    team: TeamSettings,
    env_name: str,
) -> None:
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )
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
    team: TeamSettings,
    odoo_image: str,
    env_db: str,
    odoo_env: dict[str, Any],
    odoo_volumes: dict[str, Any],
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
    init_name = get_resource_name(env_name, "odoo-init", settings.prefix, team.team_id)
    try:
        client.containers.get(init_name).remove(force=True)
    except docker.errors.NotFound:
        pass

    init_container = client.containers.run(
        image=odoo_image,
        name=init_name,
        detach=True,
        network=get_team_network_name(team.team_id, settings.prefix),
        extra_hosts={"host.docker.internal": "host-gateway"},
        **default_env_limits(),
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


def _clone_repo(
    repo_url: str,
    branch: str,
    repo_path: str,
    team: TeamSettings,
    *,
    git_user: str = "",
    depth: int = 1,
    timeout: int = 60,
) -> None:
    """Clone *branch* of *repo_url* into *repo_path* with team credentials.

    ``depth=1`` (default) is the dev-environment shallow clone; production
    passes ``depth=0`` for a full clone — commit history is the point there
    (rollback targets, deploy history display).
    """
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

    cmd = ["git", "clone", "--branch", branch]
    if depth > 0:
        cmd += ["--depth", str(depth)]
    cmd += [clone_url, repo_path]
    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            timeout=timeout,
            env=git_env,
        )
    except subprocess.CalledProcessError as e:
        error_msg = redact_url_credentials(
            e.stderr.decode("utf-8") if e.stderr else str(e)
        )
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
            f"Repository clone timed out ({timeout}s). Repository may be too "
            "large or network is slow.",
        )


def build_env_traefik_labels(
    settings: Settings, team: TeamSettings, env_name: str
) -> dict[str, str]:
    """Traefik docker-provider routing labels for an environment's Odoo container.

    Empty in port mode. This is the single source of an environment's routing
    labels, shared by :func:`create_environment` and :func:`update_environment`
    so a container recreated by an update always reflects the *current*
    ``routing_mode`` / ``routing_tls`` / team ``hostname`` — labels are frozen at
    container creation, so this is what lets a mode/TLS/hostname switch be
    repaired by a plain ``update_environment`` instead of a destroy+create.
    """
    if settings.routing_mode != "traefik":
        return {}
    slug = slugify_branch(env_name)
    router = f"oduflow-{team.team_id}-{slug}"
    host = get_env_hostname(env_name, team.hostname)
    labels: dict[str, str] = {
        "traefik.enable": "true",
        f"traefik.http.routers.{router}.rule": f"Host(`{host}`)",
        f"traefik.http.services.{router}.loadbalancer.server.port": "8069",
        "traefik.docker.network": get_team_network_name(team.team_id, settings.prefix),
    }
    if settings.routing_tls:
        labels[f"traefik.http.routers.{router}.entrypoints"] = "websecure"
        labels[f"traefik.http.routers.{router}.tls"] = "true"
        labels[f"traefik.http.routers.{router}.tls.certresolver"] = "letsencrypt"
    else:
        # Upstream (e.g. Cloudflare tunnel) terminates TLS; Traefik routes plain
        # HTTP on the web entrypoint. Public URL stays https://.
        labels[f"traefik.http.routers.{router}.entrypoints"] = "web"
    return labels


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
) -> dict[str, Any]:
    env_name = env_name or branch
    from oduflow.naming import PROD_ENV_PREFIX

    if env_name.startswith(PROD_ENV_PREFIX):
        raise ConflictError(
            f"Environment names starting with '{PROD_ENV_PREFIX}' are reserved "
            "for production environments. Use create_production instead."
        )
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

    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )
    try:
        existing = client.containers.get(odoo_container_name)
        if existing.status == "running":
            existing.reload()
            if settings.routing_mode == "traefik":
                url = f"https://{get_env_hostname(env_name, team.hostname)}"
            else:
                ports = existing.ports.get("8069/tcp")
                existing_port = ports[0]["HostPort"] if ports else "?"
                url = f"http://{team.hostname}:{existing_port}"
            raise ConflictError(
                f"Environment '{env_name}' already exists and is running at {url}."
            )
        raise ConflictError(
            f"Environment '{env_name}' already exists (status: {existing.status})."
        )
    except docker.errors.NotFound:
        pass

    # Different branch names can normalise to the same database name (the
    # container name keeps case/punctuation but get_db_name slugifies). Refuse
    # if the target DB is already owned by another live environment — otherwise
    # the cleanup below would DROP a running environment's database (#41).
    target_db = get_db_name(env_name, team.team_id)
    for other in client.containers.list(
        all=True,
        filters={
            "label": [
                f"{settings.managed_label}=true",
                f"{settings.team_label}={team.team_id}",
            ]
        },
    ):
        if other.name == odoo_container_name:
            continue
        other_branch = other.labels.get(settings.branch_label)
        if other_branch and get_db_name(other_branch, team.team_id) == target_db:
            raise ConflictError(
                f"Environment '{env_name}' maps to database '{target_db}', which "
                f"is already used by environment '{other_branch}'. Choose a name "
                "that does not normalise to the same database."
            )

    check_db_quota(client, settings, team)
    ensure_team_network(client, settings, team)

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

    # Keep inline git credentials (https://user:PAT@host/...) out of the Docker
    # label (world-readable via `docker inspect`, copied into template metadata):
    # move them into the team credential store, then persist only a sanitized URL.
    # Clone/recreate/agent paths already authenticate via that store. No-op for
    # clean URLs and for live-mount (which never clones).
    if not local_mount and repo_url:
        repo_url, git_user = _move_inline_repo_credentials(
            repo_url, team.git_credentials_file(), git_user
        )

    labels = {
        settings.managed_label: "true",
        settings.team_label: team.team_id,
        settings.branch_label: env_name,
        settings.repo_label: repo_url,
        settings.image_label: odoo_image,
        "oduflow.template": template_name if template_name is not None else "none",
        "oduflow.git_branch": branch,
        "oduflow.created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        # Personal token authorizing the scoped MCP endpoint /mcp/<env>.
        MCP_TOKEN_LABEL: generate_token(),
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

    labels.update(build_env_traefik_labels(settings, team, env_name))

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
        _clone_repo(
            repo_url,
            branch,
            repo_path,
            team,
            git_user=git_user,
        )

    # The template database is a snapshot of some commit; this checkout may sit
    # before or after it. Report the drift now, while the agent is still deciding
    # what its first pull_and_apply should do.
    lineage = _template_code_lineage(
        team,
        template_name,
        repo_path,
        current_branch=branch,
        fetch_missing_history=not local_mount,
    )

    # --- Shared immutable extra-addons checkouts ---
    extra_mount_paths: list[tuple[str, str]] = []
    extra_revisions: dict[str, str] = {}
    if extra_addons:
        from oduflow.extra_addons import ensure_shared_checkout

        for repo_name, addon_branch in extra_addons.items():
            checkout = ensure_shared_checkout(team, repo_name, addon_branch)
            container_path = f"/mnt/extra-addons-{repo_name}"
            extra_mount_paths.append((checkout["path"], container_path))
            extra_revisions[repo_name] = checkout["revision"]
        labels["oduflow.extra_addons_revisions"] = json.dumps(extra_revisions)

    ts_name = ensure_team_tablespace(client, settings, team)
    if template_name is not None:
        tpl_db = get_template_db_name(template_name, team.team_id)
        _exec_sql(
            client,
            settings,
            f'CREATE DATABASE "{env_db}" TEMPLATE "{tpl_db}" TABLESPACE "{ts_name}";',
        )
    else:
        _exec_sql(
            client,
            settings,
            f'CREATE DATABASE "{env_db}" TABLESPACE "{ts_name}";',
        )

    env_creds = create_credentials(env_name, team.team_id, team.workspaces_dir)
    _create_pg_role(
        client, settings, env_creds["pg_user"], env_creds["pg_password"], env_db
    )

    if template_name is not None:
        # A template DB's objects are owned by whatever role created them —
        # normally the superuser (pg_restore --no-owner), but the plain-SQL /
        # import_template_from_odoo path (psql without --no-owner) can leave
        # objects owned by the source env's per-env role (e.g. u_2_fs19). DDL
        # during module upgrades requires ownership; Odoo connects as the env
        # role and never SET ROLEs, so per-object ownership — not role
        # membership — is what actually enables its DDL.
        reassign_db_ownership(client, settings, env_db, env_creds["pg_user"])
        drop_signaling_sequences(client, settings, env_db)
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
        from oduflow.extra_addons import generate_odoo_conf, resolve_main_addons_path

        generated_conf = os.path.join(workspace_path, "odoo.conf")
        extra_container_paths = (
            [cp for _, cp in extra_mount_paths] if extra_mount_paths else []
        )
        main_addons_path = resolve_main_addons_path(repo_path)
        generate_odoo_conf(
            base_conf_path, generated_conf, extra_container_paths, main_addons_path
        )
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

    run_kwargs: dict[str, Any] = dict(
        image=odoo_image,
        name=odoo_container_name,
        detach=True,
        network=get_team_network_name(team.team_id, settings.prefix),
        extra_hosts={"host.docker.internal": "host-gateway"},
        **default_env_limits(),
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
                client,
                settings,
                team,
                odoo_image,
                env_db,
                odoo_env,
                odoo_volumes,
                env_name,
            )
        )

    try:
        container = client.containers.run(**run_kwargs)
    except Exception:
        # The serving container failed to start (e.g. an invalid container name
        # derived from the branch, or a host-port bind conflict). Roll back the
        # resources created so far — database, role, filestore mount, workspace
        # and the allocated port — so they are not left orphaned (#49).
        logger.error(
            "containers.run failed for '%s'; rolling back partial environment",
            env_name,
        )
        if settings.routing_mode == "port":
            release_port(team.port_registry_path, env_name)
        _cleanup_old_environment(client, settings, team, env_name)
        raise

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

    # --- Auto-install modules ---
    # MUST run before sanitization below: Odoo's native neutralization gathers
    # SQL only from already-installed modules, so any `data/neutralize.sql`
    # shipped by an auto-installed module — and the crons, payment providers, or
    # connector credentials those modules create — would be missed if neutralize
    # ran first. Install now, neutralize after.
    if auto_install_modules:
        modules_str = ",".join(auto_install_modules)
        logger.info(
            "Auto-installing modules: %s",
            modules_str,
            extra={"env_name": env_name},
        )
        setup_logs.append(
            _auto_install_modules(
                container, env_db, auto_install_modules, env_name=env_name
            )
        )
        # NOTE: the serving container is restarted further down, AFTER
        # sanitization, so the newly-installed modules' crons are already
        # deactivated in the DB before PID1 reloads them into a live registry.

    # --- Sanitize environment database ---
    # Runs AFTER auto-install so Odoo's native neutralization sees the final set
    # of installed modules (see the auto-install note above).
    if sanitize and template_name is not None:
        from oduflow.sanitizer import neutralize_environment, sanitize_environment

        # Layer 1: Odoo's native neutralization — the baseline safety net that
        # blocks anything going out (mail off, crons off, payment providers off,
        # third-party credentials scrubbed, database.is_neutralized=true). Runs
        # in the serving container so it sees the full addons_path.
        setup_logs.extend(neutralize_environment(client, settings, team, env_name))
        # Layer 2: custom team/repo sanitization scripts (e.g. PII scrubbing)
        # run on top of the neutralized database. Project scripts live under
        # .oduflow/odoo_sanitize in the active checkout.
        sanitize_logs = sanitize_environment(client, settings, team, env_name)
        setup_logs.extend(sanitize_logs)

    # Reload any auto-installed modules into the serving registry — done after
    # sanitization so their crons/credentials are neutralized in the DB first.
    if auto_install_modules:
        container.restart()

    if settings.routing_mode == "traefik":
        url = f"https://{get_env_hostname(env_name, team.hostname)}"
    else:
        url = f"http://{team.hostname}:{host_port}"
    logger.info(
        "Environment created",
        extra={"env_name": env_name, "url": url, "container": odoo_container_name},
    )

    # Add this environment's checkout to the team's agent container
    # (best-effort; ensures the container exists; only when enabled). Live-mount
    # environments have no repo to clone — clone-env.sh skips on an empty URL.
    _agent_add_env(
        client,
        settings,
        team,
        env_name,
        "" if local_mount else repo_url,
        branch,
        git_user,
    )

    result: dict[str, Any] = {
        "url": url,
        "odoo_container": odoo_container_name,
        "database": env_db,
        "workspace": workspace_path,
        "setup_logs": setup_logs,
    }
    result["extra_addons"] = extra_addons or {}
    result["extra_addons_revisions"] = extra_revisions
    result["local_path"] = repo_path if local_mount else ""
    result["template_lineage"] = lineage
    result["elapsed_seconds"] = round(time.time() - start_time, 1)
    # Capability token the usage hook authenticates with (X-Oduflow-Env-Uid).
    result["usage_uid"] = usage.register_token(settings, team, env_name)
    activity.touch(team, env_name)
    # Let the new env's MCP token resolve without waiting for the scan interval.
    invalidate_cache()
    return result


_AGENT_PROVIDER_CREDENTIALS = (
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENCODE_API_KEY",
)


def _agent_env_vars(settings: Settings, team: TeamSettings) -> dict[str, str]:
    """Variables injected into the team's agent container.

    Two sources, in order of increasing precedence:
      1. Known provider keys present in the SERVER environment (convenient for
         headless/prod: CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY /
         OPENAI_API_KEY / OPENCODE_API_KEY) — but only in single-team
         deployments; with several teams a server-level key would leak the
         operator's credential into every tenant's container.
      2. The team's ``[team.X.agent_env]`` TOML table (arbitrary KEY=VALUE,
         including those same credentials).

    For Claude the OAuth token and the API key are mutually exclusive — the key
    would silently override the subscription — so when a non-empty token is
    present the API key is dropped.
    """
    env: dict[str, str] = {}
    if len(settings.teams) == 1:
        for key in _AGENT_PROVIDER_CREDENTIALS:
            value = os.environ.get(key, "").strip()
            if value:
                env[key] = value
    env.update(team.agent_env)

    # Credentials are routinely pasted from another terminal. Normalize only
    # the known secret values (arbitrary user variables may intentionally carry
    # surrounding whitespace), and make an explicitly blank team override mask
    # a same-named server credential instead of injecting an empty value.
    for key in _AGENT_PROVIDER_CREDENTIALS:
        if key not in env:
            continue
        value = env[key].strip()
        if value:
            env[key] = value
        else:
            env.pop(key)

    if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop("ANTHROPIC_API_KEY", None)
    return env


def _claude_auth_mode(settings: Settings, team: TeamSettings) -> str:
    """Return the one effective Claude credential mode, without secrets.

    Environment credentials are alternatives, not an automatic fallback
    chain: a setup token wins over an API key, and a persisted interactive
    login is used only when neither environment credential is present.
    """
    env = _agent_env_vars(settings, team)
    if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "setup_token"
    if env.get("ANTHROPIC_API_KEY"):
        return "api_key"
    return "interactive"


def get_agent_mcp_url(settings: Settings, team: TeamSettings, env_name: str) -> str:
    """This environment's SCOPED MCP endpoint as seen from the agent container.

    The scoped ``/mcp/<env>`` endpoint + per-environment token (ADR 0028) is
    the only Oduflow access the agent gets — the team ``auth_token`` never
    enters the agent container. In Traefik mode agents use the team's public
    TLS endpoint, matching the dashboard's MCP Access URL. Port mode uses an
    explicitly configured public OAuth base when available, otherwise it falls
    back to the Docker host gateway for local deployments."""
    from urllib.parse import quote

    if settings.routing_mode == "traefik":
        base = f"https://{team.hostname}"
    elif settings.oauth_base_url:
        base = settings.oauth_base_url.rstrip("/")
    else:
        base = f"http://host.docker.internal:{settings.port}"
    return f"{base}/mcp/{quote(env_name, safe='/')}"


def refresh_agent_mcp_config(
    container: docker.models.containers.Container,
    settings: Settings,
    team: TeamSettings,
    env_name: str,
) -> None:
    """Refresh an existing Claude checkout's generated Oduflow MCP entry.

    Agent workspaces persist across Oduflow/coder upgrades, so a checkout may
    still contain the old host-gateway URL even after URL selection is fixed.
    Rewrite only the generated ``oduflow`` entry, preserving Agent Browser and
    any user-added servers. The token remains an environment placeholder and
    is never written to disk.
    """
    config_path = os.path.join(get_agent_checkout_dir(env_name), ".mcp.json")
    script = """\
set -eu
config_path=$1
mcp_url=$2
[ -f "$config_path" ] || exit 0
tmp_path="${config_path}.tmp.$$"
trap 'rm -f "$tmp_path"' EXIT
jq --arg url "$mcp_url" \
  '.mcpServers.oduflow = {type: "http", url: $url, headers: {Authorization: "Bearer ${ODUFLOW_MCP_TOKEN}"}}' \
  "$config_path" > "$tmp_path"
mv "$tmp_path" "$config_path"
"""
    exit_code, output = container.exec_run(
        [
            "sh",
            "-c",
            script,
            "sh",
            config_path,
            get_agent_mcp_url(settings, team, env_name),
        ],
        user=AGENT_USER,
    )
    if exit_code not in (0, None):
        detail = (
            output.decode("utf-8", "replace")
            if isinstance(output, (bytes, bytearray))
            else output
        )
        logger.warning(
            "Agent MCP config refresh for '%s' exited %s: %s",
            env_name,
            exit_code,
            detail,
        )


def _agent_container_config(
    settings: Settings, team: TeamSettings, env: dict[str, str]
) -> dict[str, Any]:
    """Docker run configuration for the long-lived per-team agent container.

    This dictionary is both hashed and passed to ``containers.run``. Keeping a
    single declarative representation makes every material container-spec
    change trigger recreation without a separately maintained runtime epoch.
    """
    return {
        "image": settings.agent_image,
        "detach": True,
        "user": AGENT_USER,
        "network": get_team_network_name(team.team_id, settings.prefix),
        "environment": env,
        "volumes": {
            get_agent_home_volume_name(team.team_id, settings.prefix): {
                "bind": AGENT_HOME,
                "mode": "rw",
            },
            get_agent_workspace_volume_name(team.team_id, settings.prefix): {
                "bind": "/workspace",
                "mode": "rw",
            },
        },
        "extra_hosts": {"host.docker.internal": "host-gateway"},
        "shm_size": "1g",
        "restart_policy": {"Name": "unless-stopped"},
    }


def _agent_config_hash(
    container_config: dict[str, Any], has_git_credentials: bool
) -> str:
    """Fingerprint of the config the agent container was created with.

    Stored as a container label; a mismatch on ensure means the image, injected
    config, or Docker run specification changed, so the container is recreated.
    HOME and /workspace are volumes — nothing is lost. Git credentials are
    copied into HOME by the volume init step at container creation, so their
    presence is also part of the fingerprint: a container created before
    setup_repo_auth would otherwise keep matching forever and never pick up the
    file."""
    import hashlib

    payload = json.dumps(
        {
            "container": container_config,
            "has_git_credentials": has_git_credentials,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _prepare_agent_volumes(
    client: DockerClient,
    image: str,
    home_volume: str,
    workspace_volume: str,
    cred_file: str | None,
) -> None:
    """Make persistent agent data usable by the unprivileged image user.

    Older coder images mounted HOME at ``/root`` and consequently left both
    named volumes root-owned. A short-lived, networkless init container runs
    as root only to migrate that existing data and copy the host credential
    store; the long-lived container and every agent exec run as ``agent``.
    Marker files avoid recursively walking large checkouts after migration.
    """
    volumes: dict[str, dict[str, str]] = {
        home_volume: {"bind": AGENT_HOME, "mode": "rw"},
        workspace_volume: {"bind": "/workspace", "mode": "rw"},
    }
    if cred_file:
        volumes[cred_file] = {
            "bind": "/run/oduflow/git-credentials",
            "mode": "ro",
        }

    script = """
if ! id agent >/dev/null 2>&1; then
    echo "incompatible coder image: missing required user 'agent'" >&2
    echo "build the current docker/agent image and configure [agent].image to that tag" >&2
    exit 64
fi
if [ ! -f /home/agent/.oduflow-owner-v2 ]; then
    chown -R agent:agent /home/agent
    touch /home/agent/.oduflow-owner-v2
    chown agent:agent /home/agent/.oduflow-owner-v2
fi
if [ ! -f /workspace/.oduflow-owner-v2 ]; then
    chown -R agent:agent /workspace
    touch /workspace/.oduflow-owner-v2
    chown agent:agent /workspace/.oduflow-owner-v2
fi
if [ -f /run/oduflow/git-credentials ]; then
    install -m 600 -o agent -g agent \
        /run/oduflow/git-credentials /home/agent/.git-credentials
fi
"""
    client.containers.run(
        image=image,
        command=["-eu", "-c", script],
        entrypoint=["/bin/sh"],
        user="root",
        remove=True,
        network_disabled=True,
        volumes=volumes,
    )


def _ensure_agent_container(
    client: DockerClient, settings: Settings, team: TeamSettings
) -> None:
    """Ensure the team's coding-agent container (best-effort, opt-in).

    One container serves every environment of the team. Its HOME (auth +
    sessions) and /workspace (one checkout per environment, at
    /workspace/<slug>) live on persistent named volumes, so a login done once
    survives recreation and is shared by the team's environments. Per-env
    checkouts are created by ``_agent_add_env``; the agent reaches each
    environment only through the Oduflow MCP server (git push ->
    pull_and_apply). Config (oduflow.toml) is the source of truth: an existing
    container created with a different agent config (credentials, image, port)
    is recreated automatically. Never fatal to the caller.
    See specs/0029-agent-console-and-chat.md.
    """
    if not team.agent_enabled:
        return

    try:
        agent_labels = {
            settings.managed_label: "true",
            settings.system_label: "true",
            settings.team_label: team.team_id,
        }
        home_volume = get_agent_home_volume_name(team.team_id, settings.prefix)
        workspace_volume = get_agent_workspace_volume_name(
            team.team_id, settings.prefix
        )
        for volume in (home_volume, workspace_volume):
            try:
                client.volumes.get(volume)
            except docker.errors.NotFound:
                client.volumes.create(volume, labels=agent_labels)

        # Provider/user vars only. Deliberately NO Oduflow MCP credentials
        # here: the container-wide env is readable from any agent console
        # (`env`, /proc/1/environ), so the team auth_token must never enter
        # the container. Each console/chat session injects its environment's
        # SCOPED token + /mcp/<env> URL into its own `docker exec` env instead
        # (see web_ui.ws_agent_console / ws_agent_acp and ADR 0028).
        agent_env = dict(_agent_env_vars(settings, team))
        cred_file = team.git_credentials_file()
        has_git_credentials = os.path.isfile(cred_file)
        container_config = _agent_container_config(settings, team, agent_env)
        config_hash = _agent_config_hash(container_config, has_git_credentials)

        container_name = get_agent_container_name(team.team_id, settings.prefix)
        existing_container = None
        try:
            existing_container = client.containers.get(container_name)
            if (
                existing_container.labels.get("oduflow.agent_config_hash", "")
                == config_hash
            ):
                if existing_container.status != "running":
                    existing_container.start()
                return
        except docker.errors.NotFound:
            pass

        # Resolve the exact immutable image before touching a working
        # container. A failed pull therefore leaves the previous agent
        # available and its old config hash ensures a later restart retries.
        try:
            client.images.pull(settings.agent_image)
        except Exception:
            outcome = (
                "keeping existing container"
                if existing_container is not None
                else "agent container was not created"
            )
            logger.warning(
                "Could not pull agent image %s; %s",
                settings.agent_image,
                outcome,
                extra={"container": container_name},
                exc_info=True,
            )
            return

        if existing_container is not None:
            # Config changed — recreate with the already-pulled image. HOME and
            # /workspace are volumes, so auth, sessions and checkouts survive.
            logger.info(
                "Agent config changed; recreating container",
                extra={"container": container_name},
            )
            existing_container.remove(force=True)

        ensure_team_network(client, settings, team)

        labels = dict(agent_labels)
        labels.update(
            {
                "oduflow.agent": "true",
                "oduflow.agent_config_hash": config_hash,
                "oduflow.created_at": datetime.datetime.now(
                    datetime.timezone.utc
                ).isoformat(),
            }
        )

        _prepare_agent_volumes(
            client,
            settings.agent_image,
            home_volume,
            workspace_volume,
            cred_file if has_git_credentials else None,
        )
        client.containers.run(
            name=container_name,
            labels=labels,
            **container_config,
        )
        logger.info("Agent container ensured", extra={"container": container_name})
        # Report which Claude credential the (re)created container will use, so
        # authentication failures can be diagnosed from the logs. The secret
        # value is never logged, only the selected mode.
        auth_label = {
            "setup_token": "subscription (CLAUDE_CODE_OAUTH_TOKEN)",
            "api_key": "Console API (ANTHROPIC_API_KEY)",
            "interactive": "interactive /login",
        }.get(_claude_auth_mode(settings, team), "unknown")
        logger.info("Claude auth: %s", auth_label, extra={"container": container_name})
    except Exception:
        logger.warning("Agent container ensure failed", exc_info=True)


def _agent_add_env(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    repo_url: str,
    branch: str,
    git_user: str,
) -> None:
    """Create/refresh one environment's checkout in the team's agent container.

    Ensures the container exists, then execs clone-env.sh to clone/fetch the
    branch into /workspace/<slug> and write that checkout's Claude .mcp.json.
    Best-effort: never fatal to environment creation.
    """
    if not team.agent_enabled:
        return
    try:
        _ensure_agent_container(client, settings, team)
        container = client.containers.get(
            get_agent_container_name(team.team_id, settings.prefix)
        )
        # The scoped per-env URL only; no token argument — the checkout's
        # .mcp.json holds a ${ODUFLOW_MCP_TOKEN} placeholder that each session
        # resolves from its own exec env.
        cmd = [
            "/usr/local/bin/clone-env.sh",
            repo_url,
            branch,
            slugify_branch(env_name),
            get_agent_mcp_url(settings, team, env_name),
            git_user,
        ]
        exit_code, output = container.exec_run(cmd, user=AGENT_USER)
        if exit_code not in (0, None):
            detail = (
                output.decode("utf-8", "replace")
                if isinstance(output, (bytes, bytearray))
                else output
            )
            logger.warning(
                "Agent checkout setup for '%s' exited %s: %s",
                env_name,
                exit_code,
                detail,
            )
    except Exception:
        logger.warning("Agent checkout setup failed for '%s'", env_name, exc_info=True)


def ensure_agent_env_checkout(
    settings: Settings, team: TeamSettings, env_name: str
) -> None:
    """Ensure an environment's checkout exists in the team's agent container.

    Clones it on demand (idempotent) by reading repo/branch/git_user from the
    environment's Odoo container labels. Used when opening a console/chat so
    that environments created before the agent feature — or a fresh workspace
    volume — get their ``/workspace/<slug>`` checkout without a full recreate.
    Best-effort.
    """
    if not team.agent_enabled:
        return
    try:
        client = get_client()
        odoo = client.containers.get(
            get_resource_name(env_name, "odoo", settings.prefix, team.team_id)
        )
    except docker.errors.NotFound:
        return
    except Exception:
        logger.warning("Agent checkout ensure failed for '%s'", env_name, exc_info=True)
        return
    labels = odoo.labels or {}
    # Live-mount environments have no repo to clone from; skip (clone-env.sh
    # would skip on the empty URL anyway).
    repo_url = (
        "" if labels.get("oduflow.local_path") else labels.get(settings.repo_label, "")
    )
    branch = labels.get("oduflow.git_branch", env_name)
    git_user = labels.get("oduflow.git_user", "")
    _agent_add_env(client, settings, team, env_name, repo_url, branch, git_user)


def _agent_remove_env(
    client: DockerClient, settings: Settings, team: TeamSettings, env_name: str
) -> None:
    """Remove one environment's checkout and Agent Chat attachments.

    Best-effort; the container itself keeps running (it serves other envs).
    """
    # A name that slugifies to nothing would make the checkout dir /workspace/
    # itself — never rm -rf the shared volume holding every env's checkout.
    # (clone-env.sh has the matching guard on the create side.)
    if not slugify_branch(env_name):
        logger.warning("Agent checkout removal skipped: empty slug for '%s'", env_name)
        return
    try:
        container = client.containers.get(
            get_agent_container_name(team.team_id, settings.prefix)
        )
    except docker.errors.NotFound:
        return
    except Exception:
        logger.warning(
            "Agent checkout removal failed for '%s'", env_name, exc_info=True
        )
        return
    with contextlib.suppress(Exception):
        container.exec_run(
            [
                "rm",
                "-rf",
                "--",
                get_agent_checkout_dir(env_name),
                get_agent_upload_dir(env_name),
            ],
            user=AGENT_USER,
        )


def _remove_agent_container(
    client: DockerClient, settings: Settings, team: TeamSettings
) -> None:
    """Remove the team's agent container (best-effort).

    Volumes are left intact so auth/sessions and checkouts survive.
    """
    try:
        client.containers.get(
            get_agent_container_name(team.team_id, settings.prefix)
        ).remove(force=True)
    except docker.errors.NotFound:
        pass
    except Exception:
        logger.warning("Agent container removal failed", exc_info=True)


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
    container_name = get_resource_name(env_name, "odoo", settings.prefix, team.team_id)
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
    container_name = get_resource_name(env_name, "odoo", settings.prefix, team.team_id)
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


def get_env_token(settings: Settings, team: TeamSettings, env_name: str) -> str | None:
    """Return the per-environment MCP access token from the container label.

    Returns ``None`` for environments created before the feature existed (their
    container carries no ``oduflow.mcp_token`` label and Docker labels cannot be
    added without recreating the container).
    """
    client = get_client()
    container_name = get_resource_name(env_name, "odoo", settings.prefix, team.team_id)
    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Environment '{env_name}' does not exist.")
    return container.labels.get(MCP_TOKEN_LABEL) or None


def get_note(settings: Settings, team: TeamSettings, env_name: str) -> str:
    return _get_note_text(env_name, team.workspaces_dir)


def set_note(
    settings: Settings, team: TeamSettings, env_name: str, note: str
) -> dict[str, Any]:
    """Set or clear a note for an environment."""
    client = get_client()
    container_name = get_resource_name(env_name, "odoo", settings.prefix, team.team_id)
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
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )
    env_db = get_db_name(env_name, team.team_id)
    workspace_path = get_workspace_path(env_name, team.workspaces_dir)

    # Defence in depth before the rmtree below: env_name is validated at creation,
    # but reject any name whose derived path escapes (or equals) the workspaces
    # dir — a "." / ".." env_name must never let a delete reach outside it.
    _ws_root = os.path.realpath(team.workspaces_dir)
    _target = os.path.realpath(workspace_path)
    if _target == _ws_root or os.path.commonpath([_ws_root, _target]) != _ws_root:
        raise NotFoundError(f"Environment '{env_name}' does not exist.")

    container_exists = True
    try:
        existing = client.containers.get(odoo_container_name)
        # Defence in depth on top of team-scoped names: never stop/remove a
        # container that belongs to another team (issue #39). Treat it as
        # absent so this team's own DB/workspace/port are still cleaned up.
        label = existing.labels.get(settings.team_label)
        if label is not None and label != team.team_id:
            container_exists = False
    except docker.errors.NotFound:
        container_exists = False

    if not container_exists and not os.path.exists(workspace_path):
        raise NotFoundError(f"Environment '{env_name}' does not exist.")

    warnings: list[str] = []

    logger.info("Deleting environment", extra={"env_name": env_name})

    _agent_remove_env(client, settings, team, env_name)

    # Forget any stored ACP chat sessions for this env (host-side state, so
    # cleared regardless of the agent container's presence) — a later env of the
    # same name then starts a fresh conversation instead of resuming this one.
    from oduflow import agent_sessions

    with contextlib.suppress(Exception):
        agent_sessions.clear_session(team, env_name)

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

    # Preserve usage accounting beyond the ephemeral environment, then drop its
    # capability token. Archiving reads .usage.json, so it must precede rmtree.
    usage.archive_on_delete(team, env_name)
    usage.revoke_token(settings, team, env_name)

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
    invalidate_cache()
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
                "extra_addons_revisions": json.loads(
                    container.labels.get("oduflow.extra_addons_revisions", "{}")
                ),
                "db_name": get_db_name(env_name, team.team_id),
                "protected": is_protected(settings, team, env_name),
                "auto_install_modules": container.labels.get(
                    "oduflow.auto_install_modules", ""
                ),
                "created_at": container.labels.get("oduflow.created_at", "")
                or container.attrs.get("Created", ""),
                "note": _get_note_text(env_name, team.workspaces_dir),
                "usage": usage.get_env_usage(team, env_name),
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
    import urllib.error
    import urllib.request

    # Path-free base URL: `get_environment_info()["url"]` ends in "/web?debug=1",
    # so appending "/web/health" to it probed "/web" instead — readiness then
    # meant "the login page renders", not "Odoo is healthy".
    try:
        base_url, _cookie_domain = get_env_base_url(settings, team, env_name)
    except NotFoundError:
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


def ensure_running(settings: Settings, env_name: str, team: TeamSettings) -> bool:
    """Start the environment's Odoo container if it is stopped.

    Returns True when a start was needed (the caller may want to tell the
    agent the environment was woken up), False when it was already running.
    """
    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )
    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )
    _assert_team_owns(container, settings, team, env_name)
    if container.status == "running":
        return False
    start_environment(settings, env_name, team)
    return True


def _assert_team_owns(
    container: Any, settings: Settings, team: TeamSettings, env_name: str
) -> None:
    """Reject operating on a container that belongs to another team.

    Defence in depth on top of team-scoped container names (issue #39): even
    if a name unexpectedly resolves across teams, the team label must match.
    The NotFound message is reused so the existence of another team's env is
    not disclosed.
    """
    label = container.labels.get(settings.team_label)
    if label is not None and label != team.team_id:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )


def restart_environment(
    settings: Settings, env_name: str, team: TeamSettings
) -> dict[str, str]:
    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )

    try:
        odoo_container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )
    _assert_team_owns(odoo_container, settings, team, env_name)
    odoo_container.restart()

    logger.info("Environment restarted", extra={"env_name": env_name})
    return {"odoo_container": odoo_container_name}


def stop_environment(
    settings: Settings, team: TeamSettings, env_name: str
) -> dict[str, Any]:
    if is_protected(settings, team, env_name):
        raise ProtectedError(
            f"Environment '{env_name}' is protected. Unprotect it before stopping."
        )
    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )

    try:
        odoo_container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )
    _assert_team_owns(odoo_container, settings, team, env_name)
    odoo_container.stop()

    activity.mark_stopped(team, env_name, by="manual")
    logger.info("Environment stopped", extra={"env_name": env_name})
    return {"odoo_container": odoo_container_name, "stopped": [odoo_container_name]}


def start_environment(
    settings: Settings, env_name: str, team: TeamSettings
) -> dict[str, Any]:
    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )

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
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )
    _assert_team_owns(odoo_container, settings, team, env_name)
    odoo_container.start()
    started.append(odoo_container_name)

    logger.info("Environment started", extra={"env_name": env_name})
    return {"odoo_container": odoo_container_name, "started": started}


def get_env_base_url(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    container: Any = None,
) -> tuple[str, str]:
    """Return ``(base_url, cookie_domain)`` for an environment's Odoo web UI.

    ``base_url`` is scheme + host (+ published port) with no path; ``cookie_domain``
    is the bare host an Odoo ``session_id`` cookie must be scoped to. Mirrors the
    URL logic in :func:`get_environment_info` (traefik subdomain vs. published
    host port) so a single source computes both the browsable URL and the cookie
    domain — used by ``connect_as_user`` to hand back a cookie a browser will
    actually send.
    """
    if settings.routing_mode == "traefik":
        host = get_env_hostname(env_name, team.hostname)
        return f"https://{host}", host

    # Port routing: read the container's published 8069 port. Cookies are not
    # port-scoped, so the domain is just the host.
    if container is None:
        client = get_client()
        try:
            container = client.containers.get(
                get_resource_name(env_name, "odoo", settings.prefix, team.team_id)
            )
        except docker.errors.NotFound:
            raise NotFoundError(
                f"Environment '{env_name}' does not exist. Use create_environment first."
            )
    ports = container.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
    mappings = ports.get("8069/tcp") or []
    host_port = mappings[0].get("HostPort") if mappings else None
    if not host_port:
        raise NotFoundError(
            f"Environment '{env_name}' has no published HTTP port; "
            "is the environment running?"
        )
    return f"http://{team.hostname}:{host_port}", team.hostname


def get_environment_info(
    settings: Settings, team: TeamSettings, env_name: str
) -> dict[str, Any]:
    from oduflow.docker_ops.stats import _get_one_container_stats

    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )

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
        result["local_path"] = labels.get("oduflow.local_path", "")
        result["odoo_image"] = labels.get(settings.image_label, "")
        result["git_branch"] = labels.get("oduflow.git_branch", env_name)
        result["git_user"] = labels.get("oduflow.git_user", "")
        result["extra_addons"] = _normalize_extra_addons(
            json.loads(labels.get("oduflow.extra_addons", "{}")),
        )
        result["extra_addons_revisions"] = json.loads(
            labels.get("oduflow.extra_addons_revisions", "{}")
        )
        result["auto_install_modules"] = labels.get("oduflow.auto_install_modules", "")
        result["env_vars"] = json.loads(labels.get("oduflow.env_vars", "{}"))
        result["created_at"] = labels.get(
            "oduflow.created_at", ""
        ) or odoo_container.attrs.get("Created", "")
        result["note"] = _get_note_text(env_name, team.workspaces_dir)
        result["usage"] = usage.get_env_usage(team, env_name)
        result["usage_uid"] = usage.get_token(settings, team, env_name)

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


def _load_local_snapshot(
    env_name: str, team: TeamSettings
) -> dict[str, dict[str, int]]:
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


def _reapply_odoo_conf(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    container: Any,
) -> bool:
    """Reconstruct ``/etc/odoo/odoo.conf`` inside *container*.

    Resolves the base config (repo ``.oduflow/odoo.conf`` takes priority over the
    instance-level conf), regenerates the merged conf with *container*'s current
    extra-addons paths, and copies it into the container. Returns ``True`` if a
    conf was applied, ``False`` if no base config exists.

    The container reads ``/etc/odoo/odoo.conf`` as a *copy* made at create/update
    time — a plain restart reuses the stale copy, so this must run before a
    restart whenever the source ``odoo.conf`` changed.
    """
    labels = container.labels if isinstance(container.labels, dict) else {}
    if labels.get("oduflow.prod") == "true":
        # Production uses its own conf chain (odoo.prod.conf + tuned worker
        # overrides). Local import: production_ops composes env_ops helpers.
        from oduflow.docker_ops.production_ops import reapply_prod_odoo_conf

        return reapply_prod_odoo_conf(
            settings, team, labels.get("oduflow.prod_name", ""), container
        )
    repo_path = labels.get("oduflow.local_path") or get_repo_path(
        env_name, team.workspaces_dir
    )
    workspace_path = get_workspace_path(env_name, team.workspaces_dir)
    repo_odoo_conf = os.path.join(repo_path, ".oduflow", "odoo.conf")
    if os.path.isfile(repo_odoo_conf):
        base_conf_path = repo_odoo_conf
    elif _resolve_instance_conf("odoo.conf", team.data_dir).exists():
        base_conf_path = str(_resolve_instance_conf("odoo.conf", team.data_dir))
    else:
        return False

    from oduflow.extra_addons import generate_odoo_conf, resolve_main_addons_path

    extra_addons_json = (container.labels or {}).get("oduflow.extra_addons", "")
    extra_container_paths: list[str] = []
    if extra_addons_json:
        extra_dict = _normalize_extra_addons(json.loads(extra_addons_json))
        extra_container_paths = [f"/mnt/extra-addons-{rn}" for rn in extra_dict]
    generated_conf = os.path.join(workspace_path, "odoo.conf")
    main_addons_path = resolve_main_addons_path(repo_path)
    generate_odoo_conf(
        base_conf_path, generated_conf, extra_container_paths, main_addons_path
    )
    _copy_file_to_container(container, generated_conf, "/etc/odoo")
    return True


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
    config_changed: bool = False,
    deps_changed: bool = False,
    repo_path: str = "",
) -> dict[str, Any]:
    """Run install/upgrade/restart and build the result dict (shared by the
    explicit and auto paths of :func:`pull_environment`)."""
    from oduflow.docker_ops.odoo_ops import (
        install_odoo_modules,
        upgrade_odoo_modules,
    )

    # Reinstall apt/pip deps up front so a subsequent module install/upgrade can
    # import newly-added libs (same apt-then-pip order as create/update). Never
    # restart here — a single restart happens in the branches below.
    dep_logs: list[str] = []
    dep_exit = 0
    if deps_changed and repo_path:
        dep_container = client.containers.get(odoo_container_name)
        apt_log = _install_apt_packages(dep_container, repo_path)
        if apt_log:
            dep_logs.append(apt_log)
            if "FAILED" in apt_log:
                dep_exit = 1
        pip_installed, pip_log = _install_pip_requirements(
            dep_container, repo_path, restart=False
        )
        if pip_log:
            dep_logs.append(pip_log)
        if (not pip_installed) and pip_log:  # log present but not installed => FAILED
            dep_exit = 1

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
        if dep_logs:
            messages.insert(0, "Reinstalled dependencies.")
        container = client.containers.get(odoo_container_name)
        if config_changed and _reapply_odoo_conf(settings, team, env_name, container):
            messages.append("Reapplied odoo.conf.")
        container.restart()
        messages.append("Container restarted.")
        logger.info(
            "Container restarted after module update", extra={"env_name": env_name}
        )
        return {
            "action": "install" if to_install else "upgrade",
            "modules_installed": to_install,
            "modules_upgraded": to_upgrade,
            "exit_code": last_exit_code or dep_exit,
            "changed_files": changed_files,
            "message": " ".join(messages),
            "output": "\n".join(dep_logs + odoo_output_parts),
        }

    if do_restart or deps_changed:
        container = client.containers.get(odoo_container_name)
        reapplied = config_changed and _reapply_odoo_conf(
            settings, team, env_name, container
        )
        container.restart()
        logger.info("Container restarted", extra={"env_name": env_name})
        parts: list[str] = []
        if reapplied:
            parts.append("Reapplied odoo.conf.")
        if dep_logs:
            parts.append("Reinstalled dependencies.")
        elif deps_changed:
            # A dependency file changed but nothing was installed (e.g. it was
            # deleted, or only removed lines). Removed packages are not
            # uninstalled until the container is rebuilt (update_environment).
            parts.append("Dependencies changed.")
        parts.append("Container restarted.")
        return {
            "action": "restart",
            "changed_files": changed_files,
            "message": " ".join(parts),
            "output": "\n".join(dep_logs),
            "exit_code": dep_exit,
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

    * **git** (default) — ``git pull`` the branch into the managed clone. Dev
      extra-addons resolve to immutable shared SHA checkouts; production keeps
      its private mutable worktrees for independent rollback.
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
    from oduflow.git_analysis import (
        _is_active_dep_file,
        guardrail_warnings,
        merge_recommendations,
        recommend,
    )
    from oduflow.git_ops import pull_repo

    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )

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
    # Each (repo_path, base_ref, files) unit is classified against its OWN repo
    # and old HEAD; extra-addon checkouts must not be classified against the main
    # repo's tree or their changes are misread (issue #51).
    classify_units: list[tuple[str, str | None, list[str]]] = []
    pending_extra_checkouts: dict[str, str] = {}
    pending_extra_revisions: dict[str, str] = {}
    extra_mount_switch_needed = False
    if is_local:
        _trace("pull_environment(%s): live-mount, detecting local changes", env_name)
        base_ref, all_changed = _detect_local_changes(repo_path, env_name, team)
        classify_units.append((repo_path, base_ref, all_changed))
        main_changed_files = all_changed
    else:
        _trace("pull_environment(%s): git pull started", env_name)
        git_branch = container_obj.labels.get("oduflow.git_branch", env_name)
        old_head, changed_files = pull_repo(
            repo_path, git_branch, cred_file=team.git_credentials_file()
        )
        base_ref = old_head
        classify_units.append((repo_path, old_head, changed_files))

        all_changed = changed_files
        # Dependency reinstall keys off the MAIN repo only: the pip/apt install
        # helpers read only the main repo_path, never extra-addons checkouts.
        main_changed_files = changed_files

    try:
        extra_addons = _normalize_extra_addons(
            json.loads(container_obj.labels.get("oduflow.extra_addons", "{}"))
        )
    except (json.JSONDecodeError, TypeError):
        extra_addons = {}

    extra_changed_files: list[str] = []
    if extra_addons:
        is_production = container_obj.labels.get("oduflow.prod") == "true"
        if is_production:
            # Production retains one mutable worktree per deployment. Its
            # deploy log records these HEADs and rollback resets them in place.
            from oduflow.extra_addons import pull_extra_worktree

            extra_dir = os.path.join(
                get_workspace_path(env_name, team.workspaces_dir), "extra"
            )
            for repo_name, branch in extra_addons.items():
                wt_path = os.path.join(extra_dir, repo_name)
                if not os.path.isdir(wt_path):
                    continue
                extra_old, extra_files = pull_extra_worktree(
                    team, repo_name, branch, wt_path
                )
                extra_changed_files.extend(extra_files)
                if extra_files:
                    classify_units.append((wt_path, extra_old, extra_files))
        else:
            from oduflow.extra_addons import checkout_revision, ensure_shared_checkout

            try:
                stored_revisions = json.loads(
                    container_obj.labels.get("oduflow.extra_addons_revisions", "{}")
                )
            except (json.JSONDecodeError, TypeError):
                stored_revisions = {}
            if not isinstance(stored_revisions, dict):
                stored_revisions = {}
            current_sources = _extra_checkout_sources(container_obj, iter(extra_addons))
            for repo_name, branch in extra_addons.items():
                source = current_sources.get(repo_name, "")
                current_revision = str(stored_revisions.get(repo_name, ""))
                if not current_revision and source:
                    current_revision = checkout_revision(source)
                checkout = ensure_shared_checkout(
                    team,
                    repo_name,
                    branch,
                    current_revision=current_revision,
                )
                checkout_path = str(checkout["path"])
                revision = str(checkout["revision"])
                pending_extra_checkouts[repo_name] = checkout_path
                pending_extra_revisions[repo_name] = revision
                extra_mount_switch_needed = extra_mount_switch_needed or (
                    source != checkout_path or current_revision != revision
                )
                extra_files = list(checkout["changed_files"])
                extra_changed_files.extend(extra_files)
                if extra_files:
                    classify_units.append(
                        (checkout_path, current_revision or None, extra_files)
                    )

    all_changed = all_changed + extra_changed_files

    _trace(
        "pull_environment(%s): %d changed files: %s",
        env_name,
        len(all_changed),
        all_changed,
    )

    # --- 2. Determine actions: explicit (agent-driven) vs auto (classify) ---
    explicit = bool(install) or bool(upgrade) or restart
    recommended = (
        merge_recommendations(
            recommend(files, rp, ref) for rp, ref, files in classify_units if files
        )
        if all_changed
        else {"action": "none", "modules_to_install": [], "modules_to_upgrade": []}
    )
    # A changed .oduflow/odoo.conf must be reconstructed and re-copied into the
    # container before any restart (a plain restart reuses the stale copy).
    details = recommended.get("details")
    config_changed = isinstance(details, dict) and bool(details.get("restart_required"))
    # A changed dependency descriptor (requirements.txt / apt_packages.txt) in the
    # MAIN repo must reinstall apt/pip deps into the running container and restart.
    # Scoped to the main repo because the install helpers only read its repo_path;
    # applies to both explicit and auto modes (a prerequisite, not an agent action).
    deps_changed = any(_is_active_dep_file(f, repo_path) for f in main_changed_files)

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
            if extra_mount_switch_needed:
                update_environment(
                    settings,
                    team,
                    env_name,
                    extra_checkout_overrides=pending_extra_checkouts,
                    extra_revision_overrides=pending_extra_revisions,
                    pull_image=False,
                    install_dependencies=False,
                )
                _cleanup_legacy_extra_worktrees(
                    team, env_name, iter(pending_extra_checkouts)
                )
                return {
                    "action": "none",
                    "extra_addons_cache_migrated": True,
                    "message": (
                        "Already up to date. Extra-addons mounts now use the "
                        "shared immutable checkout cache."
                    ),
                }
            return {
                "action": "none",
                "message": (
                    "No local changes detected." if is_local else "Already up to date."
                ),
            }
        to_install = list(recommended.get("modules_to_install", []))
        to_upgrade = list(recommended.get("modules_to_upgrade", []))
        do_restart = recommended.get("action") == "restart"

    # --- 3. Switch immutable mounts, then execute ---
    # Do this only after strict guardrails have accepted the requested action.
    # The checkout itself was prepared off to the side and is safe to cache even
    # when strict mode blocks without touching the running container.
    if extra_mount_switch_needed:
        update_environment(
            settings,
            team,
            env_name,
            extra_checkout_overrides=pending_extra_checkouts,
            extra_revision_overrides=pending_extra_revisions,
            pull_image=False,
            install_dependencies=False,
        )
        _cleanup_legacy_extra_worktrees(team, env_name, iter(pending_extra_checkouts))

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
        config_changed=config_changed,
        deps_changed=deps_changed,
        repo_path=repo_path,
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
    extra_checkout_overrides: dict[str, str] | None = None,
    extra_revision_overrides: dict[str, str] | None = None,
    pull_image: bool = True,
    install_dependencies: bool = True,
) -> dict[str, Any]:
    """Re-create an environment's container, preserving DB, repo and filestore.

    With no overrides this simply rebuilds the container from its current image
    and configuration (useful when the container is broken). Optionally pulls a
    different ``image_override`` and/or fully replaces the user-supplied
    environment variables with ``env_override`` (an explicit dict; an empty dict
    clears them). Internal extra-checkout overrides atomically switch immutable
    SHA mounts during pull_and_apply; that path disables image pulls and the
    redundant dependency reinstall. The PostgreSQL connection variables
    (HOST/USER/PASSWORD) are always re-derived from the environment credentials,
    and image-baked env comes from the image itself.
    """
    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )

    # ------------------------------------------------------------------
    # 1. Look up existing container and extract its configuration
    # ------------------------------------------------------------------
    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )

    # Labels
    labels = dict(container.labels)

    # Image (current → target). A digest-pinned recreation leaves Config.Image
    # as sha256:..., so prefer the persisted tag before falling back to it.
    # Capture the current digest so we can report whether the running image
    # actually changed after the pull.
    try:
        current_image = container.image.tags[0]
    except (IndexError, Exception):
        current_image = (
            labels.get(settings.image_label) or container.attrs["Config"]["Image"]
        )
    odoo_image = image_override or current_image
    try:
        old_digest = container.image.id
    except Exception:
        old_digest = container.attrs.get("Image", "")
    run_image = (
        old_digest
        if not pull_image and not image_override and old_digest
        else odoo_image
    )

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

    if extra_checkout_overrides is not None:
        _replace_extra_checkout_mounts(
            volumes,
            labels,
            extra_checkout_overrides,
            extra_revision_overrides or {},
        )

    # Command
    raw_cmd = container.attrs["Config"].get("Cmd") or []
    command = " ".join(raw_cmd) if raw_cmd else None

    # Port bindings (only relevant in port mode). Only read here; a fresh
    # allocation (when switching into port mode) is deferred until just before
    # the container is recreated, so an earlier abort (e.g. image pull failure)
    # does not leak a reserved port.
    host_port: int | None = None
    port_newly_allocated = False
    if settings.routing_mode == "port":
        port_bindings = container.attrs.get("HostConfig", {}).get("PortBindings") or {}
        tcp_bindings = port_bindings.get("8069/tcp")
        if tcp_bindings:
            try:
                host_port = int(tcp_bindings[0]["HostPort"])
            except (KeyError, IndexError, ValueError, TypeError):
                pass

    # Validate the target image is available BEFORE removing the old container,
    # so a bad or unreachable image does not leave the env with no container
    # at all (#49). If the pull fails, fall back to a local copy; if there is
    # none, abort with the existing environment left untouched.
    image_updated = False
    if pull_image:
        try:
            logger.info("Pulling image %s", odoo_image)
            new_image_obj = client.images.pull(odoo_image)
            image_updated = bool(old_digest) and new_image_obj.id != old_digest
        except Exception as exc:
            logger.warning("Could not pull image %s: %s", odoo_image, exc)
            try:
                client.images.get(odoo_image)
            except docker.errors.ImageNotFound:
                raise PrerequisiteNotMetError(
                    f"Image '{odoo_image}' could not be pulled and is not available "
                    "locally; leaving the existing environment untouched."
                ) from exc

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
                _tmp_vols: dict[str, Any] = {}
                _mount_filestore(
                    client,
                    settings,
                    team,
                    env_name,
                    env_db,
                    run_image,
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

    # Recompute the Traefik routing labels from the CURRENT settings instead of
    # copying the old container's: labels are frozen at creation, so this is what
    # lets a routing-mode / TLS / hostname switch be repaired by a plain
    # update_environment. Strip every stale ``traefik.*`` key first so a
    # traefik→port switch drops them and a renamed router does not linger.
    labels = {k: v for k, v in labels.items() if not k.startswith("traefik.")}
    labels.update(build_env_traefik_labels(settings, team, env_name))
    if settings.routing_mode == "traefik":
        # No published port in traefik mode; drop any stale reservation left from
        # a previous port-mode life so the slot is reusable.
        release_port(team.port_registry_path, env_name)

    # Verify extra addons checkouts are intact. New environments use persistent
    # SHA-keyed shared paths; legacy environments keep per-workspace worktrees
    # until their next pull_and_apply migrates the mounts.
    extra_addons_json = labels.get("oduflow.extra_addons", "")
    if extra_addons_json:
        parsed = json.loads(extra_addons_json)
        extra_dict = _normalize_extra_addons(parsed)
        revisions = json.loads(labels.get("oduflow.extra_addons_revisions", "{}"))
        extra_dir = os.path.join(
            get_workspace_path(env_name, team.workspaces_dir), "extra"
        )
        for rn in extra_dict:
            revision = revisions.get(rn, "") if isinstance(revisions, dict) else ""
            wt = (
                os.path.join(team.shared_extra_checkouts_dir, rn, revision)
                if revision
                else os.path.join(extra_dir, rn)
            )
            if not os.path.isdir(wt):
                logger.warning("Extra addons checkout missing: %s", wt)

    # Switching into port mode (was traefik) or a legacy container that never
    # published a port: allocate one now, right before recreation, so any
    # earlier abort could not have leaked the reservation.
    if settings.routing_mode == "port" and host_port is None:
        used_ports = _get_used_ports(client, settings, team, exclude_env=env_name)
        host_port = allocate_port(
            team.port_registry_path,
            env_name,
            team.port_range_start,
            team.port_range_end,
            used_ports=used_ports,
        )
        port_newly_allocated = True

    # ------------------------------------------------------------------
    # 4. Re-create the container with the same settings
    # ------------------------------------------------------------------
    run_kwargs: dict[str, Any] = dict(
        image=run_image,
        name=odoo_container_name,
        detach=True,
        network=get_team_network_name(team.team_id, settings.prefix),
        extra_hosts={"host.docker.internal": "host-gateway"},
        **default_env_limits(),
        environment=env_dict,
        labels=labels,
        volumes=volumes,
        restart_policy={"Name": "unless-stopped"},
    )
    if command:
        run_kwargs["command"] = command
    if settings.routing_mode == "port" and host_port is not None:
        run_kwargs["ports"] = {"8069/tcp": host_port}

    try:
        new_container = client.containers.run(**run_kwargs)
    except Exception:
        # The old container is already gone; at least don't leak a port slot we
        # freshly reserved for this recreate.
        if port_newly_allocated:
            release_port(team.port_registry_path, env_name)
        raise

    # Regenerate and copy odoo.conf into the new container (repo .oduflow/ takes
    # priority over the instance conf; extra-addons paths merged from labels).
    repo_path = labels.get("oduflow.local_path") or get_repo_path(
        env_name, team.workspaces_dir
    )
    _reapply_odoo_conf(settings, team, env_name, new_container)

    # ------------------------------------------------------------------
    # 5. Re-install apt packages and pip requirements
    # ------------------------------------------------------------------
    setup_logs: list[str] = []
    if install_dependencies:
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
