import os
from fastmcp import FastMCP
from flow import odoo_manager, config

# Initialize FastMCP server
mcp = FastMCP("Flow")

@mcp.tool()
def provision_env(branch_name: str, repo_url: str, version: str = "17.0") -> str:
    """
    Provision a new ephemeral Odoo environment for a specific branch.

    Args:
        branch_name: The name of the git branch (will be used for resource naming).
        repo_url: URL of the git repository to clone.
        version: Odoo version to use (default "17.0").
    """
    try:
        result = odoo_manager.provision_env(branch_name, repo_url, version)
        return (
            f"Environment provisioned successfully!\n"
            f"URL: {result['url']}\n"
            f"Odoo Container: {result['odoo_container']}\n"
            f"DB Container: {result['db_container']}\n"
            f"Workspace: {result['workspace']}"
        )
    except Exception as e:
        return f"Error provisioning environment: {str(e)}"

@mcp.tool()
def teardown_env(branch_name: str) -> str:
    """
    Stop and remove all resources associated with an Odoo environment.

    Args:
        branch_name: The name of the branch to tear down.
    """
    try:
        odoo_manager.teardown_env(branch_name)
        return f"Environment for branch '{branch_name}' has been torn down."
    except Exception as e:
        return f"Error during teardown: {str(e)}"

@mcp.tool()
def list_envs() -> str:
    """
    List all managed Odoo environments.
    """
    try:
        envs = odoo_manager.list_envs()
        if not envs:
            return "No active Flow environments found."

        output = "Active Environments:\n"
        for env in envs:
            status_line = f"- Branch: {env['branch']} (Status: {env['status']})"
            if env.get('url'):
                status_line += f" - {env['url']}"
            output += status_line + "\n"
            for container in env['containers']:
                output += f"  * {container['name']} [{container['status']}] ({container['image']})\n"
        return output
    except Exception as e:
        return f"Error listing environments: {str(e)}"

@mcp.tool()
def execute_test(branch_name: str, modules: str) -> str:
    """
    Execute Odoo tests for specific modules in an environment.

    Args:
        branch_name: The name of the branch/environment.
        modules: Comma-separated list of modules to test.
    """
    try:
        output = odoo_manager.execute_test(branch_name, modules)
        return f"Test Results for {branch_name}:\n\n{output}"
    except Exception as e:
        return f"Error executing tests: {str(e)}"

@mcp.tool()
def get_env_odoo_log(branch_name: str, n_lines: int = 100) -> str:
    """
    Get the last N lines of logs from the Odoo container for a specific branch.

    Args:
        branch_name: The name of the branch/environment.
        n_lines: The number of recent log lines to retrieve (default 100).
    """
    try:
        output = odoo_manager.get_env_odoo_log(branch_name, n_lines)
        return f"Recent logs for {branch_name}:\n\n{output}"
    except Exception as e:
        return f"Error fetching logs: {str(e)}"

def main() -> None:
    """Entry point for the Flow MCP server."""
    transport_str = os.getenv("FLOW_TRANSPORT", "stdio")
    
    if transport_str == "http":
        from fastmcp.server.http import create_streamable_http_app
        
        host = os.getenv("FLOW_HOST", "0.0.0.0")
        port = int(os.getenv("FLOW_PORT", "8000"))
        
        # Create streamable HTTP app without auth
        app = create_streamable_http_app(mcp)
        
        # Run with Uvicorn
        import uvicorn
        uvicorn.run(app, host=host, port=port)
    else:
        # Fallback to stdio
        mcp.run(transport="stdio")

if __name__ == "__main__":
    main()
