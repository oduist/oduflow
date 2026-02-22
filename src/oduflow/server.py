import argparse
import functools
import logging
import os
import threading

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from oduflow.docker_ops import env_ops, odoo_ops, service_ops, service_presets, system_ops
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
            raise ToolError(str(e))
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
def add_extra_repo(name: str, repo_url: str) -> str:
    """
    Clone an extra addons repository for use with environments.

    The repository is cloned as a bare repo to the shared repos directory.
    When creating an environment, reference it by name to mount it as
    additional addons (e.g., Odoo Enterprise).

    Args:
        name: Short name for the repo (e.g. "enterprise", "custom-themes").
        repo_url: Git URL of the repository (HTTPS or SSH).
    """
    from oduflow.extra_addons import clone_extra_repo
    result = clone_extra_repo(_get_settings(), name, repo_url)
    return f"Extra repo '{result['name']}' cloned successfully.\nPath: {result['path']}"


@mcp.tool()
@handle_errors
def list_extra_repos() -> str:
    """List all cloned extra addons repositories."""
    from oduflow.extra_addons import list_extra_repos as _list
    repos = _list(_get_settings())
    if not repos:
        return "No extra addons repositories found."
    lines = ["Extra addons repositories:"]
    for r in repos:
        branches = ", ".join(r["branches"][:10]) if r["branches"] else "(no branches)"
        lines.append(f"- {r['name']}: {r['repo_url']} [{branches}]")
    return "\n".join(lines)


@mcp.tool()
@handle_errors
@with_mutex
def delete_extra_repo(name: str) -> str:
    """
    Delete a cloned extra addons repository.

    Args:
        name: Name of the extra repo to delete.
    """
    from oduflow.extra_addons import delete_extra_repo as _delete
    _delete(_get_settings(), name)
    return f"Extra repo '{name}' deleted."

@mcp.tool()
@handle_errors
@with_mutex
def update_extra_repo(name: str) -> str:
    """
    Pull latest changes from the remote for an extra addons repository.

    Fetches all branches and prunes deleted remote refs.

    Args:
        name: Name of the extra repo to update (e.g. "enterprise").
    """
    from oduflow.extra_addons import fetch_extra_repo
    fetch_extra_repo(_get_settings(), name)
    return f"Extra repo '{name}' updated (fetched all branches)."

def _parse_extra_addons(raw: str, fallback_branch: str) -> dict[str, str]:
    result = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            name, branch = item.split(":", 1)
            result[name.strip()] = branch.strip()
        else:
            result[item] = fallback_branch
    return result


@mcp.tool()
@handle_errors
@with_mutex
def create_environment(
    branch_name: str,
    template_name: str = "",
    repo_url: str = "",
    odoo_image: str = "",
    extra_addons: str = "",
) -> str:
    """
    Provision a new ephemeral Odoo environment for a specific branch.

    Args:
        branch_name: The name of the git branch (will be used for resource naming).
        template_name: Name of the template profile to use as database template. Pass "none" to skip template and initialise Odoo from scratch with -i base. When a template is specified, repo_url and odoo_image are loaded from template metadata (but can be overridden).
        repo_url: URL of the git repository to clone. Optional when template_name is specified (loaded from template metadata).
        odoo_image: Full Docker image name with tag (e.g. "odoo:17.0"). Optional when template_name is specified (loaded from template metadata).
        extra_addons: Comma-separated list of extra addon repo names to mount (e.g. "enterprise,custom-themes"). Supports per-repo branches with colon syntax: "enterprise:18.0,custom-themes:main". If no branch is specified for a repo, defaults to the version extracted from odoo_image (e.g. "odoo:18.0" → branch "18.0").
    """
    import json
    settings = _get_settings()
    resolved_template: str | None
    if not template_name or template_name.lower() == "none":
        resolved_template = None
    else:
        resolved_template = template_name

    # Load metadata from template if available
    effective_repo_url = repo_url
    effective_odoo_image = odoo_image
    effective_git_user = ""
    if resolved_template:
        metadata_path = settings.get_template_metadata_path(resolved_template)
        if os.path.isfile(metadata_path):
            with open(metadata_path) as f:
                metadata = json.load(f)
            if not effective_repo_url:
                effective_repo_url = metadata.get("repo_url", "")
            if not effective_odoo_image:
                effective_odoo_image = metadata.get("odoo_image", "")
            if not effective_git_user:
                effective_git_user = metadata.get("git_user", "")
            if not extra_addons:
                raw_extra = metadata.get("extra_addons")
                if raw_extra:
                    from oduflow.docker_ops.env_ops import _normalize_extra_addons
                    _metadata_extra = _normalize_extra_addons(raw_extra, settings.default_branch)
                    if _metadata_extra:
                        extra_addons = ",".join(
                            f"{name}:{branch}" for name, branch in _metadata_extra.items()
                        )

    if not effective_repo_url:
        raise ValueError("repo_url is required (not found in template metadata either).")
    if not effective_odoo_image:
        raise ValueError("odoo_image is required (not found in template metadata either).")

    import re as _re
    _img_ver_match = _re.search(r"(\d+\.0)", effective_odoo_image)
    _extra_fallback = _img_ver_match.group(1) if _img_ver_match else settings.default_branch
    extra_dict = _parse_extra_addons(extra_addons, _extra_fallback) if extra_addons else {}
    result = env_ops.create_environment(
        settings, branch_name, effective_repo_url, effective_odoo_image,
        template_name=resolved_template,
        extra_addons=extra_dict or None,
        git_user=effective_git_user,
    )
    display_template = resolved_template if resolved_template is not None else "none (init from scratch)"
    lines = [
        "Environment provisioned successfully!",
        f"URL: {result['url']}",
        f"Odoo Container: {result['odoo_container']}",
        f"Database: {result['database']}",
        f"Workspace: {result['workspace']}",
        f"Template: {display_template}",
        f"Creation time: {result.get('elapsed_seconds', '?')}s",
    ]
    if extra_dict:
        extras_display = ", ".join(f"{name} ({branch})" for name, branch in extra_dict.items())
        lines.append(f"Extra Addons: {extras_display}")
    setup_logs = result.get("setup_logs", [])
    if setup_logs:
        lines.append("\n--- Setup Log ---")
        lines.extend(setup_logs)
    import re
    _ver_match = re.search(r"odoo[:/](\d+)(?:\.0)?", effective_odoo_image)
    if _ver_match:
        _odoo_ver = _ver_match.group(1)
        lines.append(f"\n⚠️ After creating an environment, immediately call get_odoo_development_guide(version=\"{_odoo_ver}\") to load Odoo {_odoo_ver} development standards and constraints. Do not wait for the user to ask — these guidelines must be loaded before writing any code.")
    return "\n".join(lines)


@mcp.tool()
@handle_errors
@with_mutex
def publish_as_template(branch_name: str, template_name: str) -> str:
    """
    Publish a branch environment to become the new template (DB + filestore).

    Replaces the template database and filestore with the data from the specified
    branch. If other environments use this template with overlay-mounted filestores,
    their filestore deltas will be reset to the new baseline — this is destructive
    for those environments. If no other environments share this template, the
    operation is safe.

    Requires EXPLICIT user permission and confirmation before execution.
    If the user has not clearly and unambiguously asked you to publish
    a specific branch, DO NOT call this tool.

    Args:
        branch_name: The name of the branch whose DB and filestore will become the new template.
        template_name: Name of the template profile to publish into.
    """
    settings = _get_settings()
    result = system_ops.publish_env_as_template(settings, branch_name, template_name=template_name)
    affected = result.get("affected_branches", [])
    lines = [
        f"Branch '{result['branch']}' saved as template '{template_name}'.",
        f"Template DB: {result['template_db']}",
        f"Dump: {result['dump']}",
        f"Filestore: {result['filestore']}",
    ]
    if affected:
        lines.append(f"⚠️ Reset filestore overlays for: {', '.join(affected)}")
    else:
        lines.append("No other environments were affected.")
    return "\n".join(lines)


@mcp.tool()
@handle_errors
def list_templates() -> str:
    """List available template profiles (database + filestore snapshots)."""
    templates = system_ops.list_templates(_get_settings())
    if not templates:
        return "No template profiles found."
    output = "Template profiles:\n"
    for r in templates:
        db_status = "loaded" if r["db_loaded"] else "not loaded"
        overlay_status = "overlay" if r.get("use_overlay") else "copy"
        fs_size = r.get("filestore_size_mb")
        dump_size = r.get("dump_size_mb")
        size_info = ""
        if fs_size is not None or dump_size is not None:
            fs_str = f"{fs_size:.0f} MB" if fs_size is not None else "?"
            dump_str = f"{dump_size:.0f} MB" if dump_size is not None else "?"
            size_info = f", Filestore size={fs_str}, Dump size={dump_str}"
        output += f"- {r['template_name']}: DB={db_status}, SQL={r['has_sql']}, Filestore={r['has_filestore']}, Mode={overlay_status}{size_info}\n"
    return output


@mcp.tool()
@handle_errors
@with_mutex
def import_template_from_odoo(
    odoo_url: str,
    master_pwd: str,
    db_name: str = "",
    template_name: str = "default",
) -> str:
    """
    Import a template from a running Odoo instance via its database manager API.

    Downloads a full backup (SQL + filestore), extracts it into the template
    directory, detects the Odoo version from the backup manifest, and loads
    the dump into PostgreSQL as a template database.

    Args:
        odoo_url: Base URL of the Odoo instance (e.g. "https://my-odoo.example.com").
        master_pwd: Odoo master password (database manager password).
        db_name: Name of the database to back up. If empty, auto-detected (fails if multiple DBs exist).
        template_name: Name of the template profile to create.
    """
    result = system_ops.import_from_odoo(
        _get_settings(),
        odoo_url=odoo_url,
        master_pwd=master_pwd,
        db_name=db_name,
        template_name=template_name,
    )
    lines = [
        f"Template '{result['template_name']}' imported successfully!",
        f"Source: {result['source_url']} (db: {result['source_db']})",
        f"Odoo version: {result['odoo_version']}",
        f"Odoo image: {result['odoo_image']}",
        f"Template DB: {result['template_db']}",
        f"Backup size: {result['zip_size_mb']} MB",
        f"DB restore time: {result['restore_seconds']}s",
    ]
    return "\n".join(lines)


@mcp.tool()
@handle_errors
def get_agent_skill() -> str:
    """Get the agent skill with instructions for AI coding agents on how to use Oduflow MCP tools."""
    import pathlib
    settings = _get_settings()
    # Support both new and legacy file names
    for name in ("agent_skill.md", "agent_guide.md"):
        skill_path = os.path.join(settings.home, "agent_guides", name)
        if os.path.isfile(skill_path):
            with open(skill_path, "r", encoding="utf-8") as f:
                return f.read()
    for name in ("agent_skill.md", "agent_guide.md"):
        bundled = pathlib.Path(__file__).resolve().parent / "templates" / "agent_guides" / name
        if bundled.is_file():
            return bundled.read_text(encoding="utf-8")
    return "Agent skill not found."


@mcp.tool()
@handle_errors
def get_odoo_development_guide(version: str) -> str:
    """
    Get Odoo development standards and constraints guide for a specific Odoo version.

    Args:
        version: Odoo version number (e.g. "17", "17.0", "18", "18.0"). Both "17" and "17.0" formats are accepted.
    """
    import pathlib
    normalized = version.split(".")[0]
    filename = f"odoo_{normalized}_guide.md"
    settings = _get_settings()
    guide_path = os.path.join(settings.home, "agent_guides", filename)
    if os.path.isfile(guide_path):
        with open(guide_path, "r", encoding="utf-8") as f:
            return f.read()
    bundled = pathlib.Path(__file__).resolve().parent / "templates" / "agent_guides" / filename
    if bundled.is_file():
        return bundled.read_text(encoding="utf-8")
    available = []
    guides_dir = os.path.join(settings.home, "agent_guides")
    if os.path.isdir(guides_dir):
        available = [f.replace("odoo_", "").replace("_guide.md", "") for f in os.listdir(guides_dir) if f.startswith("odoo_") and f.endswith("_guide.md")]
    if available:
        return f"No development guide found for Odoo {version}. Available versions: {', '.join(sorted(available))}"
    return f"No development guide found for Odoo {version}."


@mcp.tool()
@handle_errors
@with_mutex
def drop_template(template_name: str) -> str:
    """
    DANGEROUS: Drop a template profile — permanently removes its template database and files from disk.

    This is a destructive, irreversible operation. All environments that depend on this
    template will lose their baseline and cannot be recreated until a new template is set up.

    NEVER call this tool on your own initiative. Requires EXPLICIT user permission
    and confirmation before execution. If the user has not clearly and unambiguously
    asked you to drop a specific template, DO NOT call this tool.

    Args:
        template_name: Name of the template profile to drop.
    """
    result = system_ops.drop_template(_get_settings(), template_name)
    return f"Template '{result['template_name']}' dropped. Template DB '{result['template_db']}' removed."


@mcp.tool()
@handle_errors
@with_mutex
def delete_environment(branch_name: str) -> str:
    """
    Stop and remove all resources associated with an Odoo environment.

    Args:
        branch_name: The name of the branch to tear down.
    """
    warnings = env_ops.delete_environment(_get_settings(), branch_name)
    result = f"Environment for branch '{branch_name}' has been torn down."
    if warnings:
        result += "\n\n⚠️ Warnings:\n" + "\n".join(f"- {w}" for w in warnings)
    return result


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
        if env.get("db_name"):
            output += f"  Database: {env['db_name']}\n"
        if env.get("odoo_image"):
            output += f"  Image: {env['odoo_image']}\n"
        if env.get("repo_url"):
            output += f"  Repo: {env['repo_url']}\n"
        if env.get("template_name"):
            output += f"  Template: {env['template_name']}\n"
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
@with_mutex
def rebuild_environment(branch_name: str) -> str:
    """
    Rebuild the Odoo container for an environment without losing the database or filestore.

    Use this when the container is broken (e.g. packages were accidentally removed,
    system files corrupted) and you need a fresh container from the same image,
    reconnected to the existing database and filestore.

    Args:
        branch_name: The name of the branch/environment to rebuild.
    """
    result = env_ops.rebuild_environment(_get_settings(), branch_name)
    lines = [
        "Environment rebuilt successfully!",
        f"URL: {result['url']}",
        f"Odoo Container: {result['odoo_container']}",
        f"Database: {result['database']}",
        f"Workspace: {result['workspace']}",
    ]
    setup_logs = result.get("setup_logs", [])
    if setup_logs:
        lines.append("\n--- Setup Log ---")
        lines.extend(setup_logs)
    return "\n".join(lines)


@mcp.tool()
@handle_errors
def get_environment_info(branch_name: str) -> str:
    """
    Get comprehensive information about an environment for a specific branch.

    Returns database name, URL, repository, image, template, extra addons,
    workspace path, container status, and CPU/RAM stats.

    Args:
        branch_name: The name of the branch/environment to check.
    """
    info = env_ops.get_environment_info(_get_settings(), branch_name)
    overall = "All containers running" if info["all_running"] else "Some containers not running"
    lines = [f"Environment Info for '{branch_name}': {overall}"]
    lines.append(f"Database: {info['db_name']}")
    if info.get("url"):
        lines.append(f"URL: {info['url']}")
    if info.get("repo_url"):
        lines.append(f"Repo: {info['repo_url']}")
    if info.get("odoo_image"):
        lines.append(f"Image: {info['odoo_image']}")
    if info.get("template_name"):
        lines.append(f"Template: {info['template_name']}")
    if info.get("extra_addons"):
        addons = ", ".join(f"{k}:{v}" for k, v in info["extra_addons"].items())
        lines.append(f"Extra addons: {addons}")
    if info.get("workspace"):
        lines.append(f"Workspace: {info['workspace']}")
    for key in ("odoo", "db"):
        cinfo = info[key]
        label = "Odoo" if key == "odoo" else "DB (shared)"
        line = f"{label}: {cinfo['status']}"
        if "cpu_percent" in cinfo:
            line += f" | CPU: {cinfo['cpu_percent']}% | RAM: {cinfo['mem_usage_mb']} MB ({cinfo['mem_percent']}%)"
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
def sync_environment(branch_name: str) -> str:
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
        env_ops.restart_environment(_get_settings(), branch_name)
        return f"Success. Modules installed: {modules_str}. Container restarted. Exit code: 0.\n\nOutput:\n{output}"
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


@mcp.tool()
@handle_errors
@with_mutex
def run_db_query(branch_name: str, query: str, output_format: str = "csv") -> str:
    """
    Execute a SQL query against the environment's PostgreSQL database.

    Args:
        branch_name: The name of the branch/environment.
        query: SQL query to execute (e.g. "SELECT id, name FROM res_partner LIMIT 10").
        output_format: "csv" (default, compact for agent consumption) or "human" (pretty table — use when relaying results to the user).
    """
    result = odoo_ops.run_db_query(_get_settings(), branch_name, query, output_format)
    return result["output"]


@mcp.tool()
@handle_errors
@with_mutex
def create_service(name: str, image: str, port: int, hostname: str = "", env_vars: str = "") -> str:
    """
    Create a managed auxiliary service container (e.g. Redis, Meilisearch).

    Args:
        name: Short name for the service (e.g. "redis", "meilisearch").
        image: Docker image with tag (e.g. "redis:7", "getmeili/meilisearch:v1.6").
        port: The container port the service listens on.
        hostname: Custom hostname for traefik routing (optional, traefik mode only).
        env_vars: Comma-separated KEY=VALUE pairs (e.g. "MEILI_MASTER_KEY=abc,MEILI_ENV=production").
    """
    settings = _get_settings()
    parsed_env = None
    if env_vars:
        parsed_env = dict(item.split("=", 1) for item in env_vars.split(",") if "=" in item)
    result = service_ops.create_service(
        settings, name, image, port,
        hostname=hostname or None,
        env_vars=parsed_env,
    )
    return (
        f"Service created successfully!\n"
        f"Name: {result['name']}\n"
        f"Container: {result['container_name']}\n"
        f"Image: {result['image']}\n"
        f"URL: {result['url']}"
    )


@mcp.tool()
@handle_errors
@with_mutex
def update_service(name: str) -> str:
    """
    Update a managed auxiliary service container by pulling the latest image and re-creating the container with the same settings.

    Args:
        name: The name of the service to update (e.g. "redis", "meilisearch").
    """
    result = service_ops.update_service(_get_settings(), name)
    status = "Image updated (new image pulled, container recreated)" if result.get("image_updated") else "Already up-to-date (no changes)"
    digest_short = (result.get("new_digest") or "")[:19]
    return (
        f"Service updated successfully!\n"
        f"Status: {status}\n"
        f"Name: {result['name']}\n"
        f"Container: {result['container_name']}\n"
        f"Image: {result['image']}\n"
        f"Digest: {digest_short}\n"
        f"URL: {result['url']}"
    )


@mcp.tool()
@handle_errors
@with_mutex
def delete_service(name: str) -> str:
    """
    Stop and remove a managed auxiliary service container.

    Args:
        name: The name of the service to delete.
    """
    result = service_ops.delete_service(_get_settings(), name)
    return f"Service '{result['name']}' deleted. Container '{result['container_name']}' removed."


@mcp.tool()
@handle_errors
def list_service_presets() -> str:
    """List saved service presets (configurations that can be restored)."""
    presets = service_presets.list_presets(_get_settings())
    if not presets:
        return "No service presets saved."
    output = "Saved Service Presets:\n"
    for p in presets:
        env_str = ", ".join(f"{k}={v}" for k, v in p["env_vars"].items()) if p.get("env_vars") else ""
        output += f"- {p['name']}: image={p['image']}, port={p['port']}"
        if p.get("hostname"):
            output += f", hostname={p['hostname']}"
        if env_str:
            output += f", env=[{env_str}]"
        output += "\n"
    return output


@mcp.tool()
@handle_errors
@with_mutex
def restore_service(name: str) -> str:
    """
    Restore a service from a saved preset. Recreates the service container with the same configuration.

    Args:
        name: The name of the saved service preset to restore.
    """
    settings = _get_settings()
    preset = service_presets.get_preset(settings, name)
    result = service_ops.create_service(
        settings,
        name=preset["name"],
        image=preset["image"],
        port=preset["port"],
        hostname=preset.get("hostname") or None,
        env_vars=preset.get("env_vars") or None,
    )
    return (
        f"Service restored from preset!\n"
        f"Name: {result['name']}\n"
        f"Container: {result['container_name']}\n"
        f"Image: {result['image']}\n"
        f"URL: {result['url']}"
    )


@mcp.tool()
@handle_errors
def delete_service_preset(name: str) -> str:
    """
    Remove a saved service preset.

    Args:
        name: The name of the service preset to delete.
    """
    service_presets.delete_preset(_get_settings(), name)
    return f"Service preset '{name}' deleted."


@mcp.tool()
@handle_errors
def list_services() -> str:
    """List all managed auxiliary service containers."""
    services = service_ops.list_services(_get_settings())
    if not services:
        return "No active services found."
    output = "Active Services:\n"
    for svc in services:
        output += f"- {svc['name']} ({svc['container_name']}): {svc['status']}\n"
        output += f"  Image: {svc['image']}\n"
        if svc.get("port"):
            output += f"  Port: {svc['port']}\n"
        if svc.get("url"):
            output += f"  URL: {svc['url']}\n"
        if svc.get("env_vars"):
            env_str = ", ".join(f"{k}={v}" for k, v in svc["env_vars"].items())
            output += f"  Env: {env_str}\n"
    return output


@mcp.tool()
@handle_errors
def get_service_logs(name: str, n_lines: int = 100) -> str:
    """
    Get logs from a managed auxiliary service container.

    Args:
        name: The name of the service.
        n_lines: Number of recent log lines to retrieve (default 100).
    """
    output = service_ops.get_service_logs(_get_settings(), name, n_lines)
    return f"Recent logs for service '{name}':\n\n{output}"


def _run_init(settings: Settings) -> None:
    _copy_bundled_configs()
    result = system_ops.init_system(settings)
    print(f"System {result['status']}.")


def _copy_bundled_configs() -> None:
    import pathlib, shutil
    etc_oduflow = pathlib.Path("/etc/oduflow")
    try:
        os.makedirs(etc_oduflow, exist_ok=True)
    except PermissionError:
        logger.warning("Cannot create %s (permission denied), skipping config copy", etc_oduflow)
        return
    bundled_dir = pathlib.Path(__file__).resolve().parent / "templates"
    for conf_name in ("postgresql.conf", "odoo.conf"):
        dest = etc_oduflow / conf_name
        if not dest.is_file():
            bundled = bundled_dir / conf_name
            if bundled.is_file():
                try:
                    shutil.copy2(str(bundled), str(dest))
                    print(f"  Config: {dest}")
                except PermissionError:
                    logger.warning("Cannot write %s (permission denied)", dest)


def _run_init_instance(settings: Settings, *, update_guides: bool = False) -> None:
    # Initialize per-instance directories
    import os
    os.makedirs(settings.workspaces_dir, exist_ok=True)
    os.makedirs(os.path.join(settings.home, "templates"), exist_ok=True)

    _copy_bundled_configs()

    # Copy bundled agent guides
    import pathlib, shutil
    agent_guides_dest = os.path.join(settings.home, "agent_guides")
    os.makedirs(agent_guides_dest, exist_ok=True)
    bundled_guides_dir = pathlib.Path(__file__).resolve().parent / "templates" / "agent_guides"
    if bundled_guides_dir.is_dir():
        for guide_file in bundled_guides_dir.iterdir():
            if guide_file.is_file() and guide_file.suffix == ".md":
                dest_file = os.path.join(agent_guides_dest, guide_file.name)
                if update_guides or not os.path.isfile(dest_file):
                    shutil.copy2(str(guide_file), dest_file)
                    action = "Updated" if update_guides else "Created"
                    print(f"  Agent guide {action}: {dest_file}")

    if settings.routing_mode == "traefik" and settings.base_domain:
        dynamic_dir = "/etc/oduflow/traefik"
        os.makedirs(dynamic_dir, exist_ok=True)
        instance_cfg = (
            "http:\n"
            "  routers:\n"
            "    oduflow-server-{iid}:\n"
            "      rule: \"Host(`{host}`)\"\n"
            "      entryPoints: [websecure]\n"
            "      service: oduflow-server-{iid}\n"
            "      tls:\n"
            "        certResolver: le\n"
            "  services:\n"
            "    oduflow-server-{iid}:\n"
            "      loadBalancer:\n"
            "        servers:\n"
            "          - url: \"http://host.docker.internal:{port}\"\n"
        ).format(
            iid=settings.instance_id,
            host=settings.base_domain,
            port=settings.flow_server_port,
        )
        cfg_path = os.path.join(dynamic_dir, f"instance-{settings.instance_id}.yml")
        with open(cfg_path, "w") as f:
            f.write(instance_cfg)
        logger.info("Wrote Traefik dynamic config: %s", cfg_path)

    print(f"Instance {settings.instance_id} initialized.")
    print(f"  Workspaces: {settings.workspaces_dir}")
    print(f"  Templates: {os.path.join(settings.home, 'templates')}")


def _run_reload_template(settings: Settings, template_name: str, dump_path: str = "") -> None:
    result = system_ops.reload_template(
        settings,
        template_name=template_name,
        dump_path=dump_path or None,
    )
    msg = f"Template DB {result['status']}.\nTemplate DB: {result['template_db']}"
    if "restore_seconds" in result:
        msg += f"\nDB restore time: {result['restore_seconds']}s"
    print(msg)


def _run_init_template(settings: Settings, odoo_image: str, modules: str = "base", template_name: str = "", force: bool = False) -> None:
    result = system_ops.init_template(
        settings,
        odoo_image=odoo_image,
        modules=modules,
        template_name=template_name,
        force=force,
    )
    msg = (
        f"Template '{template_name}' generated and loaded.\n"
        f"Template DB: {result['template_db']}\n"
        f"Dump: {result['generated_dump']}\n"
        f"Filestore: {result['generated_filestore']}"
    )
    if "restore_seconds" in result:
        msg += f"\nDB restore time: {result['restore_seconds']}s"
    print(msg)


def _run_template_up(settings: Settings, odoo_image: str, template_name: str = "") -> None:
    result = system_ops.template_up(settings, odoo_image=odoo_image, template_name=template_name)
    print(
        f"Template editor started.\n"
        f"URL: {result['url']}\n"
        f"Container: {result['container']}\n"
        f"Database: {result['database']}\n"
        f"Filestore: {result['filestore']}\n\n"
        f"Make your changes in the browser, then run: oduflow template-down"
    )


def _run_template_down(settings: Settings, template_name: str = "") -> None:
    result = system_ops.template_down(settings, template_name=template_name)
    print(
        f"Template editor stopped.\n"
        f"Dump saved: {result['dump']}\n"
        f"Filestore: {result['filestore']}\n"
        f"Template DB '{result['database']}' restored."
    )


def _run_template_from_env(settings: Settings, branch: str, template_name: str = "") -> None:
    result = system_ops.publish_env_as_template(settings, branch_name=branch, template_name=template_name)
    print(
        f"Branch '{result['branch']}' saved as template '{template_name}'.\n"
        f"Template DB: {result['template_db']}\n"
        f"Dump: {result['dump']}\n"
        f"Filestore: {result['filestore']}"
    )


def _run_drop_template(settings: Settings, template_name: str) -> None:
    result = system_ops.drop_template(settings, template_name)
    print(f"Template '{result['template_name']}' dropped.\nTemplate DB '{result['template_db']}' removed.")


def _run_import_template(settings: Settings, odoo_url: str, master_pwd: str, db_name: str = "", template_name: str = "") -> None:
    result = system_ops.import_from_odoo(settings, odoo_url=odoo_url, master_pwd=master_pwd, db_name=db_name, template_name=template_name)
    print(
        f"Template '{result['template_name']}' imported successfully!\n"
        f"Source: {result['source_url']} (db: {result['source_db']})\n"
        f"Odoo version: {result['odoo_version']}\n"
        f"Odoo image: {result['odoo_image']}\n"
        f"Template DB: {result['template_db']}\n"
        f"Backup size: {result['zip_size_mb']} MB\n"
        f"DB restore time: {result['restore_seconds']}s"
    )


def _run_list_templates(settings: Settings) -> None:
    templates = system_ops.list_templates(settings)
    if not templates:
        print("No template profiles found.")
        return
    print("Template profiles:")
    for r in templates:
        db_status = "loaded" if r["db_loaded"] else "not loaded"
        overlay_status = "overlay" if r.get("use_overlay") else "copy"
        fs_size = r.get("filestore_size_mb")
        dump_size = r.get("dump_size_mb")
        size_info = ""
        if fs_size is not None or dump_size is not None:
            fs_str = f"{fs_size:.0f} MB" if fs_size is not None else "?"
            dump_str = f"{dump_size:.0f} MB" if dump_size is not None else "?"
            size_info = f", Filestore size={fs_str}, Dump size={dump_str}"
        print(f"  {r['template_name']}: DB={db_status}, SQL={'yes' if r['has_sql'] else 'no'}, Filestore={'yes' if r['has_filestore'] else 'no'}, Mode={overlay_status}{size_info}")


def _run_list_services(settings: Settings) -> None:
    from oduflow.docker_ops import service_ops
    services = service_ops.list_services(settings)
    if not services:
        print("No active services found.")
        return
    print("Active services:")
    for svc in services:
        status_icon = "●" if svc["status"] == "running" else "○"
        print(f"  {status_icon} {svc['name']} ({svc['container_name']}): {svc['status']}")
        print(f"    Image: {svc['image']}")
        if svc.get("port"):
            print(f"    Port: {svc['port']}")
        if svc.get("url"):
            print(f"    URL: {svc['url']}")
        if svc.get("env_vars"):
            env_str = ", ".join(f"{k}={v}" for k, v in svc["env_vars"].items())
            print(f"    Env: {env_str}")


def _run_cleanup(settings: Settings, dry_run: bool = True) -> None:
    result = system_ops.cleanup_orphans(settings, dry_run=dry_run)
    mode = "DRY RUN" if result["dry_run"] else "CLEANUP"
    dbs = result["orphan_databases"]
    workspaces = result["orphan_workspaces"]
    ports = result["orphan_ports"]

    if not dbs and not workspaces and not ports:
        print(f"[{mode}] No orphaned resources found.")
        return

    print(f"[{mode}] Orphaned resources:")
    if dbs:
        print(f"  Databases ({len(dbs)}):")
        for db in dbs:
            print(f"    - {db}")
    if workspaces:
        print(f"  Workspaces ({len(workspaces)}):")
        for ws in workspaces:
            print(f"    - {ws}")
    if ports:
        print(f"  Port registry entries ({len(ports)}):")
        for p in ports:
            print(f"    - {p}")

    total = len(dbs) + len(workspaces) + len(ports)
    if result["dry_run"]:
        print(f"\n  {total} resource(s) would be removed. Run with --force to apply.")
    else:
        print(f"\n  {total} resource(s) removed.")


def _run_destroy(settings: Settings) -> None:
    result = system_ops.destroy_system(settings)
    print(
        f"System {result['status']}.\n"
        f"Removed: {result['removed']}"
    )


def _print_tools(verbose: bool = False) -> None:
    import inspect

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
        if verbose:
            desc = (tool_fn.__doc__ or "").strip().split("\n")[0]
            if desc:
                print(f"    {desc}")


def _run_call(argv: list[str]) -> None:
    """Execute an MCP tool from the CLI: oduflow call <tool> [args...]"""
    import inspect
    import json
    import sys

    if not argv:
        _print_tools(verbose=False)
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
                                     usage="oduflow [-h] [--env ENV] <command> ...")
    parser.add_argument("--env", default=None, metavar="FILE", help="Path to .env file (default: .env in current dir)")
    sub = parser.add_subparsers(dest="command", title="commands", metavar="")

    p_init = sub.add_parser("init", help="Initialize shared infrastructure (network, DB)")
    p_init.add_argument("--license", default="", metavar="FILE", dest="license_file",
                        help="Path to license.key file to install to /etc/oduflow/license.key")

    p_init_inst = sub.add_parser("init-instance", help="Initialize per-instance directories (workspaces, templates)")
    p_init_inst.add_argument("--update-guides", action="store_true", default=False,
                             help="Overwrite existing agent guides with the latest bundled versions")

    sub.add_parser("destroy", help="Destroy all shared infrastructure")

    p_reload = sub.add_parser("reload-template", help="Drop and re-restore a template DB from template profile")
    p_reload.add_argument("template_name", help="Template profile name")
    p_reload.add_argument("--dump-path", default="", help="Path to dump file (overrides template profile path)")

    p_init_tpl = sub.add_parser("init-template", help="Generate template dump and filestore from a clean Odoo image")
    p_init_tpl.add_argument("--odoo-image", required=True, help="Docker image for Odoo (e.g. odoo:17.0)")
    p_init_tpl.add_argument("--modules", default="base", help="Comma-separated modules to install (default: base)")
    p_init_tpl.add_argument("--template-name", required=True, help="Template profile name")
    p_init_tpl.add_argument("--force", action="store_true", help="Overwrite existing dump.sql and filestore")

    p_tpl_up = sub.add_parser("template-up", help="Start a template editor: Odoo working directly with template DB and filestore")
    p_tpl_up.add_argument("--odoo-image", required=True, help="Docker image for Odoo (e.g. odoo:17.0)")
    p_tpl_up.add_argument("--template-name", required=True, help="Template profile name")

    p_tpl_down = sub.add_parser("template-down", help="Stop the template editor, dump the updated DB, restore template flag")
    p_tpl_down.add_argument("--template-name", required=True, help="Template profile name")

    p_tfe = sub.add_parser("template-from-env", help="Save a branch environment as the new template")
    p_tfe.add_argument("branch", help="Branch name to use as template source")
    p_tfe.add_argument("--template-name", required=True, help="Template profile name")

    p_drop_tpl = sub.add_parser("drop-template", help="Drop a template profile (template DB + files)")
    p_drop_tpl.add_argument("template_name", help="Template profile name to drop")

    p_import = sub.add_parser("import-template", help="Import a template from a running Odoo instance")
    p_import.add_argument("odoo_url", help="Base URL of the Odoo instance (e.g. https://my-odoo.example.com)")
    p_import.add_argument("master_pwd", help="Odoo master password")
    p_import.add_argument("--db-name", default="", help="Database name (auto-detected if only one exists)")
    p_import.add_argument("--template-name", required=True, help="Template profile name")

    sub.add_parser("list-templates", help="List available template profiles")

    sub.add_parser("list-services", help="List managed auxiliary service containers")

    p_cleanup = sub.add_parser("cleanup", help="Find and remove orphaned databases, workspaces, and port entries")
    p_cleanup.add_argument("--dry-run", action="store_true", default=False,
                           help="Only show what would be removed (default behavior)")
    p_cleanup.add_argument("--force", action="store_true", default=False,
                           help="Actually remove orphaned resources")

    p_list = sub.add_parser("list", help="List registered MCP tools")
    p_list.add_argument("--verbose", "-v", action="store_true", help="Show tool descriptions")

    p_call = sub.add_parser("call", help="Call an MCP tool: oduflow call <tool> [args...]")
    p_call.add_argument("call_args", nargs="*", default=[], help="Tool name and arguments")

    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv(args.env)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logging.getLogger("docket").setLevel(logging.WARNING)

    global _settings
    _settings = Settings.from_env()
    _settings.validate()

    if args.command == "list":
        _print_tools(verbose=args.verbose)
        return

    if args.command == "call":
        _run_call(args.call_args)
        return

    if args.command == "init":
        _run_init(_settings)
        if getattr(args, "license_file", ""):
            from oduflow.licensing import install_license
            info = install_license(args.license_file)
            print(f"License installed: {info.label}")
        return

    if args.command == "init-instance":
        _run_init_instance(_settings, update_guides=args.update_guides)
        return

    if args.command == "destroy":
        _run_destroy(_settings)
        return

    if args.command == "reload-template":
        _run_reload_template(_settings, template_name=args.template_name, dump_path=args.dump_path)
        return

    if args.command == "init-template":
        _run_init_template(_settings, odoo_image=args.odoo_image, modules=args.modules, template_name=args.template_name, force=args.force)
        return

    if args.command == "template-up":
        _run_template_up(_settings, odoo_image=args.odoo_image, template_name=args.template_name)
        return

    if args.command == "template-down":
        _run_template_down(_settings, template_name=args.template_name)
        return

    if args.command == "template-from-env":
        _run_template_from_env(_settings, branch=args.branch, template_name=args.template_name)
        return

    if args.command == "drop-template":
        _run_drop_template(_settings, template_name=args.template_name)
        return

    if args.command == "import-template":
        _run_import_template(_settings, odoo_url=args.odoo_url, master_pwd=args.master_pwd, db_name=args.db_name, template_name=args.template_name)
        return

    if args.command == "list-templates":
        _run_list_templates(_settings)
        return

    if args.command == "list-services":
        _run_list_services(_settings)
        return

    if args.command == "cleanup":
        _run_cleanup(_settings, dry_run=not args.force)
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

        settings = _get_settings()
        app = create_streamable_http_app(mcp, "/mcp", auth=auth, stateless_http=settings.stateless_http)
        if settings.stateless_http:
            logger.info("Stateless HTTP mode ENABLED (no session tracking)")

        from oduflow.git_ops import ensure_credential_helper
        ensure_credential_helper()

        from oduflow.web_ui import mount_web_ui
        mount_web_ui(app, _get_settings, _busy)
        logger.info("Web UI available at http://%s:%d/", host, port)

        import uvicorn
        uvicorn.run(app, host=host, port=port, ws="websockets-sansio")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
