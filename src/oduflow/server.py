from __future__ import annotations

import argparse
import functools
import logging
import os
import re
import sys

from fastmcp import FastMCP, Context
from fastmcp.exceptions import ToolError

from oduflow.docker_ops import (
    env_ops,
    odoo_ops,
    service_ops,
    service_presets,
    system_ops,
)
from oduflow import git_ops
from oduflow.errors import FlowError
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings, find_toml

logger = logging.getLogger("oduflow")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

mcp = FastMCP("Oduflow", stateless_http=True)
_locks = LockManager()
_settings: Settings | None = None


def _get_settings() -> Settings:
    global _settings
    if _settings is None:
        toml_path = find_toml()
        _settings = Settings.from_toml(toml_path)
        _settings.validate()
    return _settings


def _resolve_team(ctx: Context | None) -> TeamSettings:
    """Resolve the team from MCP Context.

    Priority: auth token → Host header hostname → single-team → team "1".
    """
    settings = _get_settings()
    # 1. Token-based: client_id set by auth provider
    team_id = ctx.client_id if ctx and ctx.client_id else None
    if team_id and team_id in settings.teams:
        return settings.teams[team_id]
    # 2. Hostname-based: match Host header against team hostnames
    if ctx:
        try:
            from fastmcp.server.dependencies import get_http_request

            request = get_http_request()
            host = request.headers.get("host", "")
            team = settings.get_team_by_hostname(host)
            if team:
                return team
        except Exception:
            pass
    # 3. Fallback: single team or default team "1"
    if len(settings.teams) == 1:
        return next(iter(settings.teams.values()))
    return settings.get_team("1")


# -- Decorators --


def handle_errors(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            result = fn(*args, **kwargs)
            preview = (
                (result[:200] + "...")
                if isinstance(result, str) and len(result) > 200
                else result
            )
            logger.info("[%s] -> %s", fn.__name__, preview)
            return result
        except FlowError as e:
            logger.error("[%s] Error: %s", fn.__name__, e)
            raise ToolError(str(e))

    return wrapper


def with_branch_lock(fn):
    """Acquire a per-branch lock before executing the tool function."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        branch_name = kwargs.get("branch_name") or (args[0] if args else None)
        if not branch_name:
            raise ToolError("branch_name is required")
        _locks.acquire_branch(branch_name)
        try:
            return fn(*args, **kwargs)
        finally:
            _locks.release_branch(branch_name)

    return wrapper


def with_team_lock(fn):
    """Acquire a per-team lock before executing the tool function."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        ctx = kwargs.get("ctx")
        team = _resolve_team(ctx)
        _locks.acquire_team(team.team_id)
        try:
            return fn(*args, **kwargs)
        finally:
            _locks.release_team(team.team_id)

    return wrapper


# =============================================================================
# MCP Tools — Git credentials
# =============================================================================


@mcp.tool()
@handle_errors
@with_team_lock
def setup_repo_auth(repo_url: str, ctx: Context = None) -> str:
    """
    Cache git credentials for a private repository.

    Accepts a URL with embedded credentials, stores them in git credential store,
    and verifies access with a test clone. After this, create_environment can clone
    the repo without authentication prompts.

    Args:
        repo_url: Repository URL with credentials, e.g. https://user:PAT@github.com/owner/repo.git
    """
    team = _resolve_team(ctx)
    result = git_ops.setup_repo_auth(repo_url, cred_file=team.git_credentials_file())
    return (
        f"Repository authentication configured.\n"
        f"Host: {result['host']}\n"
        f"Repo URL (clean): {result['repo_url']}\n"
        f"Status: {result['status']}\n\n"
        f"You can now use create_environment with the clean URL (without credentials)."
    )


# =============================================================================
# MCP Tools — Extra addons repos
# =============================================================================


@mcp.tool()
@handle_errors
@with_team_lock
def add_extra_repo(name: str, repo_url: str, ctx: Context = None) -> str:
    """
    Clone an extra addons repository for use with environments.

    The repository is cloned as a bare repo to the shared repos directory.
    When creating an environment, reference it by name to mount it as
    additional addons (e.g., Odoo Enterprise).

    Args:
        name: Short name for the repo (e.g. "enterprise", "custom-themes").
        repo_url: HTTPS URL of the repository (e.g. https://github.com/owner/repo.git).
    """
    from oduflow.extra_addons import clone_extra_repo

    git_ops.validate_repo_url(repo_url)
    team = _resolve_team(ctx)
    result = clone_extra_repo(team, name, repo_url)
    return f"Extra repo '{result['name']}' cloned successfully.\nPath: {result['path']}"


@mcp.tool()
@handle_errors
def list_extra_repos(ctx: Context = None) -> str:
    """List all cloned extra addons repositories."""
    from oduflow.extra_addons import list_extra_repos as _list

    team = _resolve_team(ctx)
    repos = _list(team)
    if not repos:
        return "No extra addons repositories found."
    lines = ["Extra addons repositories:"]
    for r in repos:
        branches = ", ".join(r["branches"][:10]) if r["branches"] else "(no branches)"
        lines.append(f"- {r['name']}: {r['repo_url']} [{branches}]")
    return "\n".join(lines)


@mcp.tool()
@handle_errors
@with_team_lock
def delete_extra_repo(name: str, ctx: Context = None) -> str:
    """
    Delete a cloned extra addons repository.

    Args:
        name: Name of the extra repo to delete.
    """
    from oduflow.extra_addons import delete_extra_repo as _delete

    settings = _get_settings()
    team = _resolve_team(ctx)
    _delete(settings, team, name)
    return f"Extra repo '{name}' deleted."


@mcp.tool()
@handle_errors
@with_team_lock
def update_extra_repo(name: str, ctx: Context = None) -> str:
    """
    Pull latest changes from the remote for an extra addons repository.

    Fetches all branches and prunes deleted remote refs.

    Args:
        name: Name of the extra repo to update (e.g. "enterprise").
    """
    from oduflow.extra_addons import fetch_extra_repo

    team = _resolve_team(ctx)
    summary = fetch_extra_repo(team, name)
    return _format_fetch_summary(summary)


def _format_fetch_summary(summary: dict) -> str:
    name = summary["name"]
    if summary["up_to_date"]:
        return f"Extra repo '{name}': already up to date."
    parts = [f"Extra repo '{name}' updated:"]
    for b in summary["updated_branches"]:
        parts.append(f"  {b['branch']}: {b['new_commits']} new commit(s)")
    for b in summary["new_branches"]:
        parts.append(f"  {b}: new branch")
    for b in summary["deleted_branches"]:
        parts.append(f"  {b}: deleted")
    return "\n".join(parts)


def _parse_extra_addons(raw: str) -> dict[str, str]:
    result = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            name, branch = item.split(":", 1)
            result[name.strip()] = branch.strip()
        else:
            raise ValueError(
                f"Extra addon '{item}' must include a branch (e.g. '{item}:18.0')."
            )
    return result


# =============================================================================
# MCP Tools — Environments
# =============================================================================


@mcp.tool()
@handle_errors
@with_branch_lock
def create_environment(
    branch_name: str,
    template_name: str = "",
    repo_url: str = "",
    odoo_image: str = "",
    extra_addons: str = "",
    sanitize: bool = True,
    ctx: Context = None,
) -> str:
    """
    Provision a new ephemeral Odoo environment for a specific branch.

    Args:
        branch_name: The name of the git branch (will be used for resource naming).
        template_name: Name of the template profile to use as database template. Pass "none" to skip template and initialise Odoo from scratch with -i base. When a template is specified, repo_url and odoo_image are loaded from template metadata (but can be overridden).
        repo_url: URL of the git repository to clone. Optional when template_name is specified (loaded from template metadata).
        odoo_image: Full Docker image name with tag (e.g. "odoo:17.0"). Optional when template_name is specified (loaded from template metadata).
        extra_addons: Comma-separated list of extra addon repo names with branches (e.g. "enterprise:18.0,custom-themes:main"). Each entry must include a branch after a colon.
        sanitize: Sanitize the database after provisioning (default: True). Disables incoming/outgoing mail servers and runs custom scripts from the .odoo_sanitize/ folder in the repository.
    """
    import json

    settings = _get_settings()
    team = _resolve_team(ctx)
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
        metadata_path = team.get_template_metadata_path(resolved_template)
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

                    _metadata_extra = _normalize_extra_addons(raw_extra)
                    if _metadata_extra:
                        extra_addons = ",".join(
                            f"{name}:{branch}"
                            for name, branch in _metadata_extra.items()
                        )

    if not effective_repo_url:
        raise ValueError(
            "repo_url is required (not found in template metadata either)."
        )
    git_ops.validate_repo_url(effective_repo_url)
    if not effective_odoo_image:
        raise ValueError(
            "odoo_image is required (not found in template metadata either)."
        )

    extra_dict = _parse_extra_addons(extra_addons) if extra_addons else {}
    result = env_ops.create_environment(
        settings,
        team,
        branch_name,
        effective_repo_url,
        effective_odoo_image,
        template_name=resolved_template,
        extra_addons=extra_dict or None,
        git_user=effective_git_user,
        sanitize=sanitize,
    )
    display_template = (
        resolved_template
        if resolved_template is not None
        else "none (init from scratch)"
    )
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
        extras_display = ", ".join(
            f"{name} ({branch})" for name, branch in extra_dict.items()
        )
        lines.append(f"Extra Addons: {extras_display}")
    setup_logs = result.get("setup_logs", [])
    if setup_logs:
        lines.append("\n--- Setup Log ---")
        lines.extend(setup_logs)
    import re

    _ver_match = re.search(r"odoo[:/](\d+)(?:\.0)?", effective_odoo_image)
    if _ver_match:
        _odoo_ver = _ver_match.group(1)
        lines.append(
            f'\n⚠️ After creating an environment, immediately call get_odoo_development_guide(version="{_odoo_ver}") to load Odoo {_odoo_ver} development standards and constraints. Do not wait for the user to ask — these guidelines must be loaded before writing any code.'
        )
    return "\n".join(lines)


@mcp.tool()
@handle_errors
@with_branch_lock
def save_as_template(branch_name: str, template_name: str, ctx: Context = None) -> str:
    """
    Save a branch environment as the new template (DB + filestore).

    Replaces the template database and filestore with the data from the specified
    branch. If other environments use this template with overlay-mounted filestores,
    their filestore deltas will be reset to the new baseline — this is destructive
    for those environments. If no other environments share this template, the
    operation is safe.

    Requires EXPLICIT user permission and confirmation before execution.
    If the user has not clearly and unambiguously asked you to save
    a specific branch as template, DO NOT call this tool.

    Args:
        branch_name: The name of the branch whose DB and filestore will become the new template.
        template_name: Name of the template profile to publish into.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = system_ops.publish_env_as_template(
        settings, team, branch_name, template_name=template_name
    )
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
def list_templates(ctx: Context = None) -> str:
    """List available template profiles (database + filestore snapshots)."""
    settings = _get_settings()
    team = _resolve_team(ctx)
    templates = system_ops.list_templates(settings, team)
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
@with_team_lock
def import_template_from_odoo(
    odoo_url: str,
    master_pwd: str,
    db_name: str = "",
    template_name: str = "default",
    ctx: Context = None,
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
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = system_ops.import_from_odoo(
        settings,
        team,
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


# =============================================================================
# MCP Tools — Agent guides
# =============================================================================


@mcp.tool()
@handle_errors
def get_agent_instructions(ctx: Context = None) -> str:
    """Get instructions for AI coding agents on how to use Oduflow MCP tools."""
    import pathlib

    team = _resolve_team(ctx)
    for name in ("agent_instructions.md", "agent_skill.md", "agent_guide.md"):
        skill_path = os.path.join(team.data_dir, "agent_guides", name)
        if os.path.isfile(skill_path):
            with open(skill_path, "r", encoding="utf-8") as f:
                return f.read()
    for name in ("agent_instructions.md", "agent_skill.md", "agent_guide.md"):
        bundled = (
            pathlib.Path(__file__).resolve().parent
            / "templates"
            / "agent_guides"
            / name
        )
        if bundled.is_file():
            return bundled.read_text(encoding="utf-8")
    return "Agent skill not found."


@mcp.tool()
@handle_errors
def get_odoo_development_guide(version: str, ctx: Context = None) -> str:
    """
    Get Odoo development standards and constraints guide for a specific Odoo version.

    Args:
        version: Odoo version number (e.g. "17", "17.0", "18", "18.0"). Both "17" and "17.0" formats are accepted.
    """
    import pathlib

    normalized = version.split(".")[0]
    filename = f"odoo_{normalized}_guide.md"
    team = _resolve_team(ctx)
    guide_path = os.path.join(team.data_dir, "agent_guides", filename)
    if os.path.isfile(guide_path):
        with open(guide_path, "r", encoding="utf-8") as f:
            return f.read()
    bundled = (
        pathlib.Path(__file__).resolve().parent
        / "templates"
        / "agent_guides"
        / filename
    )
    if bundled.is_file():
        return bundled.read_text(encoding="utf-8")
    available = []
    guides_dir = os.path.join(team.data_dir, "agent_guides")
    if os.path.isdir(guides_dir):
        available = [
            f.replace("odoo_", "").replace("_guide.md", "")
            for f in os.listdir(guides_dir)
            if f.startswith("odoo_") and f.endswith("_guide.md")
        ]
    if available:
        return f"No development guide found for Odoo {version}. Available versions: {', '.join(sorted(available))}"
    return f"No development guide found for Odoo {version}."


# =============================================================================
# MCP Tools — Template management
# =============================================================================


@mcp.tool()
@handle_errors
@with_team_lock
def delete_template(template_name: str, ctx: Context = None) -> str:
    """
    DANGEROUS: Delete a template profile — permanently removes its template database and files from disk.

    This is a destructive, irreversible operation. All environments that depend on this
    template will lose their baseline and cannot be recreated until a new template is set up.

    NEVER call this tool on your own initiative. Requires EXPLICIT user permission
    and confirmation before execution. If the user has not clearly and unambiguously
    asked you to delete a specific template, DO NOT call this tool.

    Args:
        template_name: Name of the template profile to delete.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = system_ops.delete_template(settings, team, template_name)
    return f"Template '{result['template_name']}' deleted. Template DB '{result['template_db']}' removed."


# =============================================================================
# MCP Tools — Environment lifecycle
# =============================================================================


@mcp.tool()
@handle_errors
@with_branch_lock
def delete_environment(branch_name: str, ctx: Context = None) -> str:
    """
    Stop and remove all resources associated with an Odoo environment.

    Args:
        branch_name: The name of the branch to tear down.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    warnings = env_ops.delete_environment(settings, team, branch_name)
    result = f"Environment for branch '{branch_name}' has been torn down."
    if warnings:
        result += "\n\n⚠️ Warnings:\n" + "\n".join(f"- {w}" for w in warnings)
    return result


@mcp.tool()
@handle_errors
def list_environments(ctx: Context = None) -> str:
    """
    List all managed Odoo environments.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    envs = env_ops.list_environments(settings, team)
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
@with_branch_lock
def run_odoo_tests(branch_name: str, modules: str, ctx: Context = None) -> str:
    """
    Run Odoo tests for specific modules in an environment.

    Args:
        branch_name: The name of the branch/environment.
        modules: Comma-separated list of modules to test.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    output = odoo_ops.run_environment_tests(settings, team, branch_name, modules)
    return f"Test Results for {branch_name}:\n\n{output}"


@mcp.tool()
@handle_errors
def get_environment_logs(
    branch_name: str, n_lines: int = 100, ctx: Context = None
) -> str:
    """
    Get the last N lines of logs from the Odoo container for a specific branch.

    Args:
        branch_name: The name of the branch/environment.
        n_lines: The number of recent log lines to retrieve (default 100).
    """
    output = odoo_ops.get_environment_logs(_get_settings(), branch_name, n_lines)
    return f"Recent logs for {branch_name}:\n\n{_ANSI_RE.sub('', output)}"


@mcp.tool()
@handle_errors
def restart_environment(branch_name: str, ctx: Context = None) -> str:
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
@with_branch_lock
def rebuild_environment(branch_name: str, ctx: Context = None) -> str:
    """
    Rebuild the Odoo container for an environment without losing the database or filestore.

    Use this when the container is broken (e.g. packages were accidentally removed,
    system files corrupted) and you need a fresh container from the same image,
    reconnected to the existing database and filestore.

    Args:
        branch_name: The name of the branch/environment to rebuild.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = env_ops.rebuild_environment(settings, team, branch_name)
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
def get_environment_info(branch_name: str, ctx: Context = None) -> str:
    """
    Get comprehensive information about an environment for a specific branch.

    Returns database name, URL, repository, image, template, extra addons,
    workspace path, container status, and CPU/RAM stats.

    Args:
        branch_name: The name of the branch/environment to check.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    info = env_ops.get_environment_info(settings, team, branch_name)
    overall = (
        "All containers running"
        if info["all_running"]
        else "Some containers not running"
    )
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
def stop_environment(branch_name: str, ctx: Context = None) -> str:
    """
    Stop the Odoo container for a specific branch environment.

    Args:
        branch_name: The name of the branch/environment to stop.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = env_ops.stop_environment(settings, team, branch_name)
    return (
        f"Environment stopped successfully!\n"
        f"Stopped containers: {', '.join(result['stopped'])}"
    )


@mcp.tool()
@handle_errors
def start_environment(branch_name: str, ctx: Context = None) -> str:
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
@with_branch_lock
def pull_and_apply(branch_name: str, ctx: Context = None) -> str:
    """
    Pull latest changes from the remote repository for an environment
    and take appropriate action based on what changed.

    Pulls both the main repository and any extra addon worktrees.

    Analyzes changed files and automatically:
    - Upgrades modules if __manifest__.py version/data/assets changed, or security XML changed
    - Restarts the container if Python files changed
    - Does nothing if only XML/JS changed (--dev=xml handles hot reload)

    Args:
        branch_name: The name of the branch/environment to pull updates for.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = env_ops.pull_environment(settings, team, branch_name)
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


# =============================================================================
# MCP Tools — Odoo operations
# =============================================================================


@mcp.tool()
@handle_errors
@with_branch_lock
def install_odoo_modules(branch_name: str, modules: str, ctx: Context = None) -> str:
    """
    Install Odoo modules in an environment.

    Args:
        branch_name: The name of the branch/environment.
        modules: Comma-separated list of modules to install (e.g., "sale,crm,web").
    """
    modules_list = [m.strip() for m in modules.split(",") if m.strip()]
    if not modules_list:
        return "Error: At least one module name is required."

    settings = _get_settings()
    team = _resolve_team(ctx)
    result = odoo_ops.install_odoo_modules(settings, team, branch_name, *modules_list)
    exit_code = result["exit_code"]
    modules_str = ", ".join(result["modules"])
    output = result.get("output", "")
    if exit_code == 0:
        env_ops.restart_environment(settings, branch_name)
        return f"Success. Modules installed: {modules_str}. Container restarted. Exit code: 0.\n\nOutput:\n{output}"
    return (
        f"Error. Modules: {modules_str}. Exit code: {exit_code}.\n\nOutput:\n{output}"
    )


@mcp.tool()
@handle_errors
@with_branch_lock
def upgrade_odoo_modules(branch_name: str, modules: str, ctx: Context = None) -> str:
    """
    Upgrade Odoo modules in an environment.

    Args:
        branch_name: The name of the branch/environment.
        modules: Comma-separated list of modules to upgrade (e.g., "sale,crm,web").
    """
    modules_list = [m.strip() for m in modules.split(",") if m.strip()]
    if not modules_list:
        return "Error: At least one module name is required."

    settings = _get_settings()
    team = _resolve_team(ctx)
    result = odoo_ops.upgrade_odoo_modules(settings, team, branch_name, *modules_list)
    exit_code = result["exit_code"]
    modules_str = ", ".join(result["modules"])
    output = result.get("output", "")
    if exit_code == 0:
        return f"Success. Modules upgraded: {modules_str}. Exit code: 0.\n\nOutput:\n{output}"
    return (
        f"Error. Modules: {modules_str}. Exit code: {exit_code}.\n\nOutput:\n{output}"
    )


@mcp.tool()
@handle_errors
def read_file_in_odoo(
    branch_name: str, path: str, read_range: str = "", ctx: Context = None
) -> str:
    """
    Read a text file or list a directory inside the Odoo container for a specific branch.

    Use this tool to inspect files in the container without constructing shell commands.
    Common use cases:
    - Read Odoo source code (e.g. /usr/lib/python3/dist-packages/odoo/addons/sale/models/sale_order.py)
    - Inspect addon structure (e.g. /mnt/extra-addons/)
    - Check config files (e.g. /etc/odoo/odoo.conf)
    - Verify file presence after pull_and_apply

    If the path is a directory, returns a listing (like `ls -la`).
    If the path is a text file, returns its contents (first 100KB by default).
    Binary files are not supported — use exec_in_odoo for binary operations.

    Prefer this tool over exec_in_odoo with `cat` or `ls` commands.

    Args:
        branch_name: The name of the branch/environment.
        path: Absolute path inside the container (e.g. "/mnt/extra-addons/my_module/__manifest__.py").
        read_range: Optional line range in format "START:END" (e.g. "1:50", "100:200").
                    If omitted, returns the entire file (up to 100KB).
    """
    result = odoo_ops.read_file_in_environment(
        _get_settings(), branch_name, path, read_range
    )
    if "error" in result:
        return f"Error: {result['error']}"
    return result["output"]


@mcp.tool()
@handle_errors
@with_branch_lock
def exec_in_odoo(
    branch_name: str, command: str, user: str = "odoo", ctx: Context = None
) -> str:
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
@with_branch_lock
def reset_admin_password(
    branch_name: str, new_password: str = "test", ctx: Context = None
) -> str:
    """
    Reset the admin user password in the environment's Odoo database.

    Hashes the password using passlib (pbkdf2_sha512) inside the Odoo container
    and updates the res_users record where login = 'admin'.

    Args:
        branch_name: The name of the branch/environment.
        new_password: The new password for the admin user (default: "test").
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = odoo_ops.reset_admin_password(settings, team, branch_name, new_password)
    return f"Admin password has been reset successfully.\nLogin: {result['login']}\nNew password: {new_password}"


@mcp.tool()
@handle_errors
@with_branch_lock
def run_db_query(
    branch_name: str, query: str, output_format: str = "csv", ctx: Context = None
) -> str:
    """
    Execute a SQL query against the environment's PostgreSQL database.

    Args:
        branch_name: The name of the branch/environment.
        query: SQL query to execute (e.g. "SELECT id, name FROM res_partner LIMIT 10").
        output_format: "csv" (default, compact for agent consumption) or "human" (pretty table — use when relaying results to the user).
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = odoo_ops.run_db_query(settings, team, branch_name, query, output_format)
    return result["output"]


# =============================================================================
# MCP Tools — Auxiliary services
# =============================================================================


@mcp.tool()
@handle_errors
@with_team_lock
def create_service(
    name: str,
    image: str,
    port: int,
    hostname: str = "",
    env_vars: str = "",
    ctx: Context = None,
) -> str:
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
    team = _resolve_team(ctx)
    parsed_env = None
    if env_vars:
        parsed_env = dict(
            item.split("=", 1) for item in env_vars.split(",") if "=" in item
        )
    result = service_ops.create_service(
        settings,
        team,
        name,
        image,
        port,
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
@with_team_lock
def update_service(name: str, ctx: Context = None) -> str:
    """
    Update a managed auxiliary service container by pulling the latest image and re-creating the container with the same settings.

    Args:
        name: The name of the service to update (e.g. "redis", "meilisearch").
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = service_ops.update_service(settings, team, name)
    status = (
        "Image updated (new image pulled, container recreated)"
        if result.get("image_updated")
        else "Already up-to-date (no changes)"
    )
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
@with_team_lock
def delete_service(name: str, ctx: Context = None) -> str:
    """
    Stop and remove a managed auxiliary service container.

    Args:
        name: The name of the service to delete.
    """
    result = service_ops.delete_service(_get_settings(), name)
    return f"Service '{result['name']}' deleted. Container '{result['container_name']}' removed."


@mcp.tool()
@handle_errors
def list_service_presets(ctx: Context = None) -> str:
    """List saved service presets (configurations that can be restored)."""
    team = _resolve_team(ctx)
    presets = service_presets.list_presets(team)
    if not presets:
        return "No service presets saved."
    output = "Saved Service Presets:\n"
    for p in presets:
        env_str = (
            ", ".join(f"{k}={v}" for k, v in p["env_vars"].items())
            if p.get("env_vars")
            else ""
        )
        output += f"- {p['name']}: image={p['image']}, port={p['port']}"
        if p.get("hostname"):
            output += f", hostname={p['hostname']}"
        if env_str:
            output += f", env=[{env_str}]"
        output += "\n"
    return output


@mcp.tool()
@handle_errors
@with_team_lock
def restore_service(name: str, ctx: Context = None) -> str:
    """
    Restore a service from a saved preset. Recreates the service container with the same configuration.

    Args:
        name: The name of the saved service preset to restore.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    preset = service_presets.get_preset(team, name)
    result = service_ops.create_service(
        settings,
        team,
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
@with_team_lock
def delete_service_preset(name: str, ctx: Context = None) -> str:
    """
    Remove a saved service preset.

    Args:
        name: The name of the service preset to delete.
    """
    team = _resolve_team(ctx)
    service_presets.delete_preset(team, name)
    return f"Service preset '{name}' deleted."


@mcp.tool()
@handle_errors
def list_services(ctx: Context = None) -> str:
    """List all managed auxiliary service containers."""
    settings = _get_settings()
    team = _resolve_team(ctx)
    services = service_ops.list_services(settings, team)
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
def get_service_logs(name: str, n_lines: int = 100, ctx: Context = None) -> str:
    """
    Get logs from a managed auxiliary service container.

    Args:
        name: The name of the service.
        n_lines: Number of recent log lines to retrieve (default 100).
    """
    output = service_ops.get_service_logs(_get_settings(), name, n_lines)
    return f"Recent logs for service '{name}':\n\n{_ANSI_RE.sub('', output)}"


# =============================================================================
# CLI helpers
# =============================================================================


def _run_init(settings: Settings) -> None:
    _copy_bundled_configs()
    result = system_ops.init_system(settings)
    print(f"System {result['status']}.")

    # Initialize per-team directories
    import pathlib
    import shutil

    bundled_dir = pathlib.Path(__file__).resolve().parent / "templates"

    for team_id, team in settings.teams.items():
        os.makedirs(team.workspaces_dir, exist_ok=True)
        os.makedirs(os.path.join(team.data_dir, "templates"), exist_ok=True)
        os.makedirs(team.shared_repos_dir, exist_ok=True)

        # Initialize empty service presets file
        presets_path = os.path.join(team.data_dir, "service_presets.json")
        if not os.path.isfile(presets_path):
            with open(presets_path, "w") as f:
                f.write("{}\n")

        # Copy bundled odoo.conf to team data dir
        odoo_conf_dest = os.path.join(team.data_dir, "odoo.conf")
        if not os.path.isfile(odoo_conf_dest):
            bundled_odoo_conf = bundled_dir / "odoo.conf"
            if bundled_odoo_conf.is_file():
                shutil.copy2(str(bundled_odoo_conf), odoo_conf_dest)
                print(f"  [team.{team_id}] Config: {odoo_conf_dest}")

        # Seed team-level sanitization scripts
        sanitize_dest = os.path.join(team.data_dir, "odoo_sanitize")
        os.makedirs(sanitize_dest, exist_ok=True)
        bundled_sql = bundled_dir / "01_disable_mail.sql"
        dest_sql = os.path.join(sanitize_dest, "01_disable_mail.sql")
        if not os.path.isfile(dest_sql) and bundled_sql.is_file():
            shutil.copy2(str(bundled_sql), dest_sql)
            print(f"  [team.{team_id}] Sanitize script: {dest_sql}")

        # Copy bundled agent guides
        agent_guides_dest = os.path.join(team.data_dir, "agent_guides")
        os.makedirs(agent_guides_dest, exist_ok=True)
        bundled_guides_dir = bundled_dir / "agent_guides"
        if bundled_guides_dir.is_dir():
            for guide_file in bundled_guides_dir.iterdir():
                if guide_file.is_file() and guide_file.suffix == ".md":
                    dest_file = os.path.join(agent_guides_dest, guide_file.name)
                    if not os.path.isfile(dest_file):
                        shutil.copy2(str(guide_file), dest_file)
                        print(f"  [team.{team_id}] Agent guide: {dest_file}")

        print(f"  Team {team_id} initialized.")
        print(f"    Workspaces: {team.workspaces_dir}")
        print(f"    Templates: {os.path.join(team.data_dir, 'templates')}")
        print(f"    Shared repos: {team.shared_repos_dir}")
        print(f"    Service presets: {presets_path}")


def _copy_bundled_configs() -> None:
    """Copy shared configs (postgresql.conf) to the config directory."""
    import pathlib
    import shutil

    settings = _get_settings()
    etc_dir = pathlib.Path(settings.etc_dir)
    try:
        os.makedirs(etc_dir, exist_ok=True)
    except PermissionError:
        logger.warning(
            "Cannot create %s (permission denied), skipping config copy", etc_dir
        )
        return
    bundled_dir = pathlib.Path(__file__).resolve().parent / "templates"
    for conf_name in ("postgresql.conf",):
        dest = etc_dir / conf_name
        if not dest.is_file():
            bundled = bundled_dir / conf_name
            if bundled.is_file():
                try:
                    shutil.copy2(str(bundled), str(dest))
                    print(f"  Config: {dest}")
                except PermissionError:
                    logger.warning("Cannot write %s (permission denied)", dest)


def _run_reload_template(
    settings: Settings, team: TeamSettings, template_name: str, dump_path: str = ""
) -> None:
    result = system_ops.reload_template(
        settings,
        team,
        template_name=template_name,
        dump_path=dump_path or None,
    )
    msg = f"Template DB {result['status']}.\nTemplate DB: {result['template_db']}"
    if "restore_seconds" in result:
        msg += f"\nDB restore time: {result['restore_seconds']}s"
    if "tables" in result:
        msg += f"\nTables restored: {result['tables']}"
    if "message" in result and result["message"].strip():
        msg += f"\nRestore output: {result['message']}"
    print(msg)


def _run_init_template(
    settings: Settings,
    team: TeamSettings,
    odoo_image: str,
    modules: str = "base",
    template_name: str = "",
    force: bool = False,
) -> None:
    result = system_ops.init_template(
        settings,
        team,
        template_name=template_name,
        odoo_image=odoo_image,
        modules=modules,
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


def _run_template_up(
    settings: Settings, team: TeamSettings, odoo_image: str, template_name: str = ""
) -> None:
    result = system_ops.template_up(
        settings, team, odoo_image=odoo_image, template_name=template_name
    )
    print(
        f"Template editor started.\n"
        f"URL: {result['url']}\n"
        f"Container: {result['container']}\n"
        f"Database: {result['database']}\n"
        f"Filestore: {result['filestore']}\n\n"
        f"Make your changes in the browser, then run: oduflow template-down"
    )


def _run_template_down(
    settings: Settings, team: TeamSettings, template_name: str = ""
) -> None:
    result = system_ops.template_down(settings, team, template_name=template_name)
    print(
        f"Template editor stopped.\n"
        f"Dump saved: {result['dump']}\n"
        f"Filestore: {result['filestore']}\n"
        f"Template DB '{result['database']}' restored."
    )


def _run_template_from_env(
    settings: Settings, team: TeamSettings, branch: str, template_name: str = ""
) -> None:
    result = system_ops.publish_env_as_template(
        settings, team, branch_name=branch, template_name=template_name
    )
    print(
        f"Branch '{result['branch']}' saved as template '{template_name}'.\n"
        f"Template DB: {result['template_db']}\n"
        f"Dump: {result['dump']}\n"
        f"Filestore: {result['filestore']}"
    )


def _run_delete_template(
    settings: Settings, team: TeamSettings, template_name: str
) -> None:
    result = system_ops.delete_template(settings, team, template_name)
    print(
        f"Template '{result['template_name']}' deleted.\nTemplate DB '{result['template_db']}' removed."
    )


def _run_import_template(
    settings: Settings,
    team: TeamSettings,
    odoo_url: str,
    master_pwd: str,
    db_name: str = "",
    template_name: str = "",
) -> None:
    result = system_ops.import_from_odoo(
        settings,
        team,
        odoo_url=odoo_url,
        master_pwd=master_pwd,
        db_name=db_name,
        template_name=template_name,
    )
    print(
        f"Template '{result['template_name']}' imported successfully!\n"
        f"Source: {result['source_url']} (db: {result['source_db']})\n"
        f"Odoo version: {result['odoo_version']}\n"
        f"Odoo image: {result['odoo_image']}\n"
        f"Template DB: {result['template_db']}\n"
        f"Backup size: {result['zip_size_mb']} MB\n"
        f"DB restore time: {result['restore_seconds']}s"
    )


def _run_list_templates(settings: Settings, team: TeamSettings) -> None:
    templates = system_ops.list_templates(settings, team)
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
        print(
            f"  {r['template_name']}: DB={db_status}, SQL={'yes' if r['has_sql'] else 'no'}, Filestore={'yes' if r['has_filestore'] else 'no'}, Mode={overlay_status}{size_info}"
        )


def _run_list_services(settings: Settings, team: TeamSettings) -> None:
    from oduflow.docker_ops import service_ops

    services = service_ops.list_services(settings, team)
    if not services:
        print("No active services found.")
        return
    print("Active services:")
    for svc in services:
        status_icon = "●" if svc["status"] == "running" else "○"
        print(
            f"  {status_icon} {svc['name']} ({svc['container_name']}): {svc['status']}"
        )
        print(f"    Image: {svc['image']}")
        if svc.get("port"):
            print(f"    Port: {svc['port']}")
        if svc.get("url"):
            print(f"    URL: {svc['url']}")
        if svc.get("env_vars"):
            env_str = ", ".join(f"{k}={v}" for k, v in svc["env_vars"].items())
            print(f"    Env: {env_str}")


def _run_cleanup(settings: Settings, team: TeamSettings, dry_run: bool = True) -> None:
    result = system_ops.cleanup_orphans(settings, team, dry_run=dry_run)
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
    print(f"System {result['status']}.\nRemoved: {result['removed']}")


def _print_tools(verbose: bool = False) -> None:
    import inspect

    print("Registered tools:")
    for name in sorted(mcp._tool_manager._tools.keys()):
        tool_fn = mcp._tool_manager._tools[name].fn
        sig = inspect.signature(tool_fn)
        params = []
        for p in sig.parameters.values():
            if p.name == "ctx":
                continue
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
        # Filter out ctx parameter for positional arg mapping
        params = [p for p in sig.parameters.values() if p.name != "ctx"]
        kwargs = {}
        for i, value in enumerate(tool_argv):
            if i >= len(params):
                print(f"Warning: extra argument '{value}' ignored", file=sys.stderr)
                continue
            param = params[i]
            annotation = param.annotation
            if annotation is bool or (
                annotation is inspect.Parameter.empty
                and isinstance(param.default, bool)
            ):
                kwargs[param.name] = value.lower() in ("true", "1", "yes")
            elif annotation is int or (
                annotation is inspect.Parameter.empty and isinstance(param.default, int)
            ):
                kwargs[param.name] = int(value)
            elif annotation is float:
                kwargs[param.name] = float(value)
            else:
                kwargs[param.name] = value

    if not kwargs:
        required = [
            p
            for p in sig.parameters.values()
            if p.default is inspect.Parameter.empty and p.name != "ctx"
        ]
        if required:
            parts = []
            for p in sig.parameters.values():
                if p.name == "ctx":
                    continue
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


def _get_version() -> str:
    """Return the installed package version."""
    from importlib.metadata import version, PackageNotFoundError

    try:
        return version("oduflow")
    except PackageNotFoundError:
        return "dev"


# =============================================================================
# CLI entry point
# =============================================================================


def main() -> None:
    """Entry point for the Oduflow MCP server."""
    parser = argparse.ArgumentParser(
        prog="oduflow", description="Oduflow — Odoo dev environment manager"
    )
    parser.add_argument(
        "--version", action="version", version=f"oduflow {_get_version()}"
    )
    sub = parser.add_subparsers(dest="command", title="commands", metavar="")

    # --- System commands ---
    p_init = sub.add_parser(
        "init", help="Initialize shared infrastructure and all team directories"
    )
    p_init.add_argument(
        "--license",
        default="",
        metavar="FILE",
        dest="license_file",
        help="Path to license.key file to install to /etc/oduflow/license.key",
    )

    sub.add_parser("destroy", help="Destroy all shared infrastructure")

    # --- Template commands (need --team) ---
    p_reload = sub.add_parser(
        "reload-template",
        help="Drop and re-restore a template DB from template profile",
    )
    p_reload.add_argument("template_name", help="Template profile name")
    p_reload.add_argument(
        "--dump-path",
        default="",
        help="Path to dump file (overrides template profile path)",
    )
    p_reload.add_argument("--team", default="1", help="Team ID (default: 1)")

    p_init_tpl = sub.add_parser(
        "init-template",
        help="Generate template dump and filestore from a clean Odoo image",
    )
    p_init_tpl.add_argument(
        "--odoo-image", required=True, help="Docker image for Odoo (e.g. odoo:17.0)"
    )
    p_init_tpl.add_argument(
        "--modules",
        default="base",
        help="Comma-separated modules to install (default: base)",
    )
    p_init_tpl.add_argument(
        "--template-name", required=True, help="Template profile name"
    )
    p_init_tpl.add_argument(
        "--force", action="store_true", help="Overwrite existing dump.sql and filestore"
    )
    p_init_tpl.add_argument("--team", default="1", help="Team ID (default: 1)")

    p_tpl_up = sub.add_parser(
        "template-up",
        help="Start a template editor: Odoo working directly with template DB and filestore",
    )
    p_tpl_up.add_argument(
        "--odoo-image", required=True, help="Docker image for Odoo (e.g. odoo:17.0)"
    )
    p_tpl_up.add_argument(
        "--template-name", required=True, help="Template profile name"
    )
    p_tpl_up.add_argument("--team", default="1", help="Team ID (default: 1)")

    p_tpl_down = sub.add_parser(
        "template-down",
        help="Stop the template editor, dump the updated DB, restore template flag",
    )
    p_tpl_down.add_argument(
        "--template-name", required=True, help="Template profile name"
    )
    p_tpl_down.add_argument("--team", default="1", help="Team ID (default: 1)")

    p_tfe = sub.add_parser(
        "template-from-env", help="Save a branch environment as the new template"
    )
    p_tfe.add_argument("branch", help="Branch name to use as template source")
    p_tfe.add_argument("--template-name", required=True, help="Template profile name")
    p_tfe.add_argument("--team", default="1", help="Team ID (default: 1)")

    p_drop_tpl = sub.add_parser(
        "delete-template", help="Delete a template profile (template DB + files)"
    )
    p_drop_tpl.add_argument("template_name", help="Template profile name to delete")
    p_drop_tpl.add_argument("--team", default="1", help="Team ID (default: 1)")

    p_import = sub.add_parser(
        "import-template", help="Import a template from a running Odoo instance"
    )
    p_import.add_argument(
        "odoo_url",
        help="Base URL of the Odoo instance (e.g. https://my-odoo.example.com)",
    )
    p_import.add_argument("master_pwd", help="Odoo master password")
    p_import.add_argument(
        "--db-name", default="", help="Database name (auto-detected if only one exists)"
    )
    p_import.add_argument(
        "--template-name", required=True, help="Template profile name"
    )
    p_import.add_argument("--team", default="1", help="Team ID (default: 1)")

    p_lt = sub.add_parser("list-templates", help="List available template profiles")
    p_lt.add_argument("--team", default="1", help="Team ID (default: 1)")

    # --- Service commands ---
    p_ls = sub.add_parser(
        "list-services", help="List managed auxiliary service containers"
    )
    p_ls.add_argument("--team", default="1", help="Team ID (default: 1)")

    # --- Cleanup ---
    p_cleanup = sub.add_parser(
        "cleanup",
        help="Find and remove orphaned databases, workspaces, and port entries",
    )
    p_cleanup.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Only show what would be removed (default behavior)",
    )
    p_cleanup.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Actually remove orphaned resources",
    )
    p_cleanup.add_argument("--team", default="1", help="Team ID (default: 1)")

    # --- Tool introspection ---
    p_list = sub.add_parser("list", help="List registered MCP tools")
    p_list.add_argument(
        "--verbose", "-v", action="store_true", help="Show tool descriptions"
    )

    p_call = sub.add_parser(
        "call", help="Call an MCP tool: oduflow call <tool> [args...]"
    )
    p_call.add_argument(
        "call_args", nargs="*", default=[], help="Tool name and arguments"
    )

    # --- Systemd ---
    sub.add_parser(
        "systemd-install", help="Install and enable a systemd service for Oduflow"
    )
    sub.add_parser(
        "systemd-uninstall",
        help="Stop, disable, and remove the Oduflow systemd service",
    )

    args = parser.parse_args()

    # --- Commands that don't need Settings -----------------------

    if args.command == "list":
        _print_tools(verbose=args.verbose)
        return

    if args.command == "call":
        _run_call(args.call_args)
        return

    if args.command == "systemd-install":
        from oduflow.systemd import install as systemd_install

        systemd_install()
        return

    if args.command == "systemd-uninstall":
        from oduflow.systemd import uninstall as systemd_uninstall

        systemd_uninstall()
        return

    # --- Load TOML settings ----------------------------------------

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logging.getLogger("docker").setLevel(logging.WARNING)

    # Bootstrap: if `init` and no config exists, copy the bundled default
    if args.command == "init":
        try:
            find_toml()
        except FileNotFoundError:
            import pathlib
            import shutil

            from oduflow.settings import _resolve_etc_dir

            dest_dir = _resolve_etc_dir()
            os.makedirs(dest_dir, exist_ok=True)
            bundled = pathlib.Path(__file__).resolve().parent / "templates" / "oduflow.toml"
            dest = os.path.join(dest_dir, "oduflow.toml")
            shutil.copy2(str(bundled), dest)
            print(f"Config created: {dest}")

    global _settings
    _settings = _get_settings()

    # --- Resolve team for CLI commands that need it ----------------

    def _cli_team() -> TeamSettings:
        team_id = getattr(args, "team", "1")
        return _settings.get_team(team_id)

    # --- Commands that need Settings --------------------------------

    if args.command is None:
        # No subcommand → start the MCP server
        _start_server()
        return

    if args.command == "init":
        _run_init(_settings)
        if getattr(args, "license_file", ""):
            from oduflow.licensing import install_license

            info = install_license(args.license_file)
            print(f"License installed: {info.label}")
        return

    if args.command == "destroy":
        _run_destroy(_settings)
        return

    if args.command == "reload-template":
        _run_reload_template(
            _settings,
            _cli_team(),
            template_name=args.template_name,
            dump_path=args.dump_path,
        )
        return

    if args.command == "init-template":
        _run_init_template(
            _settings,
            _cli_team(),
            odoo_image=args.odoo_image,
            modules=args.modules,
            template_name=args.template_name,
            force=args.force,
        )
        return

    if args.command == "template-up":
        _run_template_up(
            _settings,
            _cli_team(),
            odoo_image=args.odoo_image,
            template_name=args.template_name,
        )
        return

    if args.command == "template-down":
        _run_template_down(_settings, _cli_team(), template_name=args.template_name)
        return

    if args.command == "template-from-env":
        _run_template_from_env(
            _settings, _cli_team(), branch=args.branch, template_name=args.template_name
        )
        return

    if args.command == "delete-template":
        _run_delete_template(_settings, _cli_team(), template_name=args.template_name)
        return

    if args.command == "import-template":
        _run_import_template(
            _settings,
            _cli_team(),
            odoo_url=args.odoo_url,
            master_pwd=args.master_pwd,
            db_name=args.db_name,
            template_name=args.template_name,
        )
        return

    if args.command == "list-templates":
        _run_list_templates(_settings, _cli_team())
        return

    if args.command == "list-services":
        _run_list_services(_settings, _cli_team())
        return

    if args.command == "cleanup":
        _run_cleanup(_settings, _cli_team(), dry_run=not args.force)
        return


def _start_server() -> None:
    """Start the MCP server (HTTP transport)."""
    from fastmcp.server.http import create_streamable_http_app

    settings = _get_settings()
    host = "0.0.0.0" if settings.routing_mode == "traefik" else settings.host
    port = settings.port

    # Build auth from per-team tokens
    auth = None
    tokens = {}
    for team_id, team in settings.teams.items():
        if team.auth_token:
            tokens[team.auth_token] = {"client_id": team_id, "scopes": []}

    if tokens:
        from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

        auth = StaticTokenVerifier(tokens=tokens)
    else:
        logger.warning("HTTP auth DISABLED (no auth_token set in any team)")

    app = create_streamable_http_app(mcp, "/mcp", auth=auth)

    from oduflow.web_ui import mount_web_ui

    mount_web_ui(app, _get_settings, _locks)

    for tid, team in settings.teams.items():
        if settings.routing_mode == "traefik":
            url = f"https://{team.hostname}/"
        else:
            url = f"http://{host}:{port}/"
        mcp_status = "MCP token ON" if team.auth_token else "MCP token OFF"
        ui_status = "UI auth ON" if team.ui_password else "UI auth OFF"
        logger.info("[team.%s] %s (%s, %s)", tid, url, mcp_status, ui_status)

    import uvicorn

    uvicorn.run(app, host=host, port=port, ws="websockets-sansio")


if __name__ == "__main__":
    main()
