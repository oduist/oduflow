import json
import logging
import os
import pathlib
import shutil
import subprocess
import time
from typing import Any

_TRACE = os.environ.get("ODUFLOW_TRACE") == "1"


def _trace(msg: str, *args: object) -> None:
    if _TRACE:
        logger.info("[TRACE] " + msg, *args)

import docker
from docker import DockerClient

from oduflow.docker_ops.client import get_client, get_odoo_uid_gid
from oduflow.docker_ops.system_ops import _db_exists, _exec_sql, _resolve_conf
from oduflow.errors import (
    ConflictError,
    ExternalCommandError,
    NotFoundError,
    PrerequisiteNotMetError,
    ProtectedError,
)
from oduflow.git_ops import RepoAuthError
from oduflow.naming import get_db_name, get_env_hostname, get_filestore_paths, get_repo_path, get_resource_name, get_template_db_name, get_workspace_path, sanitize_repo_url, slugify_branch
from oduflow.port_registry import allocate_port, release_port
from oduflow.settings import Settings

logger = logging.getLogger("oduflow")

_PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _normalize_extra_addons(raw_addons, fallback_branch: str) -> dict[str, str]:
    """Convert old list format or new dict format to {name: branch} dict."""
    if isinstance(raw_addons, dict):
        return raw_addons
    if isinstance(raw_addons, list):
        return {name: fallback_branch for name in raw_addons}
    return {}


def _get_used_ports(client: DockerClient, settings: Settings, exclude_branch: str = "") -> set[int]:
    """Collect host ports currently bound by managed containers (excluding a specific branch)."""
    used: set[int] = set()
    filters = {
        "label": [
            f"{settings.managed_label}=true",
            f"{settings.instance_label}={settings.instance_id}"
        ]
    }
    for c in client.containers.list(all=True, filters=filters):
        branch = c.labels.get(settings.branch_label, "")
        if branch == exclude_branch:
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


def _ensure_system_ready(client: DockerClient, settings: Settings, template_name: str | None = None) -> None:
    try:
        db_container = client.containers.get(settings.shared_db_container)
        if db_container.status != "running":
            raise PrerequisiteNotMetError(
                f"{settings.shared_db_container} is not running. Run init_system first."
            )
    except docker.errors.NotFound:
        raise PrerequisiteNotMetError(
            f"{settings.shared_db_container} not found. Run init_system first."
        )

    if template_name is not None:
        tpl_db = get_template_db_name(template_name, settings.instance_id)
        if not _db_exists(client, settings, tpl_db):
            raise PrerequisiteNotMetError(
                f"Template database '{tpl_db}' not found. Run init_template first."
            )

    if settings.routing_mode == "traefik":
        try:
            t = client.containers.get(settings.traefik_container)
            if t.status != "running":
                raise PrerequisiteNotMetError(
                    f"{settings.traefik_container} is not running. Run init_system first."
                )
        except docker.errors.NotFound:
            raise PrerequisiteNotMetError(
                f"{settings.traefik_container} not found. Run init_system first."
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
    branch_name: str,
    env_db: str,
    odoo_image: str,
    odoo_volumes: dict,
    *,
    template_name: str,
) -> None:
    template_filestore = settings.get_template_filestore_path(template_name)
    if not template_filestore or not os.path.isdir(template_filestore):
        logger.debug("Dump filestore not found at %s, skipping overlay mount", template_filestore)
        return

    paths = get_filestore_paths(branch_name, settings.workspaces_dir)

    # Read use_overlay flag from template metadata (avoids slow filestore scan)
    use_overlay = None
    metadata_path = settings.get_template_metadata_path(template_name)
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
        for root, dirs, files in os.walk(merged):
            os.chown(root, int(uid_str), int(gid_str))
            for name in dirs + files:
                os.chown(os.path.join(root, name), int(uid_str), int(gid_str))
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
        for root, dirs, files in os.walk(d):
            os.chown(root, int(uid_str), int(gid_str))
            for name in dirs + files:
                os.chown(os.path.join(root, name), int(uid_str), int(gid_str))

    if not shutil.which("fuse-overlayfs"):
        raise PrerequisiteNotMetError(
            "fuse-overlayfs is not installed. "
            "Install it with: sudo apt install fuse-overlayfs"
        )

    result = subprocess.run(
        [
            "fuse-overlayfs",
            "-o", f"lowerdir={template_filestore},upperdir={paths['upper']},workdir={paths['work']},allow_other",
            paths["merged"],
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        error_msg = result.stderr.decode("utf-8", errors="replace").strip() if result.stderr else ""
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
    logger.info("Filestore overlay mounted", extra={"branch": branch_name})


def _unmount_filestore(branch_name: str, settings: Settings) -> None:
    paths = get_filestore_paths(branch_name, settings.workspaces_dir)
    merged = paths["merged"]
    if not os.path.isdir(merged) or not os.path.ismount(merged):
        return

    for cmd in (
        ["fusermount", "-u", merged],
        ["umount", "-l", merged],
    ):
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            logger.info("Filestore overlay unmounted (%s)", cmd[-2], extra={"branch": branch_name})
            return
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue

    logger.warning("Could not unmount filestore overlay at %s", merged)


def _install_apt_packages(container, repo_path: str) -> str:
    """Install apt packages. Returns a human-readable log of what happened."""
    apt_file = os.path.join(repo_path, "apt_packages.txt")
    if not os.path.isfile(apt_file):
        logger.debug("No apt_packages.txt in repo, skipping apt install")
        return ""

    with open(apt_file) as f:
        packages = [line.strip() for line in f if line.strip() and not line.startswith("#")]
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


def _install_pip_requirements(container, repo_path: str, *, restart: bool = True) -> tuple[bool, str]:
    """Install pip requirements from repo.

    Returns (installed, log) — *installed* is True when packages were
    installed, *log* is a human-readable summary of what happened.

    When *restart* is False the caller is responsible for restarting the
    container after all setup steps are done.
    """
    req_file = os.path.join(repo_path, "requirements.txt")
    if not os.path.isfile(req_file):
        logger.debug("No requirements.txt in repo, skipping pip install")
        return False, ""

    # Ensure the odoo user can write to its local site-packages
    _ensure_user_site_packages(container)

    cmd = "pip3 install --user --break-system-packages -r /mnt/extra-addons/requirements.txt"
    logger.info("Installing pip requirements from requirements.txt")
    exit_code, output = container.exec_run(cmd, user="odoo")
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)
    if exit_code != 0 and "no such option" in output_str.lower():
        logger.info("--break-system-packages not supported, retrying without it")
        cmd = "pip3 install --user -r /mnt/extra-addons/requirements.txt"
        exit_code, output = container.exec_run(cmd, user="odoo")
        output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)
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
    branch_name: str,
) -> None:
    odoo_container_name = get_resource_name(branch_name, "odoo", settings.prefix)
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

    env_db = get_db_name(branch_name, settings.instance_id)
    if _db_exists(client, settings, env_db):
        try:
            _exec_sql(client, settings, f'DROP DATABASE IF EXISTS "{env_db}" WITH (FORCE);')
            logger.info("Dropped old database %s", env_db)
        except Exception:
            pass

    workspace_path = get_workspace_path(branch_name, settings.workspaces_dir)
    if os.path.exists(workspace_path):
        _unmount_filestore(branch_name, settings)
        shutil.rmtree(workspace_path)


def create_environment(
    settings: Settings,
    branch_name: str,
    repo_url: str,
    odoo_image: str,
    template_name: str | None = None,
    extra_addons: dict[str, str] | None = None,
) -> dict[str, str]:
    try:
        client = get_client()
    except Exception as e:
        raise PrerequisiteNotMetError(
            f"Failed to connect to Docker daemon: {e}. Ensure Docker is running."
        )

    if template_name is not None:
        tpl_db = get_template_db_name(template_name, settings.instance_id)
        if not _db_exists(client, settings, tpl_db):
            logger.warning(
                "Template DB '%s' not found, falling back to init from scratch",
                tpl_db,
            )
            template_name = None

    _ensure_system_ready(client, settings, template_name)

    odoo_container_name = get_resource_name(branch_name, "odoo", settings.prefix)
    try:
        existing = client.containers.get(odoo_container_name)
        if existing.status == "running":
            existing.reload()
            if settings.routing_mode == "traefik":
                url = f"https://{get_env_hostname(branch_name, settings.base_domain)}"
            else:
                ports = existing.ports.get("8069/tcp")
                host_port = ports[0]["HostPort"] if ports else "?"
                url = f"http://{settings.external_host}:{host_port}"
            raise ConflictError(
                f"Environment for branch '{branch_name}' already exists and is running at {url}."
            )
        raise ConflictError(
            f"Environment for branch '{branch_name}' already exists (status: {existing.status})."
        )
    except docker.errors.NotFound:
        pass

    _cleanup_old_environment(client, settings, branch_name)
    workspace_path = get_workspace_path(branch_name, settings.workspaces_dir)
    repo_path = get_repo_path(branch_name, settings.workspaces_dir)
    env_db = get_db_name(branch_name, settings.instance_id)

    labels = {
        settings.managed_label: "true",
        settings.instance_label: settings.instance_id,
        settings.branch_label: branch_name,
        settings.repo_label: repo_url,
        settings.image_label: odoo_image,
        "oduflow.template": template_name if template_name is not None else "none",
    }

    if extra_addons:
        labels["oduflow.extra_addons"] = json.dumps(extra_addons)

    if settings.routing_mode == "traefik":
        slug = slugify_branch(branch_name)
        traefik_router = f"oduflow-{slug}"
        traefik_host = get_env_hostname(branch_name, settings.base_domain)
        labels.update({
            "traefik.enable": "true",
            f"traefik.http.routers.{traefik_router}.rule": f"Host(`{traefik_host}`)",
            f"traefik.http.routers.{traefik_router}.entrypoints": "websecure",
            f"traefik.http.routers.{traefik_router}.tls": "true",
            f"traefik.http.routers.{traefik_router}.tls.certresolver": "le",
            f"traefik.http.services.{traefik_router}.loadbalancer.server.port": "8069",
        })

    logger.info(
        "Creating environment",
        extra={
            "branch": branch_name,
            "repo": sanitize_repo_url(repo_url),
            "image": odoo_image,
            "prefix": settings.prefix,
            "routing_mode": settings.routing_mode,
            "base_domain": settings.base_domain,
            "external_host": settings.external_host,
            "workspaces_dir": settings.workspaces_dir,
        },
    )

    os.makedirs(workspace_path, exist_ok=True)

    git_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    branch_created = False
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
                "git", "clone", "--branch", branch_name,
                "--depth", "1", repo_url, repo_path,
            ],
            check=True,
            capture_output=True,
            timeout=60,
            env=git_env,
        )
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.decode("utf-8") if e.stderr else str(e)
        if "branch" in error_msg.lower() and "not found" in error_msg.lower():
            logger.info(
                "Branch '%s' not found on remote, cloning latest '%s' and creating branch",
                branch_name, settings.default_branch,
            )
            try:
                subprocess.run(
                    [
                        "git", "clone", "--branch", settings.default_branch,
                        "--depth", "1", repo_url, repo_path,
                    ],
                    check=True,
                    capture_output=True,
                    timeout=60,
                    env=git_env,
                )
            except subprocess.CalledProcessError as e2:
                error_msg2 = e2.stderr.decode("utf-8") if e2.stderr else str(e2)
                if any(kw.lower() in error_msg2.lower() for kw in auth_keywords):
                    raise RepoAuthError(
                        f"Git authentication failed for {repo_url}. "
                        f"Call 'setup_repo_auth' first with URL in format "
                        f"https://user:PAT@github.com/owner/repo.git to cache credentials."
                    )
                raise ExternalCommandError("git clone", e2.returncode, error_msg2)
            except subprocess.TimeoutExpired:
                raise ExternalCommandError(
                    "git clone", -1,
                    "Repository clone timed out (60s). Repository may be too large or network is slow.",
                )
            subprocess.run(
                ["git", "checkout", "-b", branch_name],
                check=True,
                capture_output=True,
                cwd=repo_path,
                env=git_env,
            )
            branch_created = True
        else:
            if any(kw.lower() in error_msg.lower() for kw in auth_keywords):
                raise RepoAuthError(
                    f"Git authentication failed for {repo_url}. "
                    f"Call 'setup_repo_auth' first with URL in format "
                    f"https://user:PAT@github.com/owner/repo.git to cache credentials."
                )
            raise ExternalCommandError("git clone", e.returncode, error_msg)
    except subprocess.TimeoutExpired:
        raise ExternalCommandError(
            "git clone", -1,
            "Repository clone timed out (60s). Repository may be too large or network is slow.",
        )

    # --- Extra addons worktrees ---
    extra_mount_paths = []
    if extra_addons:
        from oduflow.extra_addons import create_worktree
        extra_dir = os.path.join(workspace_path, "extra")
        os.makedirs(extra_dir, exist_ok=True)
        for repo_name, branch in extra_addons.items():
            wt_path = os.path.join(extra_dir, repo_name)
            create_worktree(settings, repo_name, branch, wt_path)
            container_path = f"/mnt/extra-addons-{repo_name}"
            extra_mount_paths.append((wt_path, container_path))

    if template_name is not None:
        tpl_db = get_template_db_name(template_name, settings.instance_id)
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

    odoo_env = {
        "HOST": settings.shared_db_container,
        "USER": settings.db_user,
        "PASSWORD": settings.db_password,
    }
    odoo_volumes = {repo_path: {"bind": "/mnt/extra-addons", "mode": "rw"}}

    for host_path, container_path in extra_mount_paths:
        odoo_volumes[host_path] = {"bind": container_path, "mode": "ro"}

    repo_odoo_conf = os.path.join(repo_path, "odoo.conf")
    if os.path.isfile(repo_odoo_conf):
        base_conf_path = repo_odoo_conf
        logger.info("Using odoo.conf from repository")
    elif _resolve_conf("odoo.conf").exists():
        base_conf_path = str(_resolve_conf("odoo.conf"))
    else:
        base_conf_path = None

    if base_conf_path:
        if extra_mount_paths:
            from oduflow.extra_addons import generate_odoo_conf
            generated_conf = os.path.join(workspace_path, "odoo.conf")
            extra_container_paths = [cp for _, cp in extra_mount_paths]
            generate_odoo_conf(base_conf_path, generated_conf, extra_container_paths)
            odoo_conf_to_mount = generated_conf
        else:
            odoo_conf_to_mount = base_conf_path
        odoo_volumes[odoo_conf_to_mount] = {
            "bind": "/etc/odoo/odoo.conf",
            "mode": "ro",
        }

    if template_name is not None:
        _mount_filestore(client, settings, branch_name, env_db, odoo_image, odoo_volumes, template_name=template_name)
    else:
        # No template — create a plain filestore directory on the host so data
        # survives container restarts (no overlay needed).
        filestore_path = os.path.join(workspace_path, "filestore")
        os.makedirs(filestore_path, mode=0o777, exist_ok=True)
        os.chmod(filestore_path, 0o777)
        _uid, _gid = get_odoo_uid_gid(client, odoo_image).split(":")
        for _root, _dirs, _files in os.walk(filestore_path):
            os.chown(_root, int(_uid), int(_gid))
            for _name in _dirs + _files:
                os.chown(os.path.join(_root, _name), int(_uid), int(_gid))
        odoo_volumes[filestore_path] = {
            "bind": f"/var/lib/odoo/.local/share/Odoo/filestore/{env_db}",
            "mode": "rw",
        }

    host_port: int | None = None
    if settings.routing_mode == "port":
        used_ports = _get_used_ports(client, settings, exclude_branch=branch_name)
        host_port = allocate_port(
            settings.port_registry_path,
            branch_name,
            settings.port_range_start,
            settings.port_range_end,
            used_ports=used_ports,
        )

    sessions_path = os.path.join(workspace_path, "sessions")
    os.makedirs(sessions_path, mode=0o777, exist_ok=True)
    os.chmod(sessions_path, 0o777)
    uid_str, gid_str = get_odoo_uid_gid(client, odoo_image).split(":")
    for root_dir, dirs, files in os.walk(sessions_path):
        os.chown(root_dir, int(uid_str), int(gid_str))
        for name in dirs + files:
            os.chown(os.path.join(root_dir, name), int(uid_str), int(gid_str))
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

    container = client.containers.run(**run_kwargs)

    setup_logs: list[str] = []

    if template_name is None:
        logger.info("No template — initialising Odoo with -i base", extra={"branch": branch_name})
        apt_log = _install_apt_packages(container, repo_path)
        if apt_log:
            setup_logs.append(apt_log)
        _, pip_log = _install_pip_requirements(container, repo_path, restart=False)
        if pip_log:
            setup_logs.append(pip_log)
        init_cmd = f"/entrypoint.sh odoo -d {env_db} -i base --stop-after-init --no-http"
        exit_code, output = container.exec_run(init_cmd)
        output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)
        if exit_code != 0:
            logger.error(
                "Odoo base init failed (exit %d): %s", exit_code, output_str,
                extra={"branch": branch_name},
            )
            setup_logs.append(f"[INIT] odoo -i base FAILED (exit {exit_code}):\n{output_str}")
        else:
            setup_logs.append("[INIT] odoo -i base completed successfully")
        container.restart()
        logger.info("Container restarted after base init", extra={"branch": branch_name})
    else:
        apt_log = _install_apt_packages(container, repo_path)
        if apt_log:
            setup_logs.append(apt_log)
        _, pip_log = _install_pip_requirements(container, repo_path)
        if pip_log:
            setup_logs.append(pip_log)

    if settings.routing_mode == "traefik":
        url = f"https://{get_env_hostname(branch_name, settings.base_domain)}"
    else:
        url = f"http://{settings.external_host}:{host_port}"
    logger.info(
        "Environment created",
        extra={"branch": branch_name, "url": url, "container": odoo_container_name},
    )

    result = {
        "url": url,
        "odoo_container": odoo_container_name,
        "database": env_db,
        "workspace": workspace_path,
        "setup_logs": setup_logs,
    }
    result["extra_addons"] = extra_addons or {}
    if branch_created:
        result["branch_created_from"] = settings.default_branch
    return result


def is_protected(settings: Settings, branch_name: str) -> bool:
    workspace_path = get_workspace_path(branch_name, settings.workspaces_dir)
    return os.path.exists(os.path.join(workspace_path, ".protected"))


def protect_environment(settings: Settings, branch_name: str) -> dict[str, Any]:
    """Mark environment as protected by creating .protected marker file."""
    client = get_client()
    container_name = get_resource_name(branch_name, "odoo", settings.prefix)
    try:
        client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Environment '{branch_name}' does not exist.")
    workspace_path = get_workspace_path(branch_name, settings.workspaces_dir)
    marker = os.path.join(workspace_path, ".protected")
    open(marker, "w").close()
    logger.info("Environment protected", extra={"branch": branch_name})
    return {"branch": branch_name, "protected": True}


def unprotect_environment(settings: Settings, branch_name: str) -> dict[str, Any]:
    """Remove protection from environment by deleting .protected marker file."""
    client = get_client()
    container_name = get_resource_name(branch_name, "odoo", settings.prefix)
    try:
        client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Environment '{branch_name}' does not exist.")
    workspace_path = get_workspace_path(branch_name, settings.workspaces_dir)
    marker = os.path.join(workspace_path, ".protected")
    if os.path.exists(marker):
        os.remove(marker)
    logger.info("Environment unprotected", extra={"branch": branch_name})
    return {"branch": branch_name, "protected": False}


def delete_environment(settings: Settings, branch_name: str) -> None:
    if is_protected(settings, branch_name):
        raise ProtectedError(f"Environment '{branch_name}' is protected. Unprotect it before deleting.")
    client = get_client()
    odoo_container_name = get_resource_name(branch_name, "odoo", settings.prefix)
    env_db = get_db_name(branch_name, settings.instance_id)

    logger.info("Deleting environment", extra={"branch": branch_name})

    if settings.routing_mode == "port":
        release_port(settings.port_registry_path, branch_name)

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
    except Exception:
        pass

    workspace_path = get_workspace_path(branch_name, settings.workspaces_dir)
    if os.path.exists(workspace_path):
        _unmount_filestore(branch_name, settings)
        extra_dir = os.path.join(workspace_path, "extra")
        if os.path.isdir(extra_dir):
            from oduflow.extra_addons import remove_worktree
            for repo_name in os.listdir(extra_dir):
                wt_path = os.path.join(extra_dir, repo_name)
                if os.path.isdir(wt_path):
                    remove_worktree(settings, repo_name, wt_path)
        shutil.rmtree(workspace_path)

    logger.info("Environment deleted", extra={"branch": branch_name})


def list_environments(settings: Settings) -> list[dict[str, Any]]:
    client = get_client()
    filters = {
        "label": [
            f"{settings.managed_label}=true",
            f"{settings.instance_label}={settings.instance_id}"
        ]
    }
    containers = client.containers.list(all=True, filters=filters)

    envs: dict[str, dict[str, Any]] = {}
    for container in containers:
        branch = container.labels.get(settings.branch_label)
        if not branch:
            continue

        if branch not in envs:
            envs[branch] = {
                "branch": branch,
                "containers": [],
                "status": "running",
                "url": None,
                "odoo_image": container.labels.get(settings.image_label, ""),
                "repo_url": sanitize_repo_url(container.labels.get(settings.repo_label, "")),
                "template_name": container.labels.get("oduflow.template", "default"),
                "extra_addons": _normalize_extra_addons(
                    json.loads(container.labels.get("oduflow.extra_addons", "{}")),
                    settings.default_branch,
                ),
                "db_name": get_db_name(branch, settings.instance_id),
                "protected": is_protected(settings, branch),
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
                envs[branch]["url"] = f"https://{get_env_hostname(branch, settings.base_domain)}"
            else:
                ports = container.attrs.get("NetworkSettings", {}).get("Ports", {})
                if ports:
                    mappings = ports.get("8069/tcp")
                    if mappings:
                        host_port = mappings[0].get("HostPort")
                        if host_port:
                            envs[branch]["url"] = f"http://{settings.external_host}:{host_port}"

        envs[branch]["containers"].append(container_info)

        if container.status != "running":
            envs[branch]["status"] = "partial"

    return list(envs.values())


def restart_environment(settings: Settings, branch_name: str) -> dict[str, str]:
    client = get_client()
    odoo_container_name = get_resource_name(branch_name, "odoo", settings.prefix)

    try:
        odoo_container = client.containers.get(odoo_container_name)
        odoo_container.restart()
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{branch_name}' does not exist. Use create_environment first."
        )

    logger.info("Environment restarted", extra={"branch": branch_name})
    return {"odoo_container": odoo_container_name}


def stop_environment(settings: Settings, branch_name: str) -> dict[str, str]:
    if is_protected(settings, branch_name):
        raise ProtectedError(f"Environment '{branch_name}' is protected. Unprotect it before stopping.")
    client = get_client()
    odoo_container_name = get_resource_name(branch_name, "odoo", settings.prefix)

    try:
        odoo_container = client.containers.get(odoo_container_name)
        odoo_container.stop()
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{branch_name}' does not exist. Use create_environment first."
        )

    logger.info("Environment stopped", extra={"branch": branch_name})
    return {"odoo_container": odoo_container_name, "stopped": [odoo_container_name]}


def start_environment(settings: Settings, branch_name: str) -> dict[str, str]:
    client = get_client()
    odoo_container_name = get_resource_name(branch_name, "odoo", settings.prefix)

    try:
        db_container = client.containers.get(settings.shared_db_container)
        if db_container.status != "running":
            db_container.start()
    except docker.errors.NotFound:
        raise PrerequisiteNotMetError(
            f"{settings.shared_db_container} not found. Run init_system first."
        )

    started = [settings.shared_db_container]

    try:
        odoo_container = client.containers.get(odoo_container_name)
        odoo_container.start()
        started.append(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{branch_name}' does not exist. Use create_environment first."
        )

    logger.info("Environment started", extra={"branch": branch_name})
    return {"odoo_container": odoo_container_name, "started": started}


def get_environment_status(settings: Settings, branch_name: str) -> dict[str, Any]:
    from oduflow.docker_ops.stats import _get_one_container_stats

    client = get_client()
    odoo_container_name = get_resource_name(branch_name, "odoo", settings.prefix)

    result: dict[str, Any] = {
        "branch": branch_name,
        "odoo": {"name": odoo_container_name, "running": False, "status": "not found"},
        "db": {"name": settings.shared_db_container, "running": False, "status": "not found"},
    }

    try:
        odoo_container = client.containers.get(odoo_container_name)
        result["odoo"]["status"] = odoo_container.status
        result["odoo"]["running"] = odoo_container.status == "running"
        result["template_name"] = odoo_container.labels.get("oduflow.template", "none")
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

    result["all_running"] = result["odoo"]["running"] and result["db"]["running"]
    return result


def pull_environment(settings: Settings, branch_name: str) -> dict[str, Any]:
    from oduflow.git_analysis import classify_changes
    from oduflow.git_ops import pull_repo

    client = get_client()
    odoo_container_name = get_resource_name(branch_name, "odoo", settings.prefix)
    repo_path = get_repo_path(branch_name, settings.workspaces_dir)

    if not os.path.isdir(repo_path):
        raise NotFoundError(f"Repository for branch '{branch_name}' not found at {repo_path}")

    try:
        client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{branch_name}' does not exist. Use create_environment first."
        )

    _trace("pull_environment(%s): git pull started", branch_name)
    changed_files = pull_repo(repo_path, branch_name)
    if not changed_files:
        _trace("pull_environment(%s): no changes, already up to date", branch_name)
        return {"action": "none", "message": "Already up to date."}

    _trace("pull_environment(%s): %d files changed: %s", branch_name, len(changed_files), changed_files)

    analysis = classify_changes(changed_files, repo_path)
    action = analysis["action"]
    _trace("pull_environment(%s): classify result action=%s", branch_name, action)

    if action in ("install", "upgrade"):
        from oduflow.docker_ops.odoo_ops import install_odoo_modules, upgrade_odoo_modules

        to_install = analysis["modules_to_install"]
        to_upgrade = analysis["modules_to_upgrade"]
        messages = []
        last_exit_code = 0
        odoo_output_parts: list[str] = []

        if to_install:
            _trace("pull_environment(%s): installing modules %s", branch_name, to_install)
            res = install_odoo_modules(settings, branch_name, *to_install)
            last_exit_code = res["exit_code"]
            _trace("pull_environment(%s): install exit_code=%d", branch_name, last_exit_code)
            messages.append(f"Installed modules: {','.join(to_install)}")
            if res.get("output"):
                odoo_output_parts.append(res["output"])

        if to_upgrade:
            _trace("pull_environment(%s): upgrading modules %s", branch_name, to_upgrade)
            res = upgrade_odoo_modules(settings, branch_name, *to_upgrade)
            last_exit_code = res["exit_code"]
            _trace("pull_environment(%s): upgrade exit_code=%d", branch_name, last_exit_code)
            messages.append(f"Upgraded modules: {','.join(to_upgrade)}")
            if res.get("output"):
                odoo_output_parts.append(res["output"])

        container = client.containers.get(odoo_container_name)
        container.restart()
        _trace("pull_environment(%s): container RESTARTED after install/upgrade", branch_name)
        logger.info("Container restarted after module update", extra={"branch": branch_name})
        messages.append("Container restarted.")
        return {
            "action": action,
            "modules_installed": to_install,
            "modules_upgraded": to_upgrade,
            "exit_code": last_exit_code,
            "changed_files": changed_files,
            "message": " ".join(messages),
            "output": "\n".join(odoo_output_parts),
        }

    if action == "restart":
        container = client.containers.get(odoo_container_name)
        container.restart()
        _trace("pull_environment(%s): container RESTARTED (Python files changed)", branch_name)
        logger.info("Container restarted after pull", extra={"branch": branch_name})
        return {
            "action": "restart",
            "changed_files": changed_files,
            "message": "Container restarted (Python files changed).",
        }

    _trace("pull_environment(%s): NO restart, action=refresh (hot-reload only)", branch_name)
    return {
        "action": "refresh",
        "changed_files": changed_files,
        "message": "Only XML/JS changes detected. Refresh your browser (--dev=xml is active).",
    }


def rebuild_environment(settings: Settings, branch_name: str) -> dict[str, str]:
    """Rebuild an environment by re-creating its container while preserving DB, repo and filestore."""
    client = get_client()
    odoo_container_name = get_resource_name(branch_name, "odoo", settings.prefix)

    # ------------------------------------------------------------------
    # 1. Look up existing container and extract its configuration
    # ------------------------------------------------------------------
    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{branch_name}' does not exist. Use create_environment first."
        )

    # Image
    try:
        odoo_image = container.image.tags[0]
    except (IndexError, Exception):
        odoo_image = container.attrs["Config"]["Image"]

    # Labels
    labels = dict(container.labels)

    # Environment – parse "KEY=VALUE" list into a dict
    raw_env = container.attrs["Config"].get("Env") or []
    env_dict: dict[str, str] = {}
    for entry in raw_env:
        key, _, value = entry.partition("=")
        env_dict[key] = value

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
        "Rebuilding environment – stopping old container",
        extra={"branch": branch_name, "container": odoo_container_name},
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
    fs_paths = get_filestore_paths(branch_name, settings.workspaces_dir)
    merged = fs_paths["merged"]
    has_overlay_dirs = os.path.isdir(fs_paths["upper"])
    if has_overlay_dirs and os.path.isdir(merged) and not os.path.ismount(merged):
        env_db = get_db_name(branch_name)
        template_name = labels.get("oduflow.template", "none")
        if template_name and template_name != "none":
            try:
                _tmp_vols: dict = {}
                _mount_filestore(
                    client,
                    settings,
                    branch_name,
                    env_db,
                    odoo_image,
                    _tmp_vols,
                    template_name=template_name,
                )
            except Exception as exc:
                logger.warning("Could not re-mount filestore overlay: %s", exc)

    # Verify extra addons worktrees are intact
    extra_addons_json = labels.get("oduflow.extra_addons", "")
    if extra_addons_json:
        parsed = json.loads(extra_addons_json)
        extra_dict = _normalize_extra_addons(parsed, settings.default_branch)
        extra_dir = os.path.join(get_workspace_path(branch_name, settings.workspaces_dir), "extra")
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

    new_container = client.containers.run(**run_kwargs)

    # ------------------------------------------------------------------
    # 5. Re-install apt packages and pip requirements
    # ------------------------------------------------------------------
    repo_path = get_repo_path(branch_name, settings.workspaces_dir)
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
        url = f"https://{get_env_hostname(branch_name, settings.base_domain)}"
    else:
        url = f"http://{settings.external_host}:{host_port}"

    env_db = get_db_name(branch_name)
    workspace = get_workspace_path(branch_name, settings.workspaces_dir)
    logger.info(
        "Environment rebuilt",
        extra={"branch": branch_name, "url": url, "container": odoo_container_name},
    )

    return {
        "url": url,
        "odoo_container": odoo_container_name,
        "database": env_db,
        "workspace": workspace,
        "setup_logs": setup_logs,
    }
