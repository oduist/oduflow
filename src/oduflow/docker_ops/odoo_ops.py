from __future__ import annotations

import logging
import re
from typing import Any

import docker

from oduflow.docker_ops.client import get_client
from oduflow.env_credentials import load_credentials
from oduflow.errors import ExternalCommandError, NotFoundError
from oduflow.naming import get_db_name, get_resource_name
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")


def _detect_odoo_major(container: Any, image_label: str) -> int | None:
    """Best-effort major version of the Odoo running in *container*.

    Fast path: parse the version out of the image tag stored in the
    ``oduflow.image`` label (e.g. ``odoo:15.0`` → 15). For custom-tagged images
    that carry no version (e.g. ``oduist/customer_odoo``) it falls back to asking
    the already-running binary via ``odoo --version`` — authoritative and
    independent of the image name. Returns ``None`` if the version can't be
    determined.
    """
    image = container.labels.get(image_label, "")
    match = re.search(r"odoo[:/](\d+)", image)
    if match:
        return int(match.group(1))

    try:
        _code, out = container.exec_run("odoo --version")
        text = out.decode("utf-8") if isinstance(out, bytes) else str(out)
        match = re.search(r"(\d+)\.\d+", text)
        if match:
            return int(match.group(1))
    except Exception as exc:  # noqa: BLE001 - version detection is best-effort
        logger.warning("Could not detect Odoo version from container: %s", exc)
    return None


def _longpoll_port_flag(container: Any, image_label: str) -> str:
    """CLI flag for the test server's long-polling/gevent port, per Odoo version.

    Odoo 16.0 renamed ``--longpolling-port`` to ``--gevent-port``; the new flag
    does not exist on 15.0 and earlier (Odoo aborts on the unknown option), while
    the old name still works as a deprecated alias on 16+. Pick the flag the
    running Odoo actually understands, defaulting to the modern ``--gevent-port``
    when the version can't be determined.
    """
    major = _detect_odoo_major(container, image_label)
    if major is not None and major < 16:
        return "--longpolling-port"
    return "--gevent-port"


def run_environment_tests(
    settings: Settings, team: TeamSettings, env_name: str, modules: str
) -> str:
    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )
    env_db = get_db_name(env_name, team.team_id)

    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )

    creds = load_credentials(
        env_name, team.workspaces_dir, settings.db_user, settings.db_password
    )
    # Use -u (upgrade), not -i (install): the module is already installed in the
    # template, and -i on an installed module is a no-op that never enters the test
    # phase (→ "0 of 0 tests"). -u re-runs the module's tests on the existing DB.
    #
    # --no-http has no effect under --test-enable (tests need a live HTTP server),
    # so instead of disabling HTTP we move the test server's HTTP and gevent ports
    # off the defaults (8069/8072) already held by the running Odoo container.
    # Odoo 16.0 renamed --longpolling-port to --gevent-port, so pick the flag the
    # environment's Odoo version actually understands.
    port_flag = _longpoll_port_flag(container, settings.image_label)
    cmd = (
        f"odoo --test-enable --stop-after-init --workers 0 "
        f"--http-port 8089 {port_flag} 8090 -u {modules} "
        f"--db_host={settings.shared_db_container} "
        f"-r {creds['pg_user']} -w {creds['pg_password']} "
        f"--database={env_db}"
    )
    logger.info(
        "Running tests",
        extra={"env_name": env_name, "modules": modules},
    )
    exit_code, output = container.exec_run(cmd)

    if isinstance(output, bytes):
        return output.decode("utf-8")
    return str(output)


def get_environment_logs(
    settings: Settings,
    env_name: str,
    n_lines: int = 100,
    grep: str = "",
    level: str = "",
    container_name: str = "",
    *,
    team: TeamSettings,
) -> str:
    client = get_client()
    if container_name:
        # Only allow containers that belong to this environment.
        env_prefix = get_resource_name(env_name, "", settings.prefix, team.team_id)
        if not container_name.startswith(env_prefix):
            raise NotFoundError(
                f"Container '{container_name}' does not belong to "
                f"environment '{env_name}'."
            )
        target_container_name = container_name
    else:
        target_container_name = get_resource_name(
            env_name, "odoo", settings.prefix, team.team_id
        )

    # Fetch more lines when filtering to have meaningful results
    fetch_lines = min(n_lines * 10, 10_000) if (grep or level) else n_lines

    try:
        container = client.containers.get(target_container_name)
    except docker.errors.NotFound:
        if container_name:
            raise NotFoundError(f"Container '{container_name}' does not exist.")
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )
    # Defence in depth on top of team-scoped names: never expose another
    # team's logs even if a name resolves unexpectedly (issue #39).
    label = container.labels.get(settings.team_label)
    if label is not None and label != team.team_id:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )
    logs = container.logs(tail=fetch_lines, stdout=True, stderr=True)
    logs_str = logs.decode("utf-8") if isinstance(logs, bytes) else str(logs)

    if grep or level:
        lines = logs_str.splitlines()
        filtered = []
        for line in lines:
            if level and f" {level} " not in line.upper():
                continue
            if grep and grep.lower() not in line.lower():
                continue
            filtered.append(line)
        return "\n".join(filtered[-n_lines:])

    return logs_str


def _run_odoo_module_command(
    settings: Settings, team: TeamSettings, env_name: str, flag: str, *modules: str
) -> dict[str, Any]:
    if not modules:
        raise ValueError("At least one module name is required.")

    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )
    env_db = get_db_name(env_name, team.team_id)

    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )

    modules_str = ",".join(modules)
    cmd = f"/entrypoint.sh odoo -d {env_db} --stop-after-init --no-http {flag} {modules_str}"

    action = "Installing" if flag == "-i" else "Upgrading"
    logger.info(
        "%s modules",
        action,
        extra={"env_name": env_name, "modules": modules_str},
    )
    exit_code, output = container.exec_run(cmd)
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)

    return {
        "modules": list(modules),
        "exit_code": exit_code,
        "output": output_str,
    }


def upgrade_odoo_modules(
    settings: Settings, team: TeamSettings, env_name: str, *modules: str
) -> dict[str, Any]:
    return _run_odoo_module_command(settings, team, env_name, "-u", *modules)


def install_odoo_modules(
    settings: Settings, team: TeamSettings, env_name: str, *modules: str
) -> dict[str, Any]:
    return _run_odoo_module_command(settings, team, env_name, "-i", *modules)


_FILE_SIZE_LIMIT = 100 * 1024  # 100KB


def read_file_in_environment(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    path: str,
    read_range: str = "",
) -> dict[str, Any]:
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

    logger.info(
        "Reading file in environment",
        extra={"env_name": env_name, "path": path},
    )

    # Check if path exists and determine its type
    exit_code, output = container.exec_run(
        [
            "sh",
            "-c",
            f'if [ -d "{path}" ]; then echo DIR; elif [ -f "{path}" ]; then echo FILE; else echo NOTFOUND; fi',
        ]
    )
    path_type = (
        output.decode("utf-8") if isinstance(output, bytes) else str(output)
    ).strip()

    if path_type == "NOTFOUND":
        return {"error": f"Path not found: {path}"}

    if path_type == "DIR":
        exit_code, output = container.exec_run(["ls", "-la", path])
        output_str = (
            output.decode("utf-8") if isinstance(output, bytes) else str(output)
        )
        return {"type": "directory", "output": output_str}

    # It's a file — check if binary
    exit_code, output = container.exec_run(["file", "--mime", path])
    mime_str = (
        output.decode("utf-8") if isinstance(output, bytes) else str(output)
    ).strip()
    if "charset=binary" in mime_str:
        return {
            "error": f"Binary file, cannot display: {path}\nUse run_odoo_command for binary file operations."
        }

    # Check file size
    exit_code, output = container.exec_run(["stat", "-c", "%s", path])
    size_str = (
        output.decode("utf-8") if isinstance(output, bytes) else str(output)
    ).strip()
    try:
        file_size = int(size_str)
    except ValueError:
        file_size = 0

    if read_range:
        # Parse "START:END" format
        parts = read_range.split(":")
        if len(parts) != 2:
            return {
                "error": f"Invalid read_range format: '{read_range}'. Expected 'START:END' (e.g. '1:50')."
            }
        try:
            start, end = int(parts[0]), int(parts[1])
        except ValueError:
            return {
                "error": f"Invalid read_range format: '{read_range}'. START and END must be integers."
            }
        exit_code, output = container.exec_run(["sed", "-n", f"{start},{end}p", path])
        output_str = (
            output.decode("utf-8") if isinstance(output, bytes) else str(output)
        )
        return {
            "type": "file",
            "output": output_str,
            "size": file_size,
            "range": read_range,
        }

    # No range — enforce size limit
    if file_size > _FILE_SIZE_LIMIT:
        size_kb = file_size // 1024
        limit_kb = _FILE_SIZE_LIMIT // 1024
        return {
            "error": (
                f"File is too large ({size_kb}KB, limit {limit_kb}KB). "
                f'Use read_range parameter to read a specific portion, e.g. read_range="1:500".'
            )
        }

    exit_code, output = container.exec_run(["cat", path])
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)
    return {"type": "file", "output": output_str, "size": file_size}


def run_command_in_environment(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    command: str,
    user: str = "odoo",
) -> dict[str, Any]:
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

    logger.info(
        "Executing command in environment",
        extra={"env_name": env_name, "command": command, "user": user},
    )
    exit_code, output = container.exec_run(command, user=user)
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)

    return {
        "exit_code": exit_code,
        "output": output_str,
    }


def reset_admin_password(
    settings: Settings, team: TeamSettings, env_name: str, new_password: str = "test"
) -> dict[str, str]:
    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )
    env_db = get_db_name(env_name, team.team_id)

    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )

    # Hash the password using passlib inside the Odoo container
    hash_cmd = [
        "python3",
        "-c",
        "from passlib.context import CryptContext; "
        f"print(CryptContext(['pbkdf2_sha512']).hash({new_password!r}))",
    ]
    exit_code, output = container.exec_run(hash_cmd)
    hashed = (
        output.decode("utf-8") if isinstance(output, bytes) else str(output)
    ).strip()
    if exit_code != 0:
        raise ExternalCommandError("python3 (passlib hash)", exit_code, hashed)

    # Update the password in the database
    try:
        db_container = client.containers.get(settings.shared_db_container)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Database container '{settings.shared_db_container}' is not running. "
            "System not initialized. Restart oduflow."
        )

    sql = f"UPDATE res_users SET password = '{hashed}' WHERE login = 'admin'"
    psql_cmd = ["psql", "-U", settings.db_user, "-d", env_db, "-c", sql]
    exit_code, output = db_container.exec_run(psql_cmd)
    output_str = (
        output.decode("utf-8") if isinstance(output, bytes) else str(output)
    ).strip()
    if exit_code != 0:
        raise ExternalCommandError("psql", exit_code, output_str)

    logger.info(
        "Admin password reset",
        extra={"env_name": env_name},
    )
    return {"status": "ok", "login": "admin", "psql_output": output_str}


def run_db_query(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    query: str,
    output_format: str = "csv",
) -> dict[str, Any]:
    client = get_client()
    env_db = get_db_name(env_name, team.team_id)

    try:
        db_container = client.containers.get(settings.shared_db_container)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Database container '{settings.shared_db_container}' is not running. "
            "System not initialized. Restart oduflow."
        )

    creds = load_credentials(
        env_name, team.workspaces_dir, settings.db_user, settings.db_password
    )
    if output_format == "human":
        cmd = ["psql", "-U", creds["pg_user"], "-d", env_db, "-c", query]
    else:
        cmd = ["psql", "-U", creds["pg_user"], "-d", env_db, "--csv", "-c", query]

    logger.info(
        "Running DB query",
        extra={"env_name": env_name, "format": output_format},
    )
    exit_code, output = db_container.exec_run(cmd)
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)

    if exit_code != 0:
        raise ExternalCommandError("psql", exit_code, output_str)

    return {
        "exit_code": exit_code,
        "output": output_str,
    }


_WRITE_FILE_LIMIT = 1_000_000  # 1 MB


def write_file_in_environment(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    path: str,
    content: str,
    user: str = "odoo",
) -> dict[str, Any]:
    """Write a text file inside the Odoo container via tar stream."""
    import io
    import tarfile

    if len(content.encode("utf-8")) > _WRITE_FILE_LIMIT:
        raise ExternalCommandError(
            "write_file", 1, f"Content exceeds {_WRITE_FILE_LIMIT} byte limit."
        )

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

    parent = path.rsplit("/", 1)[0] if "/" in path else "/"
    container.exec_run(["mkdir", "-p", parent], user=user)

    data = content.encode("utf-8")
    tar_stream = io.BytesIO()
    filename = path.rsplit("/", 1)[-1] if "/" in path else path
    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        info = tarfile.TarInfo(name=filename)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    tar_stream.seek(0)
    container.put_archive(parent, tar_stream)

    if user != "root":
        container.exec_run(["chown", user, path], user="root")

    logger.info(
        "File written in environment",
        extra={"env_name": env_name, "path": path, "size": len(data)},
    )
    return {"path": path, "size": len(data)}


def run_odoo_shell(
    settings: Settings, team: TeamSettings, env_name: str, python_code: str
) -> dict[str, Any]:
    """Execute Python code in the Odoo shell context with full ORM access."""
    import io
    import tarfile

    client = get_client()
    odoo_container_name = get_resource_name(
        env_name, "odoo", settings.prefix, team.team_id
    )
    env_db = get_db_name(env_name, team.team_id)

    try:
        container = client.containers.get(odoo_container_name)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Environment '{env_name}' does not exist. Use create_environment first."
        )

    creds = load_credentials(
        env_name, team.workspaces_dir, settings.db_user, settings.db_password
    )

    # Write Python code to temp file via tar stream (avoids shell escaping)
    script_path = "/tmp/_oduflow_shell_script.py"
    data = python_code.encode("utf-8")
    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        info = tarfile.TarInfo(name="_oduflow_shell_script.py")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    tar_stream.seek(0)
    container.put_archive("/tmp", tar_stream)

    cmd = (
        f"odoo shell --no-http --stop-after-init "
        f"--db_host={settings.shared_db_container} "
        f"-r {creds['pg_user']} -w {creds['pg_password']} "
        f"--database={env_db} "
        f"< {script_path}"
    )

    logger.info(
        "Running Odoo shell",
        extra={"env_name": env_name, "code_size": len(data)},
    )
    exit_code, output = container.exec_run(["sh", "-c", cmd], user="odoo")
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)

    # Cleanup temp file
    container.exec_run(["rm", "-f", script_path])

    return {
        "exit_code": exit_code,
        "output": output_str,
    }


def search_in_environment(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    pattern: str,
    path: str = "/mnt/extra-addons",
    glob: str = "*.py",
    max_results: int = 50,
) -> dict[str, Any]:
    """Search for a pattern in files inside the Odoo container."""
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

    cmd = [
        "grep",
        "-rnH",
        "-F",
        "--include",
        glob,
        pattern,
        path,
    ]

    logger.info(
        "Searching in environment",
        extra={"env_name": env_name, "pattern": pattern, "path": path},
    )
    exit_code, output = container.exec_run(cmd)
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)

    lines = output_str.strip().splitlines()[:max_results] if output_str.strip() else []

    return {
        "matches": len(lines),
        "output": "\n".join(lines),
        "truncated": len(output_str.strip().splitlines()) > max_results,
    }


def http_request_to_odoo(
    settings: Settings,
    team: TeamSettings,
    env_name: str,
    path: str,
    method: str = "GET",
    body: str = "",
    headers: dict[str, str] | None = None,
    session_id: str = "",
) -> dict[str, Any]:
    """Make an HTTP request to the running Odoo instance."""
    import json  # noqa: F401
    import urllib.request
    import urllib.error

    from oduflow.docker_ops.env_ops import get_environment_info

    info = get_environment_info(settings, team, env_name)
    base_url = info["url"]

    url = f"{base_url}{path}"
    req_headers = dict(headers) if headers else {}
    if session_id:
        req_headers["Cookie"] = f"session_id={session_id}"

    data = body.encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)

    logger.info(
        "HTTP request to Odoo",
        extra={"env_name": env_name, "method": method, "path": path},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            response_body = resp.read().decode("utf-8", errors="replace")
            return {
                "status_code": resp.status,
                "headers": dict(resp.headers),
                "body": response_body[:100_000],
            }
    except urllib.error.HTTPError as e:
        response_body = e.read().decode("utf-8", errors="replace")
        return {
            "status_code": e.code,
            "headers": dict(e.headers),
            "body": response_body[:100_000],
        }
    except urllib.error.URLError as e:
        return {
            "status_code": 0,
            "headers": {},
            "body": f"Connection failed: {e.reason}",
        }
