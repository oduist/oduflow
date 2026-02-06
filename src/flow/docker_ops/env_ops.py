import logging
import os
import pathlib
import shutil
import subprocess
import time
from typing import Any

import docker
from docker import DockerClient

from flow.docker_ops.client import get_client
from flow.docker_ops.system_ops import _db_exists, _exec_sql
from flow.errors import (
    ExternalCommandError,
    NotFoundError,
    PrerequisiteNotMetError,
)
from flow.git_ops import RepoAuthError
from flow.naming import get_db_name, get_filestore_paths, get_repo_path, get_resource_name, get_workspace_path
from flow.settings import Settings

logger = logging.getLogger("flow")

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
_ODOO_CONF_TEMPLATE = _PROJECT_ROOT / "templates" / "odoo.conf"


def _ensure_system_ready(client: DockerClient, settings: Settings) -> None:
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

    if not _db_exists(client, settings, settings.template_db_name):
        raise PrerequisiteNotMetError(
            f"Template database '{settings.template_db_name}' not found. Run init_system first."
        )


def _mount_filestore(
    settings: Settings,
    branch_name: str,
    env_db: str,
    odoo_volumes: dict,
) -> None:
    ref = settings.ref_filestore_path
    if not ref or not os.path.isdir(ref):
        logger.debug("Reference filestore not found at %s, skipping overlay mount", ref)
        return

    paths = get_filestore_paths(branch_name, settings.workspaces_dir)
    for d in (paths["upper"], paths["work"], paths["merged"]):
        os.makedirs(d, exist_ok=True)

    try:
        subprocess.run(
            [
                "fuse-overlayfs",
                "-o", f"lowerdir={ref},upperdir={paths['upper']},workdir={paths['work']},allow_other",
                paths["merged"],
            ],
            check=True,
            capture_output=True,
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

        odoo_volumes[paths["merged"]] = {
            "bind": f"/var/lib/odoo/.local/share/Odoo/filestore/{env_db}",
            "mode": "rw",
        }
        logger.info("Filestore overlay mounted", extra={"branch": branch_name})
    except FileNotFoundError:
        logger.warning("fuse-overlayfs not installed, skipping filestore mount")
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.decode("utf-8") if e.stderr else str(e)
        logger.warning("Failed to mount filestore overlay: %s", error_msg)


def _unmount_filestore(branch_name: str, settings: Settings) -> None:
    paths = get_filestore_paths(branch_name, settings.workspaces_dir)
    merged = paths["merged"]
    if not os.path.isdir(merged):
        return

    for cmd in (["fusermount", "-u", merged], ["umount", "-l", merged]):
        try:
            subprocess.run(cmd, check=True, capture_output=True)
            logger.info("Filestore overlay unmounted (%s)", cmd[0], extra={"branch": branch_name})
            return
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue

    logger.warning("Could not unmount filestore overlay at %s", merged)


def _install_apt_packages(container, repo_path: str) -> None:
    apt_file = os.path.join(repo_path, "apt_packages.txt")
    if not os.path.isfile(apt_file):
        logger.debug("No apt_packages.txt in repo, skipping apt install")
        return

    with open(apt_file) as f:
        packages = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    if not packages:
        return

    logger.info("Updating apt and installing packages: %s", " ".join(packages))
    exit_code, output = container.exec_run("apt-get update", user="root")
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)
    if exit_code != 0:
        logger.warning("apt-get update failed (exit %d): %s", exit_code, output_str)
        return

    cmd = "apt-get install -y " + " ".join(packages)
    exit_code, output = container.exec_run(cmd, user="root")
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)
    if exit_code != 0:
        logger.warning("apt install failed (exit %d): %s", exit_code, output_str)
    else:
        logger.info("apt packages installed")


def _install_pip_requirements(container, repo_path: str, version: str = "15.0") -> None:
    req_file = os.path.join(repo_path, "requirements.txt")
    if not os.path.isfile(req_file):
        logger.debug("No requirements.txt in repo, skipping pip install")
        return

    major = float(version.split(".")[0])
    extra = " --break-system-packages" if major >= 17 else ""
    cmd = f"pip3 install{extra} -r /mnt/extra-addons/requirements.txt"
    logger.info("Installing pip requirements from requirements.txt")
    exit_code, output = container.exec_run(cmd, user="root")
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)
    if exit_code != 0:
        logger.warning("pip install failed (exit %d): %s", exit_code, output_str)
    else:
        logger.info("pip requirements installed")
        container.restart()
        logger.info("Container restarted after pip install")


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

    env_db = get_db_name(branch_name)
    if _db_exists(client, settings, env_db):
        try:
            _exec_sql(client, settings, f'DROP DATABASE IF EXISTS "{env_db}" WITH (FORCE);')
            logger.info("Dropped old database %s", env_db)
        except Exception:
            pass

    workspace_path = get_workspace_path(branch_name, settings.workspaces_dir)
    if os.path.exists(workspace_path):
        _unmount_filestore(branch_name, settings)
        shutil.rmtree(workspace_path, ignore_errors=True)


def create_environment(
    settings: Settings,
    branch_name: str,
    repo_url: str,
    version: str = "15.0",
) -> dict[str, str]:
    try:
        client = get_client()
    except Exception as e:
        raise PrerequisiteNotMetError(
            f"Failed to connect to Docker daemon: {e}. Ensure Docker is running."
        )

    _ensure_system_ready(client, settings)

    _cleanup_old_environment(client, settings, branch_name)

    odoo_container_name = get_resource_name(branch_name, "odoo", settings.prefix)
    workspace_path = get_workspace_path(branch_name, settings.workspaces_dir)
    repo_path = get_repo_path(branch_name, settings.workspaces_dir)
    env_db = get_db_name(branch_name)

    labels = {settings.managed_label: "true", settings.branch_label: branch_name}

    logger.info(
        "Creating environment",
        extra={"branch": branch_name, "repo": repo_url, "version": version},
    )

    os.makedirs(workspace_path, exist_ok=True)

    git_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

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
            raise NotFoundError(
                f"Branch '{branch_name}' not found in repository {repo_url}"
            )
        auth_keywords = (
            "Authentication failed",
            "could not read Username",
            "Permission denied",
            "Repository not found",
            "terminal prompts disabled",
            "Invalid username or password",
        )
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

    _exec_sql(
        client,
        settings,
        f'CREATE DATABASE "{env_db}" TEMPLATE {settings.template_db_name};',
    )

    odoo_image = f"{settings.odoo_image}:{version}"

    odoo_env = {
        "HOST": settings.shared_db_container,
        "USER": settings.db_user,
        "PASSWORD": settings.db_password,
    }
    odoo_volumes = {repo_path: {"bind": "/mnt/extra-addons", "mode": "rw"}}
    if _ODOO_CONF_TEMPLATE.exists():
        odoo_volumes[str(_ODOO_CONF_TEMPLATE)] = {
            "bind": "/etc/odoo/odoo.conf",
            "mode": "ro",
        }

    _mount_filestore(settings, branch_name, env_db, odoo_volumes)

    sessions_path = os.path.join(workspace_path, "sessions")
    os.makedirs(sessions_path, mode=0o777, exist_ok=True)
    os.chmod(sessions_path, 0o777)
    odoo_volumes[sessions_path] = {
        "bind": "/var/lib/odoo/.local/share/Odoo/sessions",
        "mode": "rw",
    }

    container = client.containers.run(
        odoo_image,
        name=odoo_container_name,
        detach=True,
        network=settings.shared_network,
        environment=odoo_env,
        labels=labels,
        ports={"8069/tcp": None},
        volumes=odoo_volumes,
        restart_policy={"Name": "unless-stopped"},
        command=f"odoo -d {env_db} --dev=xml",
    )

    _install_apt_packages(container, repo_path)
    _install_pip_requirements(container, repo_path, version)

    container.reload()
    host_port = container.ports["8069/tcp"][0]["HostPort"]

    url = f"http://{settings.external_host}:{host_port}"
    logger.info(
        "Environment created",
        extra={"branch": branch_name, "url": url, "container": odoo_container_name},
    )

    return {
        "url": url,
        "odoo_container": odoo_container_name,
        "database": env_db,
        "workspace": workspace_path,
    }


def delete_environment(settings: Settings, branch_name: str) -> None:
    client = get_client()
    odoo_container_name = get_resource_name(branch_name, "odoo", settings.prefix)
    env_db = get_db_name(branch_name)

    logger.info("Deleting environment", extra={"branch": branch_name})

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
        shutil.rmtree(workspace_path)

    logger.info("Environment deleted", extra={"branch": branch_name})


def list_environments(settings: Settings) -> list[dict[str, Any]]:
    client = get_client()
    filters = {"label": [settings.managed_label]}
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
            }

        container_info = {
            "name": container.name,
            "status": container.status,
            "image": container.image.tags[0] if container.image.tags else "unknown",
        }

        if "-odoo" in container.name:
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
        raise NotFoundError(f"Odoo container for branch {branch_name} not found.")

    logger.info("Environment restarted", extra={"branch": branch_name})
    return {"odoo_container": odoo_container_name}


def stop_environment(settings: Settings, branch_name: str) -> dict[str, str]:
    client = get_client()
    odoo_container_name = get_resource_name(branch_name, "odoo", settings.prefix)

    try:
        odoo_container = client.containers.get(odoo_container_name)
        odoo_container.stop()
    except docker.errors.NotFound:
        raise NotFoundError(f"Odoo container for branch {branch_name} not found.")

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
        raise NotFoundError(f"Odoo container for branch {branch_name} not found.")

    logger.info("Environment started", extra={"branch": branch_name})
    return {"odoo_container": odoo_container_name, "started": started}


def get_environment_status(settings: Settings, branch_name: str) -> dict[str, Any]:
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
    except docker.errors.NotFound:
        pass

    try:
        db_container = client.containers.get(settings.shared_db_container)
        result["db"]["status"] = db_container.status
        result["db"]["running"] = db_container.status == "running"
    except docker.errors.NotFound:
        pass

    result["all_running"] = result["odoo"]["running"] and result["db"]["running"]
    return result
