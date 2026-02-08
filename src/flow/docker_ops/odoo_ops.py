import logging
from typing import Any

import docker

from flow.docker_ops.client import get_client
from flow.errors import NotFoundError
from flow.naming import get_db_name, get_resource_name
from flow.settings import Settings

logger = logging.getLogger("flow")


def run_environment_tests(settings: Settings, branch_name: str, modules: str) -> str:
    client = get_client()
    odoo_container_name = get_resource_name(branch_name, "odoo", settings.prefix)
    env_db = get_db_name(branch_name)

    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{branch_name}' does not exist. Use create_environment first."
        )

    cmd = (
        f"odoo --test-enable --stop-after-init -i {modules} "
        f"--db_host={settings.shared_db_container} "
        f"-r {settings.db_user} -w {settings.db_password} "
        f"--database={env_db}"
    )

    logger.info(
        "Running tests",
        extra={"branch": branch_name, "modules": modules},
    )
    exit_code, output = container.exec_run(cmd)

    if isinstance(output, bytes):
        return output.decode("utf-8")
    return str(output)


def get_environment_logs(settings: Settings, branch_name: str, n_lines: int = 100) -> str:
    client = get_client()
    odoo_container_name = get_resource_name(branch_name, "odoo", settings.prefix)

    try:
        container = client.containers.get(odoo_container_name)
        logs = container.logs(tail=n_lines, stdout=True, stderr=True)
        if isinstance(logs, bytes):
            return logs.decode("utf-8")
        return str(logs)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{branch_name}' does not exist. Use create_environment first."
        )


def install_odoo_modules(settings: Settings, branch_name: str, *modules: str) -> dict[str, Any]:
    if not modules:
        raise ValueError("At least one module name is required.")

    client = get_client()
    odoo_container_name = get_resource_name(branch_name, "odoo", settings.prefix)
    env_db = get_db_name(branch_name)

    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{branch_name}' does not exist. Use create_environment first."
        )

    modules_str = ",".join(modules)
    cmd = f"/entrypoint.sh odoo -d {env_db} --no-http --stop-after-init -i {modules_str}"

    logger.info(
        "Installing modules",
        extra={"branch": branch_name, "modules": modules_str},
    )
    exit_code, output = container.exec_run(cmd)

    return {
        "modules": list(modules),
        "exit_code": exit_code,
    }


def _run_odoo_module_command(
    settings: Settings, branch_name: str, flag: str, *modules: str
) -> dict[str, Any]:
    if not modules:
        raise ValueError("At least one module name is required.")

    client = get_client()
    odoo_container_name = get_resource_name(branch_name, "odoo", settings.prefix)
    env_db = get_db_name(branch_name)

    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{branch_name}' does not exist. Use create_environment first."
        )

    modules_str = ",".join(modules)
    cmd = f"/entrypoint.sh odoo -d {env_db} --stop-after-init --no-http {flag} {modules_str}"

    action = "Installing" if flag == "-i" else "Upgrading"
    logger.info(
        "%s modules", action,
        extra={"branch": branch_name, "modules": modules_str},
    )
    exit_code, output = container.exec_run(cmd)

    return {
        "modules": list(modules),
        "exit_code": exit_code,
    }


def upgrade_odoo_modules(settings: Settings, branch_name: str, *modules: str) -> dict[str, Any]:
    return _run_odoo_module_command(settings, branch_name, "-u", *modules)


def install_odoo_modules(settings: Settings, branch_name: str, *modules: str) -> dict[str, Any]:
    return _run_odoo_module_command(settings, branch_name, "-i", *modules)
