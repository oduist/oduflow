import argparse
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
            preview = (result[:200] + '...') if isinstance(result, str) and len(result) > 200 else result
            logger.info("[%s] -> %s", fn.__name__, preview)
            return result
        except FlowError as e:
            logger.error("[%s] Error: %s", fn.__name__, e)
            raise ValueError(str(e)) from e
    return wrapper


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
def create_environment(branch_name: str, repo_url: str, odoo_image: str) -> str:
    """
    Provision a new ephemeral Odoo environment for a specific branch.

    Args:
        branch_name: The name of the git branch (will be used for resource naming).
        repo_url: URL of the git repository to clone.
        odoo_image: Full Docker image name with tag (e.g. "odoo:17.0"). Use a pre-built image with all dependencies for faster startup.
    """
    result = env_ops.create_environment(_get_settings(), branch_name, repo_url, odoo_image)
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
def promote_environment(branch_name: str) -> str:
    """
    DANGEROUS: Promote a branch environment to become the new reference (DB + filestore).

    This is a destructive, irreversible operation that replaces the shared reference
    database and filestore with the data from the specified branch. All other
    environments will lose their filestore deltas and be reset to the new baseline.

    NEVER call this tool on your own initiative. Requires EXPLICIT user permission
    and confirmation before execution. If the user has not clearly and unambiguously
    asked you to promote a specific branch, DO NOT call this tool.

    Args:
        branch_name: The name of the branch whose DB and filestore will become the new reference.
    """
    result = system_ops.promote_env(_get_settings(), branch_name)
    return (
        f"Branch '{result['branch']}' promoted to reference.\n"
        f"Template DB: {result['template_db']}\n"
        f"Dump: {result['dump']}\n"
        f"Filestore: {result['filestore']}"
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
        if env.get("odoo_image"):
            output += f"  Image: {env['odoo_image']}\n"
        if env.get("repo_url"):
            output += f"  Repo: {env['repo_url']}\n"
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
    lines = [f"Environment Status for '{branch_name}': {overall}"]
    for key in ("odoo", "db"):
        info = status[key]
        label = "Odoo" if key == "odoo" else "DB (shared)"
        line = f"{label}: {info['status']} (running: {info['running']})"
        if "cpu_percent" in info:
            line += f" | CPU: {info['cpu_percent']}% | RAM: {info['mem_usage_mb']} MB ({info['mem_percent']}%)"
        lines.append(line)
    return "\n".join(lines)


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
def pull_environment_repository(branch_name: str) -> str:
    """
    Pull latest changes from the remote repository for an environment
    and take appropriate action based on what changed.

    Analyzes changed files and automatically:
    - Upgrades modules if __manifest__.py version/data/assets changed, or security XML changed
    - Restarts the container if Python files changed
    - Does nothing if only XML/JS changed (--dev=xml handles hot reload)

    Args:
        branch_name: The name of the branch/environment to pull updates for.
    """
    result = env_ops.pull_environment(_get_settings(), branch_name)
    action = result["action"]

    if action == "none":
        return result["message"]

    lines = [result["message"]]
    if result.get("modules_installed"):
        lines.append(f"Installed: {', '.join(result['modules_installed'])}")
    if result.get("modules_upgraded"):
        lines.append(f"Upgraded: {', '.join(result['modules_upgraded'])}")
    lines.append(f"Changed files ({len(result.get('changed_files', []))}):")
    for f in result.get("changed_files", [])[:20]:
        lines.append(f"  - {f}")
    if len(result.get("changed_files", [])) > 20:
        lines.append(f"  ... and {len(result['changed_files']) - 20} more")
    return "\n".join(lines)


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
    exit_code = result['exit_code']
    modules_str = ', '.join(result['modules'])
    if exit_code == 0:
        return f"Success. Modules installed: {modules_str}. Exit code: 0."
    return (
        f"Error. Modules: {modules_str}. Exit code: {exit_code}. "
        f"Call get_environment_logs with branch_name='{branch_name}', tail=20 to investigate."
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
    exit_code = result['exit_code']
    modules_str = ', '.join(result['modules'])
    if exit_code == 0:
        return f"Success. Modules upgraded: {modules_str}. Exit code: 0."
    return (
        f"Error. Modules: {modules_str}. Exit code: {exit_code}. "
        f"Call get_environment_logs with branch_name='{branch_name}', tail=20 to investigate."
    )


def _run_init(settings: Settings, args: argparse.Namespace) -> None:
    result = system_ops.init_system(
        settings,
        dump_path=args.dump_path or None,
        version=args.version,
        force=args.force,
    )
    msg = f"System {result['status']}.\nTemplate DB: {result['template_db']}"
    if "restore_seconds" in result:
        msg += f"\nDB restore time: {result['restore_seconds']}s"
    print(msg)


def _run_reload_dump(settings: Settings, args: argparse.Namespace) -> None:
    result = system_ops.reload_template_db(
        settings,
        dump_path=args.dump_path or None,
    )
    msg = f"Template DB {result['status']}.\nTemplate DB: {result['template_db']}"
    if "restore_seconds" in result:
        msg += f"\nDB restore time: {result['restore_seconds']}s"
    print(msg)


def _run_generate_ref(settings: Settings, args: argparse.Namespace) -> None:
    odoo_image = args.odoo_image
    if not odoo_image:
        print("Error: --odoo-image is required for --generate-ref (e.g. --odoo-image odoo:17.0)")
        raise SystemExit(1)
    result = system_ops.generate_ref(
        settings,
        odoo_image=odoo_image,
        modules=args.modules,
    )
    msg = (
        f"Reference generated and system initialized.\n"
        f"Template DB: {result['template_db']}\n"
        f"Dump: {result['generated_dump']}\n"
        f"Filestore: {result['generated_filestore']}"
    )
    if "restore_seconds" in result:
        msg += f"\nDB restore time: {result['restore_seconds']}s"
    print(msg)


def _run_ref_up(settings: Settings, args: argparse.Namespace) -> None:
    odoo_image = args.odoo_image
    if not odoo_image:
        print("Error: --odoo-image is required for --ref-up (e.g. --odoo-image odoo:17.0)")
        raise SystemExit(1)
    result = system_ops.ref_up(settings, odoo_image=odoo_image)
    print(
        f"Reference editor started.\n"
        f"URL: {result['url']}\n"
        f"Container: {result['container']}\n"
        f"Database: {result['database']}\n"
        f"Filestore: {result['filestore']}\n\n"
        f"Make your changes in the browser, then run: flow --ref-down"
    )


def _run_ref_down(settings: Settings) -> None:
    result = system_ops.ref_down(settings)
    print(
        f"Reference editor stopped.\n"
        f"Dump saved: {result['dump']}\n"
        f"Filestore: {result['filestore']}\n"
        f"Template DB '{result['database']}' restored."
    )


def _run_promote(settings: Settings, args: argparse.Namespace) -> None:
    result = system_ops.promote_env(settings, branch_name=args.promote)
    print(
        f"Branch '{result['branch']}' promoted to reference.\n"
        f"Template DB: {result['template_db']}\n"
        f"Dump: {result['dump']}\n"
        f"Filestore: {result['filestore']}"
    )


def _run_destroy(settings: Settings) -> None:
    result = system_ops.destroy_system(settings)
    print(
        f"System {result['status']}.\n"
        f"Removed: {result['removed']}"
    )


def _run_call(argv: list[str]) -> None:
    """Execute an MCP tool from the CLI: flow call <tool> [args...]"""
    import inspect
    import json
    import sys

    if not argv or argv[0] == "--list":
        print("Registered tools:")
        for name in sorted(mcp._tool_manager._tools.keys()):
            tool_fn = mcp._tool_manager._tools[name].fn
            sig = inspect.signature(tool_fn)
            params = []
            for p in sig.parameters.values():
                if p.default is inspect.Parameter.empty:
                    params.append(f"<{p.name}>")
                else:
                    params.append(f"[{p.name}={p.default}]")
            print(f"  {name} {' '.join(params)}")
        return

    tool_name = argv[0]
    tool_argv = argv[1:]

    if tool_name not in mcp._tool_manager._tools:
        print(f"Unknown tool: {tool_name}")
        print(f"Available: {', '.join(sorted(mcp._tool_manager._tools.keys()))}")
        sys.exit(1)

    tool_fn = mcp._tool_manager._tools[tool_name].fn
    sig = inspect.signature(tool_fn)

    if tool_argv and tool_argv[0].startswith("{"):
        kwargs = json.loads(tool_argv[0])
    else:
        params = list(sig.parameters.values())
        kwargs = {}
        for i, value in enumerate(tool_argv):
            if i >= len(params):
                print(f"Warning: extra argument '{value}' ignored", file=sys.stderr)
                continue
            param = params[i]
            annotation = param.annotation
            if annotation is bool or (annotation is inspect.Parameter.empty and isinstance(param.default, bool)):
                kwargs[param.name] = value.lower() in ("true", "1", "yes")
            elif annotation is int or (annotation is inspect.Parameter.empty and isinstance(param.default, int)):
                kwargs[param.name] = int(value)
            elif annotation is float:
                kwargs[param.name] = float(value)
            else:
                kwargs[param.name] = value

    if not kwargs:
        required = [p for p in sig.parameters.values() if p.default is inspect.Parameter.empty]
        if required:
            parts = []
            for p in sig.parameters.values():
                if p.default is inspect.Parameter.empty:
                    parts.append(f"<{p.name}>")
                else:
                    parts.append(f"[{p.name}={p.default}]")
            print(f"Usage: flow call {tool_name} {' '.join(parts)}")
            return

    print(f"Calling: {tool_name}({kwargs})")
    print("-" * 60)
    logging.getLogger("flow").setLevel(logging.WARNING)
    try:
        result = tool_fn(**kwargs)
        print(result)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    """Entry point for the Flow MCP server."""
    parser = argparse.ArgumentParser(prog="flow", description="Flow — Odoo dev environment manager")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("call", help="Call an MCP tool: flow call <tool> [args...]").add_argument("call_args", nargs="*", default=[], help="Tool name and arguments")
    parser.add_argument("--init", action="store_true", help="Initialize shared infrastructure (network, DB, template)")
    parser.add_argument("--destroy", action="store_true", help="Destroy all shared infrastructure")
    parser.add_argument("--reload-dump", action="store_true", help="Drop and re-restore the template DB from dump (safe while server is running)")
    parser.add_argument("--generate-ref", action="store_true", help="Generate reference dump and filestore from a clean Odoo image (requires --odoo-image)")
    parser.add_argument("--ref-up", action="store_true", help="Start a ref editor: Odoo container working directly with the template DB and filestore (requires --odoo-image)")
    parser.add_argument("--ref-down", action="store_true", help="Stop the ref editor, dump the updated DB, restore template flag")
    parser.add_argument("--dump-path", default="", help="Path to DB dump file (for --init / --reload-dump)")
    parser.add_argument("--version", default="15.0", help="Odoo version (for --init, default 15.0)")
    parser.add_argument("--force", action="store_true", help="Force recreate template DB (for --init)")
    parser.add_argument("--odoo-image", default="", help="Docker image for Odoo (for --generate-ref, e.g. odoo:17.0)")
    parser.add_argument("--modules", default="base", help="Comma-separated modules to install during --generate-ref (default: base)")
    parser.add_argument("--promote", default="", metavar="BRANCH", help="Promote a branch environment to become the new reference (DB + filestore)")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    global _settings
    _settings = Settings.from_env()
    _settings.validate()

    if args.command == "call":
        _run_call(args.call_args)
        return

    if args.init:
        _run_init(_settings, args)
        return

    if args.destroy:
        _run_destroy(_settings)
        return

    if args.reload_dump:
        _run_reload_dump(_settings, args)
        return

    if args.generate_ref:
        _run_generate_ref(_settings, args)
        return

    if args.ref_up:
        _run_ref_up(_settings, args)
        return

    if args.ref_down:
        _run_ref_down(_settings)
        return

    if args.promote:
        _run_promote(_settings, args)
        return

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

        from flow.web_ui import mount_web_ui
        mount_web_ui(app, _get_settings, _busy)
        logger.info("Web UI available at http://%s:%d/", host, port)

        import uvicorn
        uvicorn.run(app, host=host, port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
