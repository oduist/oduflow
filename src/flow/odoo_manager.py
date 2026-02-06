import docker
import os
import re
import subprocess
import shutil
import socket
import time
from typing import List, Dict, Any, Optional
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
    MODULE_OPERATION_LOG_LINES,
    SHARED_NETWORK,
    SHARED_DB_CONTAINER,
    SHARED_DB_VOLUME,
    TEMPLATE_DB_NAME,
    SYSTEM_LABEL,
    DUMP_FILE_PATH,
)


def get_client() -> DockerClient:
    return docker.from_env()


def _get_resource_name(branch_name: str, resource_type: str) -> str:
    return f"{PREFIX}{branch_name.replace('/', '-')}-{resource_type}"


def _get_workspace_path(branch_name: str) -> str:
    return os.path.join(WORKSPACES_DIR, branch_name.replace("/", "-"))


def _slugify_branch(branch_name: str) -> str:
    slug = branch_name.replace("/", "-")
    slug = re.sub(r"[^a-zA-Z0-9_-]", "", slug)
    slug = slug.lower()
    return slug[:63]


def _get_db_name(branch_name: str) -> str:
    return f"flow_{_slugify_branch(branch_name)}"


def _get_available_port() -> int:
    for port in range(PORT_RANGE_START, PORT_RANGE_END + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
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


def _exec_sql(client: DockerClient, sql: str, db: str = "postgres") -> str:
    container = client.containers.get(SHARED_DB_CONTAINER)
    exit_code, output = container.exec_run(
        ["psql", "-U", ODOO_DB_USER, "-d", db, "-tAc", sql]
    )
    result = output.decode("utf-8") if isinstance(output, bytes) else str(output)
    if exit_code != 0:
        raise Exception(f"SQL error (exit {exit_code}): {result}")
    return result.strip()


def _db_exists(client: DockerClient, db_name: str) -> bool:
    result = _exec_sql(
        client,
        f"SELECT 1 FROM pg_database WHERE datname='{db_name}';",
    )
    return result == "1"


def _ensure_system_ready(client: DockerClient) -> None:
    try:
        db_container = client.containers.get(SHARED_DB_CONTAINER)
        if db_container.status != "running":
            raise Exception(
                f"{SHARED_DB_CONTAINER} is not running. Run init_system first."
            )
    except docker.errors.NotFound:
        raise Exception(
            f"{SHARED_DB_CONTAINER} not found. Run init_system first."
        )

    if not _db_exists(client, TEMPLATE_DB_NAME):
        raise Exception(
            f"Template database '{TEMPLATE_DB_NAME}' not found. Run init_system first."
        )


def _wait_pg_ready(client: DockerClient, timeout: int = 30) -> None:
    container = client.containers.get(SHARED_DB_CONTAINER)
    for _ in range(timeout):
        exit_code, _ = container.exec_run(
            ["pg_isready", "-U", ODOO_DB_USER]
        )
        if exit_code == 0:
            return
        time.sleep(1)
    raise Exception(f"PostgreSQL did not become ready within {timeout}s")


# ---------------------------------------------------------------------------
# init_system
# ---------------------------------------------------------------------------

def init_system(
    dump_path: Optional[str] = None,
    version: str = "15.0",
    force: bool = False,
) -> Dict[str, str]:
    client = get_client()

    system_labels = {MANAGED_LABEL: "true", SYSTEM_LABEL: "true"}

    # 1. Ensure flow-net
    try:
        client.networks.get(SHARED_NETWORK)
    except docker.errors.NotFound:
        client.networks.create(SHARED_NETWORK, labels=system_labels)

    # 2. Ensure flow-db-data volume
    try:
        client.volumes.get(SHARED_DB_VOLUME)
    except docker.errors.NotFound:
        client.volumes.create(SHARED_DB_VOLUME, labels=system_labels)

    # 3. Ensure flow-db container
    try:
        db_container = client.containers.get(SHARED_DB_CONTAINER)
        if db_container.status != "running":
            db_container.start()
    except docker.errors.NotFound:
        client.containers.run(
            DEFAULT_POSTGRES_IMAGE,
            name=SHARED_DB_CONTAINER,
            detach=True,
            network=SHARED_NETWORK,
            volumes={SHARED_DB_VOLUME: {"bind": "/var/lib/postgresql/data", "mode": "rw"}},
            environment={
                "POSTGRES_USER": ODOO_DB_USER,
                "POSTGRES_PASSWORD": ODOO_DB_PASSWORD,
            },
            labels=system_labels,
            restart_policy={"Name": "unless-stopped"},
        )

    # 4. Wait PG ready
    _wait_pg_ready(client)

    # 5. Check template DB
    if _db_exists(client, TEMPLATE_DB_NAME):
        if not force:
            return {"status": "already initialized", "template_db": TEMPLATE_DB_NAME}
        _exec_sql(
            client,
            f"UPDATE pg_database SET datistemplate=false WHERE datname='{TEMPLATE_DB_NAME}';",
        )
        _exec_sql(client, f"DROP DATABASE {TEMPLATE_DB_NAME};")

    # 6. Determine dump path
    resolved_dump = dump_path or DUMP_FILE_PATH
    if not os.path.isfile(resolved_dump):
        raise Exception(f"Dump file not found: {resolved_dump}")

    # 7. Create odoo_ref
    _exec_sql(client, f"CREATE DATABASE {TEMPLATE_DB_NAME};")

    # 8. Copy dump into container and restore
    db_container = client.containers.get(SHARED_DB_CONTAINER)
    tmp_name = os.path.basename(resolved_dump)
    subprocess.run(
        ["docker", "cp", resolved_dump, f"{SHARED_DB_CONTAINER}:/tmp/{tmp_name}"],
        check=True,
        capture_output=True,
    )

    if resolved_dump.endswith(".sql"):
        exit_code, output = db_container.exec_run(
            ["psql", "-U", ODOO_DB_USER, "-d", TEMPLATE_DB_NAME, "-f", f"/tmp/{tmp_name}"]
        )
    else:
        exit_code, output = db_container.exec_run(
            ["pg_restore", "-U", ODOO_DB_USER, "-d", TEMPLATE_DB_NAME, f"/tmp/{tmp_name}"]
        )

    if exit_code != 0:
        msg = output.decode("utf-8") if isinstance(output, bytes) else str(output)
        raise Exception(f"Dump restore failed (exit {exit_code}): {msg}")

    # 9. Mark as template
    _exec_sql(
        client,
        f"UPDATE pg_database SET datistemplate=true WHERE datname='{TEMPLATE_DB_NAME}';",
    )

    return {"status": "initialized", "template_db": TEMPLATE_DB_NAME}


# ---------------------------------------------------------------------------
# destroy_system
# ---------------------------------------------------------------------------

def destroy_system() -> Dict[str, str]:
    client = get_client()

    # Check no active environments
    filters = {"label": [f"{MANAGED_LABEL}=true"]}
    containers = client.containers.list(all=True, filters=filters)
    env_containers = [
        c for c in containers
        if c.labels.get(BRANCH_LABEL) and c.name != SHARED_DB_CONTAINER
    ]
    if env_containers:
        names = [c.name for c in env_containers]
        raise Exception(
            f"Active environments exist: {', '.join(names)}. Delete them first."
        )

    removed: List[str] = []

    # Stop & remove flow-db
    try:
        db = client.containers.get(SHARED_DB_CONTAINER)
        db.stop()
        db.remove(v=True)
        removed.append(SHARED_DB_CONTAINER)
    except docker.errors.NotFound:
        pass

    # Remove volume
    try:
        vol = client.volumes.get(SHARED_DB_VOLUME)
        vol.remove()
        removed.append(SHARED_DB_VOLUME)
    except docker.errors.NotFound:
        pass

    # Remove network
    try:
        net = client.networks.get(SHARED_NETWORK)
        net.remove()
        removed.append(SHARED_NETWORK)
    except docker.errors.NotFound:
        pass

    return {"status": "destroyed", "removed": ", ".join(removed)}


# ---------------------------------------------------------------------------
# create_environment
# ---------------------------------------------------------------------------

def create_environment(
    branch_name: str, repo_url: str, version: str = "15.0"
) -> Dict[str, str]:
    try:
        client = get_client()
    except Exception as e:
        raise Exception(
            f"Failed to connect to Docker daemon: {str(e)}. Ensure Docker is running."
        )

    _ensure_system_ready(client)

    odoo_container_name = _get_resource_name(branch_name, "odoo")
    workspace_path = _get_workspace_path(branch_name)
    env_db = _get_db_name(branch_name)

    labels = {MANAGED_LABEL: "true", BRANCH_LABEL: branch_name}

    # 1. Prepare workspace & clone
    if os.path.exists(workspace_path):
        shutil.rmtree(workspace_path)
    os.makedirs(workspace_path, exist_ok=True)

    try:
        subprocess.run(
            [
                "git", "clone", "--branch", branch_name,
                "--depth", "1", repo_url, workspace_path,
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.decode("utf-8") if e.stderr else str(e)
        if "Permission denied" in error_msg or "not found" in error_msg:
            raise Exception(
                "Failed to clone repository: Invalid credentials or repository not found."
            )
        elif "Repository not found" in error_msg:
            raise Exception(f"Repository not found: {repo_url}")
        elif "branch" in error_msg.lower() and "not found" in error_msg.lower():
            raise Exception(
                f"Branch '{branch_name}' not found in repository {repo_url}"
            )
        else:
            raise Exception(f"Failed to clone repository: {error_msg}")
    except subprocess.TimeoutExpired:
        raise Exception(
            "Repository clone timed out (60s). Repository may be too large or network is slow."
        )

    # 2. Clone DB from template
    _exec_sql(
        client,
        f'CREATE DATABASE "{env_db}" TEMPLATE {TEMPLATE_DB_NAME};',
    )

    # 3. Start Odoo container
    odoo_image = f"{DEFAULT_ODOO_IMAGE}:{version}"
    host_port = _get_available_port()

    odoo_env = {
        "HOST": SHARED_DB_CONTAINER,
        "USER": ODOO_DB_USER,
        "PASSWORD": ODOO_DB_PASSWORD,
    }
    odoo_volumes = {workspace_path: {"bind": "/mnt/extra-addons", "mode": "rw"}}

    client.containers.run(
        odoo_image,
        name=odoo_container_name,
        detach=True,
        network=SHARED_NETWORK,
        environment=odoo_env,
        labels=labels,
        ports={"8069/tcp": host_port},
        volumes=odoo_volumes,
        restart_policy={"Name": "unless-stopped"},
        command=f"odoo -d {env_db} --dev=xml",
    )

    return {
        "url": f"http://{EXTERNAL_HOST}:{host_port}",
        "odoo_container": odoo_container_name,
        "database": env_db,
        "workspace": workspace_path,
    }


# ---------------------------------------------------------------------------
# delete_environment
# ---------------------------------------------------------------------------

def delete_environment(branch_name: str) -> None:
    client = get_client()
    odoo_container_name = _get_resource_name(branch_name, "odoo")
    env_db = _get_db_name(branch_name)

    # 1. Stop & remove Odoo container
    try:
        container = client.containers.get(odoo_container_name)
        container.stop()
        container.remove(v=True)
    except docker.errors.NotFound:
        pass

    # 2. Drop database
    try:
        _exec_sql(
            client,
            f'DROP DATABASE IF EXISTS "{env_db}" WITH (FORCE);',
        )
    except Exception:
        pass

    # 3. Remove workspace
    workspace_path = _get_workspace_path(branch_name)
    if os.path.exists(workspace_path):
        shutil.rmtree(workspace_path)


# ---------------------------------------------------------------------------
# list_environments
# ---------------------------------------------------------------------------

def list_environments() -> List[Dict[str, Any]]:
    client = get_client()
    filters = {"label": [MANAGED_LABEL]}
    containers = client.containers.list(all=True, filters=filters)

    envs: Dict[str, Dict[str, Any]] = {}
    for container in containers:
        branch = container.labels.get(BRANCH_LABEL)
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
                        envs[branch]["url"] = f"http://{EXTERNAL_HOST}:{host_port}"

        envs[branch]["containers"].append(container_info)

        if container.status != "running":
            envs[branch]["status"] = "partial"

    return list(envs.values())


# ---------------------------------------------------------------------------
# restart / stop / start
# ---------------------------------------------------------------------------

def restart_environment(branch_name: str) -> Dict[str, str]:
    client = get_client()
    odoo_container_name = _get_resource_name(branch_name, "odoo")

    try:
        odoo_container = client.containers.get(odoo_container_name)
        odoo_container.restart()
    except docker.errors.NotFound:
        raise Exception(f"Odoo container for branch {branch_name} not found.")

    return {"odoo_container": odoo_container_name}


def stop_environment(branch_name: str) -> Dict[str, str]:
    client = get_client()
    odoo_container_name = _get_resource_name(branch_name, "odoo")

    try:
        odoo_container = client.containers.get(odoo_container_name)
        odoo_container.stop()
    except docker.errors.NotFound:
        raise Exception(f"Odoo container for branch {branch_name} not found.")

    return {"odoo_container": odoo_container_name, "stopped": [odoo_container_name]}


def start_environment(branch_name: str) -> Dict[str, str]:
    client = get_client()
    odoo_container_name = _get_resource_name(branch_name, "odoo")

    # Ensure flow-db is running
    try:
        db_container = client.containers.get(SHARED_DB_CONTAINER)
        if db_container.status != "running":
            db_container.start()
    except docker.errors.NotFound:
        raise Exception(
            f"{SHARED_DB_CONTAINER} not found. Run init_system first."
        )

    started = [SHARED_DB_CONTAINER]

    try:
        odoo_container = client.containers.get(odoo_container_name)
        odoo_container.start()
        started.append(odoo_container_name)
    except docker.errors.NotFound:
        raise Exception(f"Odoo container for branch {branch_name} not found.")

    return {"odoo_container": odoo_container_name, "started": started}


# ---------------------------------------------------------------------------
# get_environment_status
# ---------------------------------------------------------------------------

def get_environment_status(branch_name: str) -> Dict[str, Any]:
    client = get_client()
    odoo_container_name = _get_resource_name(branch_name, "odoo")

    result: Dict[str, Any] = {
        "branch": branch_name,
        "odoo": {"name": odoo_container_name, "running": False, "status": "not found"},
        "db": {"name": SHARED_DB_CONTAINER, "running": False, "status": "not found"},
    }

    try:
        odoo_container = client.containers.get(odoo_container_name)
        result["odoo"]["status"] = odoo_container.status
        result["odoo"]["running"] = odoo_container.status == "running"
    except docker.errors.NotFound:
        pass

    try:
        db_container = client.containers.get(SHARED_DB_CONTAINER)
        result["db"]["status"] = db_container.status
        result["db"]["running"] = db_container.status == "running"
    except docker.errors.NotFound:
        pass

    result["all_running"] = result["odoo"]["running"] and result["db"]["running"]
    return result


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------

def run_environment_tests(branch_name: str, modules: str) -> str:
    client = get_client()
    odoo_container_name = _get_resource_name(branch_name, "odoo")
    env_db = _get_db_name(branch_name)
    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise Exception(f"Odoo container for branch {branch_name} not found.")

    cmd = (
        f"odoo --test-enable --stop-after-init -i {modules} "
        f"--db_host={SHARED_DB_CONTAINER} "
        f"-r {ODOO_DB_USER} -w {ODOO_DB_PASSWORD} "
        f"--database={env_db}"
    )

    exit_code, output = container.exec_run(cmd)

    if isinstance(output, bytes):
        return output.decode("utf-8")
    return str(output)


# ---------------------------------------------------------------------------
# install / upgrade modules
# ---------------------------------------------------------------------------

def install_odoo_modules(branch_name: str, *modules: str) -> Dict[str, Any]:
    if not modules:
        raise Exception("At least one module name is required.")

    client = get_client()
    odoo_container_name = _get_resource_name(branch_name, "odoo")
    env_db = _get_db_name(branch_name)

    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise Exception(f"Odoo container for branch {branch_name} not found.")

    modules_str = ",".join(modules)
    cmd = f"/entrypoint.sh odoo -d {env_db} --no-http --stop-after-init -i {modules_str}"
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
    env_db = _get_db_name(branch_name)

    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise Exception(f"Odoo container for branch {branch_name} not found.")

    modules_str = ",".join(modules)
    cmd = f"/entrypoint.sh odoo -d {env_db} --stop-after-init --no-http -u {modules_str}"

    exit_code, output = container.exec_run(cmd)

    return {
        "modules": list(modules),
        "exit_code": exit_code,
        "command_output": output.decode("utf-8") if isinstance(output, bytes) else str(output),
    }
