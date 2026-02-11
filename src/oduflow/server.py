import argparse
import functools
import logging
import os
import threading

from fastmcp import FastMCP

from oduflow.docker_ops import env_ops, odoo_ops, system_ops
from oduflow import git_ops
from oduflow.errors import BusyError, FlowError
from oduflow.settings import Settings

logger = logging.getLogger("oduflow")

mcp = FastMCP("Oduflow")
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
def create_environment(branch_name: str, repo_url: str, odoo_image: str, ref_name: str = "default") -> str:
    """
    Provision a new ephemeral Odoo environment for a specific branch.

    Args:
        branch_name: The name of the git branch (will be used for resource naming).
        repo_url: URL of the git repository to clone.
        odoo_image: Full Docker image name with tag (e.g. "odoo:17.0"). Use a pre-built image with all dependencies for faster startup.
        ref_name: Name of the reference profile to use as database template (default: "default").
    """
    result = env_ops.create_environment(_get_settings(), branch_name, repo_url, odoo_image, ref_name=ref_name)
    return (
        f"Environment provisioned successfully!\n"
        f"URL: {result['url']}\n"
        f"Odoo Container: {result['odoo_container']}\n"
        f"Database: {result['database']}\n"
        f"Workspace: {result['workspace']}\n"
        f"Ref: {ref_name}"
    )


@mcp.tool()
@handle_errors
@with_mutex
def promote_environment(branch_name: str, ref_name: str = "default") -> str:
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
        ref_name: Name of the reference profile to promote into (default: "default").
    """
    result = system_ops.promote_env(_get_settings(), branch_name, ref_name=ref_name)
    return (
        f"Branch '{result['branch']}' promoted to reference '{ref_name}'.\n"
        f"Template DB: {result['template_db']}\n"
        f"Dump: {result['dump']}\n"
        f"Filestore: {result['filestore']}"
    )


@mcp.tool()
@handle_errors
def list_refs() -> str:
    """List available reference profiles (database + filestore snapshots)."""
    refs = system_ops.list_refs(_get_settings())
    if not refs:
        return "No reference profiles found."
    output = "Reference profiles:\n"
    for r in refs:
        db_status = "loaded" if r["db_loaded"] else "not loaded"
        output += f"- {r['ref_name']}: DB={db_status}, SQL={r['has_sql']}, Filestore={r['has_filestore']}\n"
    return output


@mcp.tool()
@handle_errors
@with_mutex
def drop_ref(ref_name: str) -> str:
    """
    Drop a reference profile: remove its template database and files from disk.

    Args:
        ref_name: Name of the reference profile to drop.
    """
    result = system_ops.drop_ref(_get_settings(), ref_name)
    return f"Reference '{result['ref_name']}' dropped. Template DB '{result['template_db']}' removed."


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
        if env.get("ref_name") and env["ref_name"] != "default":
            output += f"  Ref: {env['ref_name']}\n"
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
    if result.get("output"):
        lines.append(f"\nOutput:\n{result['output']}")
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
    output = result.get('output', '')
    if exit_code == 0:
        return f"Success. Modules installed: {modules_str}. Exit code: 0.\n\nOutput:\n{output}"
    return (
        f"Error. Modules: {modules_str}. Exit code: {exit_code}.\n\nOutput:\n{output}"
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
    output = result.get('output', '')
    if exit_code == 0:
        return f"Success. Modules upgraded: {modules_str}. Exit code: 0.\n\nOutput:\n{output}"
    return (
        f"Error. Modules: {modules_str}. Exit code: {exit_code}.\n\nOutput:\n{output}"
    )


@mcp.tool()
@handle_errors
@with_mutex
def exec_in_environment(branch_name: str, command: str, user: str = "odoo") -> str:
    """
    Execute an arbitrary shell command inside the Odoo container for a specific branch.

    Args:
        branch_name: The name of the branch/environment.
        command: The shell command to execute (e.g. "ls /mnt/extra-addons", "python3 -c 'print(1)'").
        user: The OS user to run the command as (default "odoo"). Use "root" for privileged operations.
    """
    result = odoo_ops.exec_in_environment(_get_settings(), branch_name, command, user)
    exit_code = result["exit_code"]
    output = result.get("output", "")
    status = "Success" if exit_code == 0 else "Error"
    return f"{status}. Exit code: {exit_code}.\n\nOutput:\n{output}"


def _run_init(settings: Settings, version: str = "15.0", force: bool = False) -> None:
    result = system_ops.init_system(settings, version=version, force=force)
    print(f"System {result['status']}.")


def _run_reload_ref(settings: Settings, ref_name: str = "default", dump_path: str = "") -> None:
    result = system_ops.reload_ref(
        settings,
        ref_name=ref_name,
        dump_path=dump_path or None,
    )
    msg = f"Template DB {result['status']}.\nTemplate DB: {result['template_db']}"
    if "restore_seconds" in result:
        msg += f"\nDB restore time: {result['restore_seconds']}s"
    print(msg)


def _run_init_ref(settings: Settings, odoo_image: str, modules: str = "base", ref_name: str = "default", force: bool = False) -> None:
    result = system_ops.init_ref(
        settings,
        odoo_image=odoo_image,
        modules=modules,
        ref_name=ref_name,
        force=force,
    )
    msg = (
        f"Reference '{ref_name}' generated and loaded.\n"
        f"Template DB: {result['template_db']}\n"
        f"Dump: {result['generated_dump']}\n"
        f"Filestore: {result['generated_filestore']}"
    )
    if "restore_seconds" in result:
        msg += f"\nDB restore time: {result['restore_seconds']}s"
    print(msg)


def _run_ref_up(settings: Settings, odoo_image: str, ref_name: str = "default") -> None:
    result = system_ops.ref_up(settings, odoo_image=odoo_image, ref_name=ref_name)
    print(
        f"Reference editor started.\n"
        f"URL: {result['url']}\n"
        f"Container: {result['container']}\n"
        f"Database: {result['database']}\n"
        f"Filestore: {result['filestore']}\n\n"
        f"Make your changes in the browser, then run: oduflow ref-down"
    )


def _run_ref_down(settings: Settings, ref_name: str = "default") -> None:
    result = system_ops.ref_down(settings, ref_name=ref_name)
    print(
        f"Reference editor stopped.\n"
        f"Dump saved: {result['dump']}\n"
        f"Filestore: {result['filestore']}\n"
        f"Template DB '{result['database']}' restored."
    )


def _run_promote(settings: Settings, branch: str, ref_name: str = "default") -> None:
    result = system_ops.promote_env(settings, branch_name=branch, ref_name=ref_name)
    print(
        f"Branch '{result['branch']}' promoted to reference '{ref_name}'.\n"
        f"Template DB: {result['template_db']}\n"
        f"Dump: {result['dump']}\n"
        f"Filestore: {result['filestore']}"
    )


def _run_drop_ref(settings: Settings, ref_name: str) -> None:
    result = system_ops.drop_ref(settings, ref_name)
    print(f"Reference '{result['ref_name']}' dropped.\nTemplate DB '{result['template_db']}' removed.")


def _run_list_refs(settings: Settings) -> None:
    refs = system_ops.list_refs(settings)
    if not refs:
        print("No reference profiles found.")
        return
    print("Reference profiles:")
    for r in refs:
        db_status = "loaded" if r["db_loaded"] else "not loaded"
        print(f"  {r['ref_name']}: DB={db_status}, SQL={'yes' if r['has_sql'] else 'no'}, Filestore={'yes' if r['has_filestore'] else 'no'}")


def _run_destroy(settings: Settings) -> None:
    result = system_ops.destroy_system(settings)
    print(
        f"System {result['status']}.\n"
        f"Removed: {result['removed']}"
    )


def _run_call(argv: list[str]) -> None:
    """Execute an MCP tool from the CLI: oduflow call <tool> [args...]"""
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
            print(f"Usage: oduflow call {tool_name} {' '.join(parts)}")
            return

    print(f"Calling: {tool_name}({kwargs})")
    print("-" * 60)
    logging.getLogger("oduflow").setLevel(logging.WARNING)
    try:
        result = tool_fn(**kwargs)
        print(result)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    """Entry point for the Oduflow MCP server."""
    parser = argparse.ArgumentParser(prog="oduflow", description="Oduflow — Odoo dev environment manager",
                                     usage="oduflow [-h] <command> ...")
    sub = parser.add_subparsers(dest="command", title="commands", metavar="")

    p_init = sub.add_parser("init", help="Initialize shared infrastructure (network, DB, template)")
    p_init.add_argument("--version", default="15.0", help="Odoo version (default: 15.0)")
    p_init.add_argument("--force", action="store_true", help="Force recreate template DB")

    sub.add_parser("destroy", help="Destroy all shared infrastructure")

    p_reload = sub.add_parser("reload-ref", help="Drop and re-restore a template DB from ref profile")
    p_reload.add_argument("--ref-name", default="default", help="Reference profile name (default: default)")
    p_reload.add_argument("--dump-path", default="", help="Path to dump file (overrides ref profile path)")

    p_initref = sub.add_parser("init-ref", help="Generate reference dump and filestore from a clean Odoo image")
    p_initref.add_argument("--odoo-image", required=True, help="Docker image for Odoo (e.g. odoo:17.0)")
    p_initref.add_argument("--modules", default="base", help="Comma-separated modules to install (default: base)")
    p_initref.add_argument("--ref-name", default="default", help="Reference profile name (default: default)")
    p_initref.add_argument("--force", action="store_true", help="Overwrite existing dump.sql and filestore")

    p_refup = sub.add_parser("ref-up", help="Start a ref editor: Odoo working directly with template DB and filestore")
    p_refup.add_argument("--odoo-image", required=True, help="Docker image for Odoo (e.g. odoo:17.0)")
    p_refup.add_argument("--ref-name", default="default", help="Reference profile name (default: default)")

    p_refdown = sub.add_parser("ref-down", help="Stop the ref editor, dump the updated DB, restore template flag")
    p_refdown.add_argument("--ref-name", default="default", help="Reference profile name (default: default)")

    p_promote = sub.add_parser("promote", help="Promote a branch environment to become the new reference")
    p_promote.add_argument("branch", help="Branch name to promote")
    p_promote.add_argument("--ref-name", default="default", help="Reference profile name (default: default)")

    p_dropref = sub.add_parser("drop-ref", help="Drop a reference profile (template DB + files)")
    p_dropref.add_argument("ref_name", help="Reference profile name to drop")

    sub.add_parser("list-refs", help="List available reference profiles")

    p_call = sub.add_parser("call", help="Call an MCP tool: oduflow call <tool> [args...]")
    p_call.add_argument("call_args", nargs="*", default=[], help="Tool name and arguments")

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

    if args.command == "init":
        _run_init(_settings, version=args.version, force=args.force)
        return

    if args.command == "destroy":
        _run_destroy(_settings)
        return

    if args.command == "reload-ref":
        _run_reload_ref(_settings, ref_name=args.ref_name, dump_path=args.dump_path)
        return

    if args.command == "init-ref":
        _run_init_ref(_settings, odoo_image=args.odoo_image, modules=args.modules, ref_name=args.ref_name, force=args.force)
        return

    if args.command == "ref-up":
        _run_ref_up(_settings, odoo_image=args.odoo_image, ref_name=args.ref_name)
        return

    if args.command == "ref-down":
        _run_ref_down(_settings, ref_name=args.ref_name)
        return

    if args.command == "promote":
        _run_promote(_settings, branch=args.branch, ref_name=args.ref_name)
        return

    if args.command == "drop-ref":
        _run_drop_ref(_settings, ref_name=args.ref_name)
        return

    if args.command == "list-refs":
        _run_list_refs(_settings)
        return

    transport_str = os.getenv("ODUFLOW_TRANSPORT", "http")

    if transport_str == "http":
        from fastmcp.server.http import create_streamable_http_app

        host = os.getenv("ODUFLOW_HOST", "0.0.0.0")
        port = int(os.getenv("ODUFLOW_PORT", "8000"))

        auth_token = (os.getenv("ODUFLOW_AUTH_TOKEN") or "").strip()
        auth = None
        if auth_token:
            from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

            auth = StaticTokenVerifier(
                tokens={auth_token: {"client_id": "oduflow-user", "scopes": []}}
            )
            logger.info("HTTP Bearer token auth ENABLED")
        else:
            logger.warning("HTTP auth DISABLED (ODUFLOW_AUTH_TOKEN not set)")

        app = create_streamable_http_app(mcp, "/mcp", auth=auth)

        from oduflow.web_ui import mount_web_ui
        mount_web_ui(app, _get_settings, _busy)
        logger.info("Web UI available at http://%s:%d/", host, port)

        import uvicorn
        uvicorn.run(app, host=host, port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
