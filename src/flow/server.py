import functools
import logging
import os
import threading

from fastmcp import FastMCP

from flow.docker_ops import env_ops, odoo_ops, system_ops
from flow import git_ops
from flow.errors import BusyError, FlowError
from flow.settings import Settings

logger = logging.getLogger("flow")

mcp = FastMCP("Flow")
_busy = threading.Lock()
_settings: Settings | None = None


def _get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
        _settings.validate()
    return _settings


def with_mutex(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not _busy.acquire(blocking=False):
            raise BusyError("Another operation is in progress. Try again later.")
        try:
            return fn(*args, **kwargs)
        finally:
            _busy.release()
    return wrapper


def handle_errors(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            result = fn(*args, **kwargs)
            logger.info("[%s] -> %s", fn.__name__, result)
            return result
        except FlowError as e:
            logger.error("[%s] Error: %s", fn.__name__, e)
            raise ValueError(str(e)) from e
    return wrapper


@mcp.tool()
@handle_errors
@with_mutex
def init_system(dump_path: str = "", version: str = "15.0", force: bool = False) -> str:
    """
    Initialize shared system infrastructure: network, volume, PostgreSQL container,
    and template database from a dump file.

    Args:
        dump_path: Path to the database dump file. Uses FLOW_DUMP_PATH or ~/.flow/odoo_ref.dump if empty.
        version: Odoo version (default "15.0").
        force: If True, recreate the template database even if it exists.
    """
    result = system_ops.init_system(
        _get_settings(),
        dump_path=dump_path or None,
        version=version,
        force=force,
    )
    msg = f"System {result['status']}.\nTemplate DB: {result['template_db']}"
    if "restore_seconds" in result:
        msg += f"\nDB restore time: {result['restore_seconds']}s"
    return msg


@mcp.tool()
@handle_errors
@with_mutex
def destroy_system() -> str:
    """
    Destroy all shared system resources (network, volume, PostgreSQL container).
    Requires all environments to be deleted first.
    """
    result = system_ops.destroy_system(_get_settings())
    return (
        f"System {result['status']}.\n"
        f"Removed: {result['removed']}"
    )


@mcp.tool()
@handle_errors
@with_mutex
def setup_repo_auth(repo_url: str) -> str:
    """
    Cache git credentials for a private repository.

    Accepts a URL with embedded credentials, stores them in git credential store,
    and verifies access with a test clone. After this, create_environment can clone
    the repo without authentication prompts.

    Args:
        repo_url: Repository URL with credentials, e.g. https://user:PAT@github.com/owner/repo.git
    """
    result = git_ops.setup_repo_auth(repo_url)
    return (
        f"Repository authentication configured.\n"
        f"Host: {result['host']}\n"
        f"Repo URL (clean): {result['repo_url']}\n"
        f"Status: {result['status']}\n\n"
        f"You can now use create_environment with the clean URL (without credentials)."
    )


@mcp.tool()
@handle_errors
@with_mutex
def create_environment(branch_name: str, repo_url: str, version: str = "15.0") -> str:
    """
    Provision a new ephemeral Odoo environment for a specific branch and Odoo version.

    Args:
        branch_name: The name of the git branch (will be used for resource naming).
        repo_url: URL of the git repository to clone.
        version: Odoo version to use (default "15.0").
    """
    result = env_ops.create_environment(_get_settings(), branch_name, repo_url, version)
    return (
        f"Environment provisioned successfully!\n"
        f"URL: {result['url']}\n"
        f"Odoo Container: {result['odoo_container']}\n"
        f"Database: {result['database']}\n"
        f"Workspace: {result['workspace']}"
    )


@mcp.tool()
@handle_errors
@with_mutex
def delete_environment(branch_name: str) -> str:
    """
    Stop and remove all resources associated with an Odoo environment.

    Args:
        branch_name: The name of the branch to tear down.
    """
    env_ops.delete_environment(_get_settings(), branch_name)
    return f"Environment for branch '{branch_name}' has been torn down."


@mcp.tool()
@handle_errors
def list_environments() -> str:
    """
    List all managed Odoo environments.
    """
    envs = env_ops.list_environments(_get_settings())
    if not envs:
        return "No active Flow environments found."

    output = "Active Environments:\n"
    for env in envs:
        status_line = f"- Branch: {env['branch']} (Status: {env['status']})"
        if env.get("url"):
            status_line += f" - {env['url']}"
        output += status_line + "\n"
        for container in env["containers"]:
            output += f"  * {container['name']} [{container['status']}] ({container['image']})\n"
    return output


@mcp.tool()
@handle_errors
@with_mutex
def test_environment(branch_name: str, modules: str) -> str:
    """
    Execute Odoo tests for specific modules in an environment.

    Args:
        branch_name: The name of the branch/environment.
        modules: Comma-separated list of modules to test.
    """
    output = odoo_ops.run_environment_tests(_get_settings(), branch_name, modules)
    return f"Test Results for {branch_name}:\n\n{output}"


@mcp.tool()
@handle_errors
def get_environment_logs(branch_name: str, n_lines: int = 100) -> str:
    """
    Get the last N lines of logs from the Odoo container for a specific branch.

    Args:
        branch_name: The name of the branch/environment.
        n_lines: The number of recent log lines to retrieve (default 100).
    """
    output = odoo_ops.get_environment_logs(_get_settings(), branch_name, n_lines)
    return f"Recent logs for {branch_name}:\n\n{output}"


@mcp.tool()
@handle_errors
def restart_environment(branch_name: str) -> str:
    """
    Restart the Odoo container for a specific branch.

    Args:
        branch_name: The name of the branch/environment to restart.
    """
    result = env_ops.restart_environment(_get_settings(), branch_name)
    return (
        f"Environment restarted successfully!\n"
        f"Odoo Container: {result['odoo_container']}"
    )


@mcp.tool()
@handle_errors
def get_environment_status(branch_name: str) -> str:
    """
    Check if containers are running for a specific branch.

    Args:
        branch_name: The name of the branch/environment to check.
    """
    status = env_ops.get_environment_status(_get_settings(), branch_name)
    overall = "All containers running" if status["all_running"] else "Some containers not running"
    return (
        f"Environment Status for '{branch_name}': {overall}\n"
        f"Odoo: {status['odoo']['status']} (running: {status['odoo']['running']})\n"
        f"DB (shared): {status['db']['status']} (running: {status['db']['running']})"
    )


@mcp.tool()
@handle_errors
def stop_environment(branch_name: str) -> str:
    """
    Stop the Odoo container for a specific branch environment.

    Args:
        branch_name: The name of the branch/environment to stop.
    """
    result = env_ops.stop_environment(_get_settings(), branch_name)
    return (
        f"Environment stopped successfully!\n"
        f"Stopped containers: {', '.join(result['stopped'])}"
    )


@mcp.tool()
@handle_errors
def start_environment(branch_name: str) -> str:
    """
    Start all containers for a specific branch environment.

    Args:
        branch_name: The name of the branch/environment to start.
    """
    result = env_ops.start_environment(_get_settings(), branch_name)
    return (
        f"Environment started successfully!\n"
        f"Started containers: {', '.join(result['started'])}"
    )


@mcp.tool()
@handle_errors
@with_mutex
def install_odoo_modules(branch_name: str, modules: str) -> str:
    """
    Install Odoo modules in an environment.

    Args:
        branch_name: The name of the branch/environment.
        modules: Comma-separated list of modules to install (e.g., "sale,crm,web").
    """
    modules_list = [m.strip() for m in modules.split(",") if m.strip()]
    if not modules_list:
        return "Error: At least one module name is required."

    result = odoo_ops.install_odoo_modules(_get_settings(), branch_name, *modules_list)
    return (
        f"Modules installed: {', '.join(result['modules'])}\n"
        f"Exit code: {result['exit_code']}\n\n"
        f"Logs after restart:\n{result['output']}"
    )


@mcp.tool()
@handle_errors
@with_mutex
def upgrade_odoo_modules(branch_name: str, modules: str) -> str:
    """
    Upgrade Odoo modules in an environment.

    Args:
        branch_name: The name of the branch/environment.
        modules: Comma-separated list of modules to upgrade (e.g., "sale,crm,web").
    """
    modules_list = [m.strip() for m in modules.split(",") if m.strip()]
    if not modules_list:
        return "Error: At least one module name is required."

    result = odoo_ops.upgrade_odoo_modules(_get_settings(), branch_name, *modules_list)
    return (
        f"Modules upgraded: {', '.join(result['modules'])}\n"
        f"Exit code: {result['exit_code']}\n\n"
        f"Logs after restart:\n{result['command_output']}"
    )


def main() -> None:
    """Entry point for the Flow MCP server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    global _settings
    _settings = Settings.from_env()
    _settings.validate()

    transport_str = os.getenv("FLOW_TRANSPORT", "http")

    if transport_str == "http":
        from fastmcp.server.http import create_streamable_http_app

        host = os.getenv("FLOW_HOST", "0.0.0.0")
        port = int(os.getenv("FLOW_PORT", "8000"))

        auth_token = (os.getenv("FLOW_AUTH_TOKEN") or "").strip()
        auth = None
        if auth_token:
            from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

            auth = StaticTokenVerifier(
                tokens={auth_token: {"client_id": "flow-user", "scopes": []}}
            )
            logger.info("HTTP Bearer token auth ENABLED")
        else:
            logger.warning("HTTP auth DISABLED (FLOW_AUTH_TOKEN not set)")

        app = create_streamable_http_app(mcp, "/mcp", auth=auth)

        import uvicorn
        uvicorn.run(app, host=host, port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
