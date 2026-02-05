import os
from typing import Literal, Any, cast
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from flow import odoo_manager, config

# Initialize FastMCP server
mcp = FastMCP("Flow")

def _get_user_id() -> str:
    """Extract and verify user_id from API_KEY header."""
    headers = get_http_headers()
    api_key = headers.get("API_KEY") or headers.get("api_key")
    if not api_key:
        raise Exception("Missing API_KEY header")
    
    if config.FLOW_API_KEYS:
        allowed_keys = [k.strip() for k in config.FLOW_API_KEYS.split(",") if k.strip()]
        if api_key not in allowed_keys:
            raise Exception("Invalid API_KEY")
            
    return api_key

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
        user_id = _get_user_id()
        result = odoo_manager.provision_env(user_id, branch_name, repo_url, version)
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
        user_id = _get_user_id()
        odoo_manager.teardown_env(user_id, branch_name)
        return f"Environment for branch '{branch_name}' has been torn down."
    except Exception as e:
        return f"Error during teardown: {str(e)}"

@mcp.tool()
def list_envs() -> str:
    """
    List all managed Odoo environments for the current user.
    """
    try:
        user_id = _get_user_id()
        envs = odoo_manager.list_envs(user_id)
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
        user_id = _get_user_id()
        output = odoo_manager.execute_test(user_id, branch_name, modules)
        return f"Test Results for {branch_name}:\n\n{output}"
    except Exception as e:
        return f"Error executing tests: {str(e)}"

def main() -> None:
    """Entry point for the Flow MCP server."""
    transport_str = os.getenv("FLOW_TRANSPORT", "sse")
    if transport_str not in ["stdio", "sse"]:
        transport_str = "sse"
    
    transport = cast(Literal["stdio", "sse"], transport_str)
    
    kwargs: dict[str, Any] = {}
    if transport == "sse":
        kwargs["host"] = os.getenv("FLOW_HOST", "0.0.0.0")
        kwargs["port"] = int(os.getenv("FLOW_PORT", "8000"))
    
    mcp.run(transport=transport, **kwargs)

if __name__ == "__main__":
    main()
