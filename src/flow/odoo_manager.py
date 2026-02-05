import docker
import os
import subprocess
import shutil
import socket
import time
from typing import List, Dict, Any
from docker import DockerClient
from flow.config import (
    PREFIX,
    BRANCH_LABEL,
    MANAGED_LABEL,
    DEFAULT_ODOO_IMAGE,
    DEFAULT_POSTGRES_IMAGE,
    ODOO_DB_USER,
    ODOO_DB_PASSWORD,
    WORKSPACES_DIR,
    EXTERNAL_HOST,
    PORT_RANGE_START,
    PORT_RANGE_END,
    MODULE_OPERATION_DELAY,
    MODULE_OPERATION_LOG_LINES
)

def get_client() -> DockerClient:
    return docker.from_env()

def _get_resource_name(branch_name: str, resource_type: str) -> str:
    return f"{PREFIX}{branch_name.replace('/', '-')}-{resource_type}"

def _get_workspace_path(branch_name: str) -> str:
    return os.path.join(WORKSPACES_DIR, branch_name.replace('/', '-'))

def _get_available_port() -> int:
    for port in range(PORT_RANGE_START, PORT_RANGE_END + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('0.0.0.0', port))
                return port
            except OSError:
                continue
    raise Exception(f"No available ports in range {PORT_RANGE_START}-{PORT_RANGE_END}")

def _wait_for_container(client: DockerClient, container_name: str, timeout: int = 30) -> None:
    for _ in range(timeout):
        try:
            container = client.containers.get(container_name)
            if container.status == "running":
                return
        except docker.errors.NotFound:
            pass
        time.sleep(1)
    raise Exception(f"Container {container_name} did not start within {timeout}s")

def _init_odoo_database(client: DockerClient, odoo_container_name: str) -> None:
    _wait_for_container(client, odoo_container_name)
    container = client.containers.get(odoo_container_name)
    cmd = "/entrypoint.sh odoo -d odoo --no-http --stop-after-init -i base"
    exit_code, output = container.exec_run(cmd)
    if exit_code != 0:
        output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)
        raise Exception(f"Database initialization failed (exit code {exit_code}): {output_str}")

def create_environment(branch_name: str, repo_url: str, version: str = "15.0") -> Dict[str, str]:
    try:
        client = get_client()
    except Exception as e:
        raise Exception(f"Failed to connect to Docker daemon: {str(e)}. Ensure Docker is running.")

    network_name = _get_resource_name(branch_name, "net")
    db_container_name = _get_resource_name(branch_name, "db")
    odoo_container_name = _get_resource_name(branch_name, "odoo")
    workspace_path = _get_workspace_path(branch_name)

    labels = {
        MANAGED_LABEL: "true",
        BRANCH_LABEL: branch_name
    }

    # 1. Prepare Workspace and Clone Repo
    if os.path.exists(workspace_path):
        shutil.rmtree(workspace_path)
    os.makedirs(workspace_path, exist_ok=True)

    # Clone and checkout branch
    try:
        subprocess.run(
            ["git", "clone", "--branch", branch_name, "--depth", "1", repo_url, workspace_path],
            check=True,
            capture_output=True,
            timeout=60
        )
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.decode('utf-8') if e.stderr else str(e)
        if "Permission denied" in error_msg or "not found" in error_msg:
            raise Exception(f"Failed to clone repository: Invalid credentials or repository not found. Please check your SSH keys and repository URL.")
        elif "Repository not found" in error_msg:
            raise Exception(f"Repository not found: {repo_url}")
        elif "branch" in error_msg.lower() and "not found" in error_msg.lower():
            raise Exception(f"Branch '{branch_name}' not found in repository {repo_url}")
        else:
            raise Exception(f"Failed to clone repository: {error_msg}")
    except subprocess.TimeoutExpired:
        raise Exception(f"Repository clone timed out (60s). Repository may be too large or network is slow.")

    # 2. Create Network
    try:
        client.networks.get(network_name)
    except docker.errors.NotFound:
        client.networks.create(network_name, labels=labels)

    # 3. Start PostgreSQL
    db_image = DEFAULT_POSTGRES_IMAGE
    client.containers.run(
        db_image,
        name=db_container_name,
        detach=True,
        network=network_name,
        environment={
            "POSTGRES_USER": ODOO_DB_USER,
            "POSTGRES_PASSWORD": ODOO_DB_PASSWORD
        },
        labels=labels,
        restart_policy={"Name": "unless-stopped"}
    )

    # 4. Start Odoo
    odoo_image = f"{DEFAULT_ODOO_IMAGE}:{version}"
    host_port = _get_available_port()

    client.containers.run(
        odoo_image,
        name=odoo_container_name,
        detach=True,
        network=network_name,
        environment={
            "HOST": db_container_name,
            "USER": ODOO_DB_USER,
            "PASSWORD": ODOO_DB_PASSWORD
        },
        labels=labels,
        ports={'8069/tcp': host_port},
        volumes={
            workspace_path: {'bind': '/mnt/extra-addons', 'mode': 'rw'}
        },
        restart_policy={"Name": "unless-stopped"},
        command="odoo -d odoo --dev=xml"
    )

    _init_odoo_database(client, odoo_container_name)

    return {
        "url": f"http://{EXTERNAL_HOST}:{host_port}",
        "odoo_container": odoo_container_name,
        "db_container": db_container_name,
        "network": network_name,
        "workspace": workspace_path
    }

def delete_environment(branch_name: str) -> None:
    client = get_client()
    # Find resources by label
    filters = {
        "label": [
            f"{BRANCH_LABEL}={branch_name}"
        ]
    }

    # Remove containers
    containers = client.containers.list(all=True, filters=filters)
    for container in containers:
        container.stop()
        container.remove(v=True)

    # Remove networks
    networks = client.networks.list(filters=filters)
    for network in networks:
        network.remove()

    # Remove workspace
    workspace_path = _get_workspace_path(branch_name)
    if os.path.exists(workspace_path):
        shutil.rmtree(workspace_path)

def list_environments() -> List[Dict[str, Any]]:
    client = get_client()
    filters = {
        "label": [
            MANAGED_LABEL
        ]
    }
    containers = client.containers.list(all=True, filters=filters)

    envs = {}
    for container in containers:
        branch = container.labels.get(BRANCH_LABEL)
        if not branch:
            continue

        if branch not in envs:
            envs[branch] = {"branch": branch, "containers": [], "status": "running", "url": None}

        container_info = {
            "name": container.name,
            "status": container.status,
            "image": container.image.tags[0] if container.image.tags else "unknown"
        }

        # Extract URL from Odoo container
        if "-odoo" in container.name:
            ports = container.attrs.get('NetworkSettings', {}).get('Ports', {})
            if ports:
                # Look for 8069 mapping
                mappings = ports.get('8069/tcp')
                if mappings:
                    host_port = mappings[0].get('HostPort')
                    if host_port:
                        envs[branch]["url"] = f"http://{EXTERNAL_HOST}:{host_port}"

        envs[branch]["containers"].append(container_info)

        if container.status != "running":
            envs[branch]["status"] = "partial"

    return list(envs.values())

def test_environment(branch_name: str, modules: str) -> str:
    client = get_client()
    odoo_container_name = _get_resource_name(branch_name, "odoo")
    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise Exception(f"Odoo container for branch {branch_name} not found.")

    cmd = f"odoo --test-enable --stop-after-init -i {modules} --db_host={_get_resource_name(branch_name, 'db')} -u {ODOO_DB_USER} -p {ODOO_DB_PASSWORD} --database=postgres"

    exit_code, output = container.exec_run(cmd)

    if isinstance(output, bytes):
        return output.decode("utf-8")
    return str(output)

def get_environment_logs(branch_name: str, n_lines: int = 100) -> str:
    client = get_client()
    odoo_container_name = _get_resource_name(branch_name, "odoo")
    try:
        container = client.containers.get(odoo_container_name)
        logs = container.logs(tail=n_lines, stdout=True, stderr=True)
        if isinstance(logs, bytes):
            return logs.decode("utf-8")
        return str(logs)
    except docker.errors.NotFound:
        raise Exception(f"Odoo container for branch {branch_name} not found.")
    except Exception as e:
        raise Exception(f"Error fetching logs: {str(e)}")

def restart_environment(branch_name: str) -> Dict[str, str]:
    client = get_client()
    odoo_container_name = _get_resource_name(branch_name, "odoo")
    db_container_name = _get_resource_name(branch_name, "db")

    try:
        odoo_container = client.containers.get(odoo_container_name)
        odoo_container.restart()
    except docker.errors.NotFound:
        raise Exception(f"Odoo container for branch {branch_name} not found.")

    try:
        db_container = client.containers.get(db_container_name)
        db_container.restart()
    except docker.errors.NotFound:
        pass  # DB container may not exist in some setups

    return {
        "odoo_container": odoo_container_name,
        "db_container": db_container_name
    }

def get_environment_status(branch_name: str) -> Dict[str, Any]:
    client = get_client()
    odoo_container_name = _get_resource_name(branch_name, "odoo")
    db_container_name = _get_resource_name(branch_name, "db")

    result = {
        "branch": branch_name,
        "odoo": {"name": odoo_container_name, "running": False, "status": "not found"},
        "db": {"name": db_container_name, "running": False, "status": "not found"}
    }

    try:
        odoo_container = client.containers.get(odoo_container_name)
        result["odoo"]["status"] = odoo_container.status
        result["odoo"]["running"] = odoo_container.status == "running"
    except docker.errors.NotFound:
        pass

    try:
        db_container = client.containers.get(db_container_name)
        result["db"]["status"] = db_container.status
        result["db"]["running"] = db_container.status == "running"
    except docker.errors.NotFound:
        pass

    result["all_running"] = result["odoo"]["running"] and result["db"]["running"]
    return result

def stop_environment(branch_name: str) -> Dict[str, str]:
    client = get_client()
    odoo_container_name = _get_resource_name(branch_name, "odoo")
    db_container_name = _get_resource_name(branch_name, "db")

    stopped = []

    try:
        odoo_container = client.containers.get(odoo_container_name)
        odoo_container.stop()
        stopped.append(odoo_container_name)
    except docker.errors.NotFound:
        raise Exception(f"Odoo container for branch {branch_name} not found.")

    try:
        db_container = client.containers.get(db_container_name)
        db_container.stop()
        stopped.append(db_container_name)
    except docker.errors.NotFound:
        pass

    return {
        "odoo_container": odoo_container_name,
        "db_container": db_container_name,
        "stopped": stopped
    }

def start_environment(branch_name: str) -> Dict[str, str]:
    client = get_client()
    odoo_container_name = _get_resource_name(branch_name, "odoo")
    db_container_name = _get_resource_name(branch_name, "db")

    started = []

    try:
        db_container = client.containers.get(db_container_name)
        db_container.start()
        started.append(db_container_name)
    except docker.errors.NotFound:
        pass

    try:
        odoo_container = client.containers.get(odoo_container_name)
        odoo_container.start()
        started.append(odoo_container_name)
    except docker.errors.NotFound:
        raise Exception(f"Odoo container for branch {branch_name} not found.")

    return {
        "odoo_container": odoo_container_name,
        "db_container": db_container_name,
        "started": started
    }


def install_odoo_modules(branch_name: str, *modules: str) -> Dict[str, Any]:
    if not modules:
        raise Exception("At least one module name is required.")

    client = get_client()
    odoo_container_name = _get_resource_name(branch_name, "odoo")

    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise Exception(f"Odoo container for branch {branch_name} not found.")

    modules_str = ",".join(modules)
    cmd = f"/entrypoint.sh odoo -d odoo --no-http --stop-after-init -i {modules_str}"
    exit_code, output = container.exec_run(cmd)
    return {
        "modules": list(modules),
        "exit_code": exit_code,
        "output": output.decode("utf-8") if isinstance(output, bytes) else str(output),
    }


def upgrade_odoo_modules(branch_name: str, *modules: str) -> Dict[str, Any]:
    if not modules:
        raise Exception("At least one module name is required.")

    client = get_client()
    odoo_container_name = _get_resource_name(branch_name, "odoo")

    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise Exception(f"Odoo container for branch {branch_name} not found.")

    modules_str = ",".join(modules)
    cmd = f"/entrypoint.sh odoo -d odoo --stop-after-init --no-http -u {modules_str}"

    exit_code, output = container.exec_run(cmd)

    return {
        "modules": list(modules),
        "exit_code": exit_code,
        "command_output": output.decode("utf-8") if isinstance(output, bytes) else str(output),
    }
