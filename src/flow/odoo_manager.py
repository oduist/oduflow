import docker
import os
import subprocess
import shutil
import socket
from typing import List, Dict, Any
from docker import DockerClient
from flow.config import (
    PREFIX,
    BRANCH_LABEL,
    MANAGED_LABEL,
    USER_LABEL,
    DEFAULT_ODOO_IMAGE,
    DEFAULT_POSTGRES_IMAGE,
    ODOO_DB_USER,
    ODOO_DB_PASSWORD,
    WORKSPACES_DIR,
    EXTERNAL_HOST,
    PORT_RANGE_START,
    PORT_RANGE_END
)

def get_client() -> DockerClient:
    return docker.from_env()

def _get_resource_name(user_id: str, branch_name: str, resource_type: str) -> str:
    return f"{PREFIX}{user_id}-{branch_name.replace('/', '-')}-{resource_type}"

def _get_workspace_path(user_id: str, branch_name: str) -> str:
    return os.path.join(WORKSPACES_DIR, user_id, branch_name.replace('/', '-'))

def _get_available_port() -> int:
    for port in range(PORT_RANGE_START, PORT_RANGE_END + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('0.0.0.0', port))
                return port
            except OSError:
                continue
    raise Exception(f"No available ports in range {PORT_RANGE_START}-{PORT_RANGE_END}")

def provision_env(user_id: str, branch_name: str, repo_url: str, version: str = "17.0") -> Dict[str, str]:
    client = get_client()
    network_name = _get_resource_name(user_id, branch_name, "net")
    db_container_name = _get_resource_name(user_id, branch_name, "db")
    odoo_container_name = _get_resource_name(user_id, branch_name, "odoo")
    workspace_path = _get_workspace_path(user_id, branch_name)
    
    labels = {
        MANAGED_LABEL: "true",
        BRANCH_LABEL: branch_name,
        USER_LABEL: user_id
    }

    # 1. Prepare Workspace and Clone Repo
    if os.path.exists(workspace_path):
        shutil.rmtree(workspace_path)
    os.makedirs(workspace_path, exist_ok=True)
    
    # Clone and checkout branch
    subprocess.run(
        ["git", "clone", "--branch", branch_name, "--depth", "1", repo_url, workspace_path],
        check=True,
        capture_output=True
    )

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
            "POSTGRES_PASSWORD": ODOO_DB_PASSWORD,
            "POSTGRES_DB": "postgres"
        },
        labels=labels,
        restart_policy={"Name": "always"}
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
        ports={'8072/tcp': host_port},
        volumes={
            workspace_path: {'bind': '/mnt/extra-addons', 'mode': 'rw'}
        },
        restart_policy={"Name": "always"}
    )

    return {
        "url": f"http://{EXTERNAL_HOST}:{host_port}",
        "odoo_container": odoo_container_name,
        "db_container": db_container_name,
        "network": network_name,
        "workspace": workspace_path
    }

def teardown_env(user_id: str, branch_name: str) -> None:
    client = get_client()
    # Find resources by label
    filters = {
        "label": [
            f"{BRANCH_LABEL}={branch_name}",
            f"{USER_LABEL}={user_id}"
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
    workspace_path = _get_workspace_path(user_id, branch_name)
    if os.path.exists(workspace_path):
        shutil.rmtree(workspace_path)

def list_envs(user_id: str) -> List[Dict[str, Any]]:
    client = get_client()
    filters = {
        "label": [
            MANAGED_LABEL,
            f"{USER_LABEL}={user_id}"
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
                # Look for 8072 mapping
                mappings = ports.get('8072/tcp')
                if mappings:
                    host_port = mappings[0].get('HostPort')
                    if host_port:
                        envs[branch]["url"] = f"http://{EXTERNAL_HOST}:{host_port}"

        envs[branch]["containers"].append(container_info)
        
        if container.status != "running":
            envs[branch]["status"] = "partial"

    return list(envs.values())

def execute_test(user_id: str, branch_name: str, modules: str) -> str:
    client = get_client()
    odoo_container_name = _get_resource_name(user_id, branch_name, "odoo")
    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise Exception(f"Odoo container for branch {branch_name} not found.")

    cmd = f"odoo --test-enable --stop-after-init -i {modules} --db_host={_get_resource_name(user_id, branch_name, 'db')} -u {ODOO_DB_USER} -p {ODOO_DB_PASSWORD} --database=postgres"
    
    exit_code, output = container.exec_run(cmd)
    
    if isinstance(output, bytes):
        return output.decode("utf-8")
    return str(output)
