import logging
import os
import glob as glob_mod

from docker import DockerClient

from oduflow.docker_ops.system_ops import _exec_sql
from oduflow.env_credentials import load_credentials
from oduflow.naming import get_db_name, get_repo_path, get_resource_name
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")


def _run_scripts_from_dir(
    sanitize_dir: str,
    label: str,
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    env_db: str,
    branch_name: str,
) -> list[str]:
    """Run .sql and .py sanitization scripts from a directory.

    Returns a list of human-readable log lines.
    """
    logs: list[str] = []
    if not os.path.isdir(sanitize_dir):
        return logs

    # .sql scripts
    sql_files = sorted(glob_mod.glob(os.path.join(sanitize_dir, "*.sql")))
    for sql_file in sql_files:
        name = os.path.basename(sql_file)
        try:
            with open(sql_file) as f:
                sql = f.read().strip()
            if not sql:
                continue
            _exec_sql(client, settings, sql, db=env_db)
            logger.info("[%s] Executed sanitize script %s", label, name)
            logs.append(f"[SANITIZE:{label}] Executed {name}")
        except Exception as exc:
            logger.warning("[%s] Sanitize script %s failed: %s", label, name, exc)
            logs.append(f"[SANITIZE:{label}] WARNING: {name} failed: {exc}")

    # .py scripts (executed inside the Odoo container)
    py_files = sorted(glob_mod.glob(os.path.join(sanitize_dir, "*.py")))
    if not py_files:
        return logs

    import docker as _docker

    odoo_container_name = get_resource_name(branch_name, "odoo", settings.prefix)
    try:
        container = client.containers.get(odoo_container_name)
    except _docker.errors.NotFound:
        logs.append(
            f"[SANITIZE:{label}] WARNING: container not found, skipping .py scripts"
        )
        return logs

    creds = load_credentials(
        branch_name, team.workspaces_dir, settings.db_user, settings.db_password
    )

    for py_file in py_files:
        name = os.path.basename(py_file)
        try:
            with open(py_file) as f:
                script = f.read()
            exit_code, output = container.exec_run(
                ["python3", "-c", script],
                environment={
                    "ODOO_DB": env_db,
                    "DB_HOST": settings.shared_db_container,
                    "DB_USER": creds["pg_user"],
                    "DB_PASSWORD": creds["pg_password"],
                },
            )
            output_str = (
                output.decode("utf-8") if isinstance(output, bytes) else str(output)
            )
            if exit_code != 0:
                logger.warning(
                    "[%s] Sanitize script %s failed (exit %d): %s",
                    label,
                    name,
                    exit_code,
                    output_str,
                )
                logs.append(
                    f"[SANITIZE:{label}] WARNING: {name} failed (exit {exit_code})"
                )
            else:
                logger.info("[%s] Executed sanitize script %s", label, name)
                logs.append(f"[SANITIZE:{label}] Executed {name}")
        except Exception as exc:
            logger.warning("[%s] Sanitize script %s failed: %s", label, name, exc)
            logs.append(f"[SANITIZE:{label}] WARNING: {name} failed: {exc}")

    return logs


def sanitize_environment(
    client: DockerClient,
    settings: Settings,
    team: TeamSettings,
    branch_name: str,
) -> list[str]:
    """Sanitize environment database after provisioning.

    Runs sanitization scripts in two tiers:
    1. Team-level scripts from ``{data_dir}/odoo_sanitize/`` (managed by the
       team administrator, created during ``oduflow init``).
    2. Per-project scripts from the repository's ``.odoo_sanitize/`` folder
       (managed by the developer).

    Both folders support ``.sql`` and ``.py`` files executed in alphabetical
    order.

    Returns a list of human-readable log lines describing what was done.
    """
    env_db = get_db_name(branch_name, team.team_id)
    logs: list[str] = []

    # --- Team-level sanitization ---
    system_dir = os.path.join(team.data_dir, "odoo_sanitize")
    logs.extend(
        _run_scripts_from_dir(
            system_dir, "system", client, settings, team, env_db, branch_name
        )
    )

    # --- Per-project sanitization from repo ---
    repo_path = get_repo_path(branch_name, team.workspaces_dir)
    repo_dir = os.path.join(repo_path, ".odoo_sanitize")
    logs.extend(
        _run_scripts_from_dir(
            repo_dir, "repo", client, settings, team, env_db, branch_name
        )
    )

    return logs
