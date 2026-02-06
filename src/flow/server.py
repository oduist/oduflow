import os
from fastmcp import FastMCP
from flow import odoo_manager, config

# Initialize FastMCP server
mcp = FastMCP("Flow")


@mcp.tool()
def init_system(dump_path: str = "", version: str = "15.0", force: bool = False) -> str:
    """
    Initialize shared system infrastructure: network, volume, PostgreSQL container,
    and template database from a dump file.

    Args:
        dump_path: Path to the database dump file. Uses FLOW_DUMP_PATH or ~/.flow/odoo_ref.dump if empty.
        version: Odoo version (default "15.0").
        force: If True, recreate the template database even if it exists.
    """
    try:
        result = odoo_manager.init_system(
            dump_path=dump_path or None,
            version=version,
            force=force,
        )
        return (
            f"System {result['status']}.\n"
            f"Template DB: {result['template_db']}"
        )
    except Exception as e:
        return f"Error initializing system: {str(e)}"


@mcp.tool()
def destroy_system() -> str:
    """
    Destroy all shared system resources (network, volume, PostgreSQL container).
    Requires all environments to be deleted first.
    """
    try:
        result = odoo_manager.destroy_system()
        return (
            f"System {result['status']}.\n"
            f"Removed: {result['removed']}"
        )
    except Exception as e:
        return f"Error destroying system: {str(e)}"


@mcp.tool()
def create_environment(branch_name: str, repo_url: str, version: str = "15.0") -> str:
    """
    Provision a new ephemeral Odoo environment for a specific branch and Odoo version.

    Args:
        branch_name: The name of the git branch (will be used for resource naming).
        repo_url: URL of the git repository to clone.
        version: Odoo version to use (default "15.0").
    """
    try:
        result = odoo_manager.create_environment(branch_name, repo_url, version)
        return (
            f"Environment provisioned successfully!\n"
            f"URL: {result['url']}\n"
            f"Odoo Container: {result['odoo_container']}\n"
            f"Database: {result['database']}\n"
            f"Workspace: {result['workspace']}"
        )
    except Exception as e:
        return f"Error provisioning environment: {str(e)}"


@mcp.tool()
def delete_environment(branch_name: str) -> str:
    """
    Stop and remove all resources associated with an Odoo environment.

    Args:
        branch_name: The name of the branch to tear down.
    """
    try:
        odoo_manager.delete_environment(branch_name)
        return f"Environment for branch '{branch_name}' has been torn down."
    except Exception as e:
        return f"Error during teardown: {str(e)}"


@mcp.tool()
def list_environments() -> str:
    """
    List all managed Odoo environments.
    """
    try:
        envs = odoo_manager.list_environments()
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
    except Exception as e:
        return f"Error listing environments: {str(e)}"


@mcp.tool()
def test_environment(branch_name: str, modules: str) -> str:
    """
    Execute Odoo tests for specific modules in an environment.

    Args:
        branch_name: The name of the branch/environment.
        modules: Comma-separated list of modules to test.
    """
    try:
        output = odoo_manager.run_environment_tests(branch_name, modules)
        return f"Test Results for {branch_name}:\n\n{output}"
    except Exception as e:
        return f"Error executing tests: {str(e)}"


@mcp.tool()
def get_environment_logs(branch_name: str, n_lines: int = 100) -> str:
    """
    Get the last N lines of logs from the Odoo container for a specific branch.

    Args:
        branch_name: The name of the branch/environment.
        n_lines: The number of recent log lines to retrieve (default 100).
    """
    try:
        output = odoo_manager.get_environment_logs(branch_name, n_lines)
        return f"Recent logs for {branch_name}:\n\n{output}"
    except Exception as e:
        return f"Error fetching logs: {str(e)}"


@mcp.tool()
def restart_environment(branch_name: str) -> str:
    """
    Restart the Odoo container for a specific branch.

    Args:
        branch_name: The name of the branch/environment to restart.
    """
    try:
        result = odoo_manager.restart_environment(branch_name)
        return (
            f"Environment restarted successfully!\n"
            f"Odoo Container: {result['odoo_container']}"
        )
    except Exception as e:
        return f"Error restarting environment: {str(e)}"


@mcp.tool()
def get_environment_status(branch_name: str) -> str:
    """
    Check if containers are running for a specific branch.

    Args:
        branch_name: The name of the branch/environment to check.
    """
    try:
        status = odoo_manager.get_environment_status(branch_name)
        overall = "All containers running" if status["all_running"] else "Some containers not running"
        return (
            f"Environment Status for '{branch_name}': {overall}\n"
            f"Odoo: {status['odoo']['status']} (running: {status['odoo']['running']})\n"
            f"DB (shared): {status['db']['status']} (running: {status['db']['running']})"
        )
    except Exception as e:
        return f"Error checking status: {str(e)}"


@mcp.tool()
def stop_environment(branch_name: str) -> str:
    """
    Stop the Odoo container for a specific branch environment.

    Args:
        branch_name: The name of the branch/environment to stop.
    """
    try:
        result = odoo_manager.stop_environment(branch_name)
        return (
            f"Environment stopped successfully!\n"
            f"Stopped containers: {', '.join(result['stopped'])}"
        )
    except Exception as e:
        return f"Error stopping environment: {str(e)}"


@mcp.tool()
def start_environment(branch_name: str) -> str:
    """
    Start all containers for a specific branch environment.

    Args:
        branch_name: The name of the branch/environment to start.
    """
    try:
        result = odoo_manager.start_environment(branch_name)
        return (
            f"Environment started successfully!\n"
            f"Started containers: {', '.join(result['started'])}"
        )
    except Exception as e:
        return f"Error starting environment: {str(e)}"


@mcp.tool()
def install_odoo_modules(branch_name: str, modules: str) -> str:
    """
    Install Odoo modules in an environment.

    Args:
        branch_name: The name of the branch/environment.
        modules: Comma-separated list of modules to install (e.g., "sale,crm,web").
    """
    try:
        modules_list = [m.strip() for m in modules.split(",") if m.strip()]
        if not modules_list:
            return "Error: At least one module name is required."

        result = odoo_manager.install_odoo_modules(branch_name, *modules_list)
        return (
            f"Modules installed: {', '.join(result['modules'])}\n"
            f"Exit code: {result['exit_code']}\n\n"
            f"Logs after restart:\n{result['output']}"
        )
    except Exception as e:
        return f"Error installing modules: {str(e)}"


@mcp.tool()
def upgrade_odoo_modules(branch_name: str, modules: str) -> str:
    """
    Upgrade Odoo modules in an environment.

    Args:
        branch_name: The name of the branch/environment.
        modules: Comma-separated list of modules to upgrade (e.g., "sale,crm,web").
    """
    try:
        modules_list = [m.strip() for m in modules.split(",") if m.strip()]
        if not modules_list:
            return "Error: At least one module name is required."

        result = odoo_manager.upgrade_odoo_modules(branch_name, *modules_list)
        return (
            f"Modules upgraded: {', '.join(result['modules'])}\n"
            f"Exit code: {result['exit_code']}\n\n"
            f"Logs after restart:\n{result['command_output']}"
        )
    except Exception as e:
        return f"Error upgrading modules: {str(e)}"


def main() -> None:
    """Entry point for the Flow MCP server."""
    transport_str = os.getenv("FLOW_TRANSPORT", "http")

    if transport_str == "http":
        from fastmcp.server.http import create_streamable_http_app

        host = os.getenv("FLOW_HOST", "0.0.0.0")
        port = int(os.getenv("FLOW_PORT", "8000"))

        app = create_streamable_http_app(mcp)

        import uvicorn
        uvicorn.run(app, host=host, port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
