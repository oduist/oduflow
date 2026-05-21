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
    volume_file_ops,
    volume_ops,
)
from oduflow import git_ops
from oduflow.errors import FlowError
from oduflow.locking import LockManager
from oduflow.output_cache import OutputCache, CachedOutput
from oduflow.settings import Settings, TeamSettings, find_toml

logger = logging.getLogger("oduflow")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

_CACHE_THRESHOLD = 5_000  # chars — outputs above this are cached + summarized
_SUMMARY_HEAD_LINES = 200
_SUMMARY_TAIL_LINES = 100
_SUMMARY_ERROR_CONTEXT = 5

_output_cache = OutputCache()

mcp = FastMCP("Oduflow")
_locks = LockManager()
_settings: Settings | None = None
_instance_id: str = ""


def _get_settings() -> Settings:
    global _settings
    if _settings is None:
        toml_path = find_toml()
        _settings = Settings.from_toml(toml_path)
        _settings.validate()
    return _settings


def _resolve_team(ctx: Context | None) -> TeamSettings:
    """Resolve the team from MCP Context.

    Priority: static token → GitHub user → Host header → single-team → team "1".
    """
    settings = _get_settings()
    # 1. Token-based: client_id set by auth provider (static tokens map to team_id)
    team_id = ctx.client_id if ctx and ctx.client_id else None
    if team_id and team_id in settings.teams:
        return settings.teams[team_id]
    # 2. GitHub OAuth: resolve by GitHub login from access token claims
    if settings.oauth_enabled and ctx:
        github_login = _get_github_user(ctx)
        if github_login:
            team = settings.get_team_by_github_user(github_login)
            if team:
                return team
            # No github_users configured anywhere → single-team fallback
            if not settings.any_team_has_github_users:
                if len(settings.teams) == 1:
                    return next(iter(settings.teams.values()))
    # 3. Hostname-based: match Host header against team hostnames
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
    # 4. Fallback: single team or default team "1"
    if len(settings.teams) == 1:
        return next(iter(settings.teams.values()))
    return settings.get_team("1")


def _get_github_user(ctx: Context | None) -> str:
    """Extract GitHub login from MCP Context access token claims.

    Returns empty string if not available (e.g. static token auth).
    """
    if not ctx:
        return ""
    try:
        from fastmcp.server.dependencies import get_access_token

        token = get_access_token()
        if token and hasattr(token, "claims") and token.claims:
            return str(token.claims.get("login", ""))
    except Exception:
        pass
    return ""


# -- Output cache helpers --


def _make_summary(cached: CachedOutput) -> str:
    """Build a smart summary from cached output: head + errors + tail + metadata."""
    lines = cached.lines
    total = cached.total_lines
    parts: list[str] = []

    # Head
    parts.extend(lines[:_SUMMARY_HEAD_LINES])

    # Errors with context (deduplicated)
    if cached.error_line_indices:
        parts.append(
            f"\n--- Errors/Warnings ({len(cached.error_line_indices)} occurrences) ---"
        )
        seen: set[int] = set()
        for idx in cached.error_line_indices:
            context_end = min(idx + _SUMMARY_ERROR_CONTEXT + 1, total)
            for i in range(idx, context_end):
                if i not in seen:
                    parts.append(lines[i])
                    seen.add(i)
            parts.append("")

    # Skipped count
    skip_start = _SUMMARY_HEAD_LINES
    skip_end = total - _SUMMARY_TAIL_LINES
    if skip_end > skip_start:
        parts.append(f"--- Skipped {skip_end - skip_start} lines of output ---")

    # Tail
    tail_start = max(total - _SUMMARY_TAIL_LINES, _SUMMARY_HEAD_LINES)
    parts.extend(lines[tail_start:])

    # Metadata footer
    parts.append("")
    parts.append(
        f"[Cached output: id={cached.output_id}, {cached.total_lines} lines, {cached.total_chars} chars]"
    )
    parts.append(
        f'[Use read_output(output_id="{cached.output_id}", ...) to search, read ranges, or get full output]'
    )

    return "\n".join(parts)


def _maybe_cache(output: str, header: str, source_tool: str, source_args: str) -> str:
    """If output exceeds threshold, cache it and return header + summary. Otherwise return as-is."""
    if len(output) > _CACHE_THRESHOLD:
        cached = _output_cache.store(
            output, source_tool=source_tool, source_args=source_args
        )
        return f"{header}\n\n{_make_summary(cached)}"
    return f"{header}\n\nOutput:\n{output}"


# -- Decorators --


def handle_errors(fn):
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        import anyio

        def _run():
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

        return await anyio.to_thread.run_sync(_run)

    return wrapper


def with_env_lock(fn):
    """Acquire a per-environment lock before executing the tool function."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        env_name = kwargs.get("env_name") or (args[0] if args else None)
        if not env_name:
            raise ToolError("env_name is required")
        _locks.acquire_env(env_name)
        try:
            return fn(*args, **kwargs)
        finally:
            _locks.release_env(env_name)

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
def create_environment(
    branch: str,
    env_name: str = "",
    template_name: str = "",
    repo_url: str = "",
    odoo_image: str = "",
    extra_addons: str = "",
    sanitize: bool = True,
    auto_install_modules: str = "",
    ctx: Context = None,
) -> str:
    """
    Provision a new ephemeral Odoo environment.

    Args:
        branch: The git branch to clone (e.g. "18.0", "feature/my-feature").
        env_name: Optional environment name. If empty, defaults to the branch name. Use this to create multiple environments from the same branch (e.g. env_name="client-a" with branch="18.0").
        template_name: Name of the template profile to use as database template. Pass "none" to skip template and initialise Odoo from scratch with -i base. When a template is specified, repo_url and odoo_image are loaded from template metadata (but can be overridden).
        repo_url: URL of the git repository to clone. Optional when template_name is specified (loaded from template metadata).
        odoo_image: Full Docker image name with tag (e.g. "odoo:17.0"). Optional when template_name is specified (loaded from template metadata).
        extra_addons: Comma-separated list of extra addon repo names with branches (e.g. "enterprise:18.0,custom-themes:main"). Each entry must include a branch after a colon.
        sanitize: Sanitize the database after provisioning (default: True). Disables incoming/outgoing mail servers and runs custom scripts from the .odoo_sanitize/ folder in the repository.
        auto_install_modules: Comma-separated list of Odoo modules to install automatically after the environment is provisioned (e.g. "sale,purchase,stock"). When a template is specified and this is empty, the value is loaded from template metadata.
    """
    import json

    resolved_env_name = env_name or branch
    _locks.acquire_env(resolved_env_name)
    try:
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
                                f"{name}:{b}" for name, b in _metadata_extra.items()
                            )
                if not auto_install_modules:
                    auto_install_modules = metadata.get("auto_install_modules", "")

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
        auto_install_list = (
            [m.strip() for m in auto_install_modules.split(",") if m.strip()]
            if auto_install_modules
            else []
        )
        result = env_ops.create_environment(
            settings,
            team,
            branch,
            effective_repo_url,
            effective_odoo_image,
            env_name=resolved_env_name,
            template_name=resolved_template,
            extra_addons=extra_dict or None,
            git_user=effective_git_user,
            sanitize=sanitize,
            auto_install_modules=auto_install_list or None,
        )

        from oduflow.telemetry import record_env_created

        record_env_created(_instance_id, _get_version(), settings.disable_telemetry)

        display_template = (
            resolved_template
            if resolved_template is not None
            else "none (init from scratch)"
        )
        lines = [
            "Environment provisioned successfully!",
            f"Environment: {resolved_env_name}",
            f"URL: {result['url']}",
            f"Odoo Container: {result['odoo_container']}",
            f"Database: {result['database']}",
            f"Workspace: {result['workspace']}",
            f"Template: {display_template}",
            f"Creation time: {result.get('elapsed_seconds', '?')}s",
        ]
        if resolved_env_name != branch:
            lines.insert(2, f"Git Branch: {branch}")
        if extra_dict:
            extras_display = ", ".join(
                f"{name} ({b})" for name, b in extra_dict.items()
            )
            lines.append(f"Extra Addons: {extras_display}")
        if auto_install_list:
            lines.append(f"Auto-install modules: {', '.join(auto_install_list)}")
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
    finally:
        _locks.release_env(resolved_env_name)


@mcp.tool()
@handle_errors
@with_env_lock
def save_as_template(env_name: str, template_name: str, ctx: Context = None) -> str:
    """
    Save an environment as the new template (DB + filestore).

    Replaces the template database and filestore with the data from the specified
    environment. If other environments use this template with overlay-mounted filestores,
    their filestore deltas will be reset to the new baseline — this is destructive
    for those environments. If no other environments share this template, the
    operation is safe.

    Requires EXPLICIT user permission and confirmation before execution.
    If the user has not clearly and unambiguously asked you to save
    a specific environment as template, DO NOT call this tool.

    Args:
        env_name: The name of the environment whose DB and filestore will become the new template.
        template_name: Name of the template profile to publish into.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = system_ops.publish_env_as_template(
        settings, team, env_name, template_name=template_name
    )
    affected = result.get("affected_envs", [])
    lines = [
        f"Environment '{result['env_name']}' saved as template '{template_name}'.",
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
        auto_install = r.get("auto_install_modules", "")
        auto_info = f", Auto-install={auto_install}" if auto_install else ""
        output += f"- {r['template_name']}: DB={db_status}, SQL={r['has_sql']}, Filestore={r['has_filestore']}, Mode={overlay_status}{size_info}{auto_info}\n"
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
@with_env_lock
def delete_environment(env_name: str, ctx: Context = None) -> str:
    """
    Stop and remove all resources associated with an Odoo environment.

    Args:
        env_name: The name of the environment to tear down.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    warnings = env_ops.delete_environment(settings, team, env_name)
    result = f"Environment '{env_name}' has been torn down."
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
        status_line = f"- {env['env_name']} (Status: {env['status']})"
        if env.get("url"):
            status_line += f" - {env['url']}"
        output += status_line + "\n"
        git_branch = env.get("git_branch", "")
        if git_branch and git_branch != env["env_name"]:
            output += f"  Git Branch: {git_branch}\n"
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
@with_env_lock
def run_odoo_tests(env_name: str, modules: str, ctx: Context = None) -> str:
    """
    Run Odoo tests for specific modules in an environment.

    Args:
        env_name: The name of the environment.
        modules: Comma-separated list of modules to test.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    output = odoo_ops.run_environment_tests(settings, team, env_name, modules)
    header = f"Test Results for {env_name}:"
    return _maybe_cache(
        output, header, "run_odoo_tests", f"env={env_name}, modules={modules}"
    )


@mcp.tool()
@handle_errors
def get_environment_logs(
    env_name: str,
    n_lines: int = 100,
    grep: str = "",
    level: str = "",
    ctx: Context = None,
) -> str:
    """
    Get the last N lines of logs from the Odoo container for a specific environment.

    Args:
        env_name: The name of the environment.
        n_lines: The number of recent log lines to retrieve (default 100).
        grep: Filter logs to only show lines matching this pattern (case-insensitive substring search). Useful to find specific errors, modules, or messages.
        level: Filter by Odoo log level. One of: "ERROR", "WARNING", "CRITICAL". Returns only lines containing the specified level marker. Can be combined with grep.
    """
    output = odoo_ops.get_environment_logs(
        _get_settings(), env_name, n_lines, grep=grep, level=level
    )
    return f"Recent logs for {env_name}:\n\n{_ANSI_RE.sub('', output)}"


@mcp.tool()
@handle_errors
def restart_environment(env_name: str, wait: bool = True, ctx: Context = None) -> str:
    """
    Restart the Odoo container for a specific environment.

    Args:
        env_name: The name of the environment to restart.
        wait: Wait for Odoo to become ready after restart (default True). Polls /web/health every 2 seconds for up to 120 seconds.
    """
    settings = _get_settings()
    result = env_ops.restart_environment(settings, env_name)
    lines = [
        "Environment restarted successfully!",
        f"Odoo Container: {result['odoo_container']}",
    ]
    if wait:
        team = _resolve_team(ctx)
        ready = env_ops.wait_for_odoo_ready(settings, team, env_name)
        if ready:
            lines.append("Odoo is ready.")
        else:
            lines.append(
                "Warning: Odoo did not become ready within 120 seconds. Check logs with get_environment_logs."
            )
    return "\n".join(lines)


@mcp.tool()
@handle_errors
@with_env_lock
def rebuild_environment(env_name: str, ctx: Context = None) -> str:
    """
    Rebuild the Odoo container for an environment without losing the database or filestore.

    Use this when the container is broken (e.g. packages were accidentally removed,
    system files corrupted) and you need a fresh container from the same image,
    reconnected to the existing database and filestore.

    Args:
        env_name: The name of the environment to rebuild.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = env_ops.rebuild_environment(settings, team, env_name)
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
def get_environment_info(env_name: str, ctx: Context = None) -> str:
    """
    Get comprehensive information about an environment.

    Returns database name, URL, repository, image, template, extra addons,
    workspace path, container status, and CPU/RAM stats.

    Args:
        env_name: The name of the environment to check.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    info = env_ops.get_environment_info(settings, team, env_name)
    overall = (
        "All containers running"
        if info["all_running"]
        else "Some containers not running"
    )
    lines = [f"Environment Info for '{env_name}': {overall}"]
    git_branch = info.get("git_branch", "")
    if git_branch and git_branch != env_name:
        lines.append(f"Git Branch: {git_branch}")
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
def stop_environment(env_name: str, ctx: Context = None) -> str:
    """
    Stop the Odoo container for a specific environment.

    Args:
        env_name: The name of the environment to stop.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = env_ops.stop_environment(settings, team, env_name)
    return (
        f"Environment stopped successfully!\n"
        f"Stopped containers: {', '.join(result['stopped'])}"
    )


@mcp.tool()
@handle_errors
def start_environment(env_name: str, wait: bool = True, ctx: Context = None) -> str:
    """
    Start all containers for a specific environment.

    Args:
        env_name: The name of the environment to start.
        wait: Wait for Odoo to become ready after start (default True). Polls /web/health every 2 seconds for up to 120 seconds.
    """
    settings = _get_settings()
    result = env_ops.start_environment(settings, env_name)
    lines = [
        "Environment started successfully!",
        f"Started containers: {', '.join(result['started'])}",
    ]
    if wait:
        team = _resolve_team(ctx)
        ready = env_ops.wait_for_odoo_ready(settings, team, env_name)
        if ready:
            lines.append("Odoo is ready.")
        else:
            lines.append(
                "Warning: Odoo did not become ready within 120 seconds. Check logs with get_environment_logs."
            )
    return "\n".join(lines)


@mcp.tool()
@handle_errors
@with_env_lock
def pull_and_apply(env_name: str, ctx: Context = None) -> str:
    """
    Pull latest changes from the remote repository for an environment
    and take appropriate action based on what changed.

    Pulls both the main repository and any extra addon worktrees.

    Analyzes changed files and automatically:
    - Upgrades modules if __manifest__.py version/data/assets changed, or security XML changed
    - Restarts the container if Python files changed
    - Does nothing if only XML/JS changed (--dev=xml hot-reloads XML; JS assets are reloaded on browser refresh)

    Args:
        env_name: The name of the environment to pull updates for.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = env_ops.pull_environment(settings, team, env_name)
    action = result["action"]

    if action == "none":
        return result["message"]

    header_lines = [result["message"]]
    if result.get("modules_installed"):
        header_lines.append(f"Installed: {', '.join(result['modules_installed'])}")
    if result.get("modules_upgraded"):
        header_lines.append(f"Upgraded: {', '.join(result['modules_upgraded'])}")
    header_lines.append(f"Changed files ({len(result.get('changed_files', []))}):")
    for f in result.get("changed_files", [])[:20]:
        header_lines.append(f"  - {f}")
    if len(result.get("changed_files", [])) > 20:
        header_lines.append(f"  ... and {len(result['changed_files']) - 20} more")

    header = "\n".join(header_lines)
    output = result.get("output", "")
    if output:
        return _maybe_cache(output, header, "pull_and_apply", f"env={env_name}")
    return header


# =============================================================================
# MCP Tools — Output cache drill-down
# =============================================================================


@mcp.tool()
@handle_errors
def read_output(
    output_id: str,
    mode: str = "lines",
    start: int = 1,
    end: int = 0,
    grep: str = "",
    ctx: Context = None,
) -> str:
    """
    Read from a cached tool output by its ID.

    After calling tools like install_odoo_modules, upgrade_odoo_modules,
    run_odoo_tests, etc., large outputs are cached on the server. The tool
    response includes an output_id and metadata. Use this tool to explore
    the cached output interactively.

    Modes:
    - "lines" (default): Return a range of lines. Use start/end for pagination
      (1-indexed). Default: first 200 lines. Example: start=100, end=200.
    - "errors": Return only ERROR/WARNING/CRITICAL lines with ±5 lines of context.
    - "grep": Search for a pattern (case-insensitive substring). Returns matching
      lines with line numbers. Combine with start/end to paginate results.
    - "info": Return metadata only — line count, char count, error count, source tool.
    - "tail": Return last 100 lines.

    Args:
        output_id: The cached output ID (e.g. "a3f7c012"), returned by the original tool.
        mode: Read mode — "lines", "errors", "grep", "info", "tail".
        start: First line number to return (1-indexed, default 1). Used with mode="lines" and "grep".
        end: Last line number to return (0 = start+200 for "lines", all results for "grep").
        grep: Search pattern for mode="grep". Case-insensitive substring match.
    """
    import time as _time

    cached = _output_cache.get(output_id)
    if cached is None:
        return f"Output '{output_id}' not found or expired (TTL: 1 hour)."

    lines = cached.lines
    total = cached.total_lines

    if mode == "info":
        return (
            f"Cached output: {output_id}\n"
            f"Source: {cached.source_tool}({cached.source_args})\n"
            f"Lines: {total}\n"
            f"Characters: {cached.total_chars}\n"
            f"Errors/Warnings: {len(cached.error_line_indices)} lines\n"
            f"Age: {int(_time.time() - cached.created_at)}s"
        )

    if mode == "errors":
        if not cached.error_line_indices:
            return "No errors or warnings found in cached output."
        result_lines: list[str] = []
        seen: set[int] = set()
        for idx in cached.error_line_indices:
            ctx_start = max(0, idx - 2)
            ctx_end = min(total, idx + 6)
            for i in range(ctx_start, ctx_end):
                if i not in seen:
                    result_lines.append(f"{i + 1:>6}| {lines[i]}")
                    seen.add(i)
            result_lines.append("")
        return "\n".join(result_lines)

    if mode == "grep":
        if not grep:
            return "Error: grep parameter is required for mode='grep'."
        pattern = grep.lower()
        matches = []
        for i, line in enumerate(lines):
            if pattern in line.lower():
                matches.append(f"{i + 1:>6}| {line}")
        if not matches:
            return f"No matches for '{grep}' in {total} lines."
        s = max(start - 1, 0)
        e = end if end > 0 else s + 200
        page = matches[s:e]
        header = f"Matches for '{grep}': {len(matches)} total (showing {s + 1}-{min(e, len(matches))})"
        return header + "\n" + "\n".join(page)

    if mode == "tail":
        tail = lines[-100:]
        start_num = total - len(tail) + 1
        numbered = [f"{start_num + i:>6}| {ln}" for i, ln in enumerate(tail)]
        return f"Last {len(tail)} lines (of {total}):\n" + "\n".join(numbered)

    # mode == "lines" (default)
    s = max(start - 1, 0)
    e = end if end > 0 else s + 200
    e = min(e, total)
    page = lines[s:e]
    numbered = [f"{s + i + 1:>6}| {ln}" for i, ln in enumerate(page)]
    header = f"Lines {s + 1}-{e} of {total}:"
    return header + "\n" + "\n".join(numbered)


# =============================================================================
# MCP Tools — Odoo operations
# =============================================================================


@mcp.tool()
@handle_errors
@with_env_lock
def install_odoo_modules(env_name: str, modules: str, ctx: Context = None) -> str:
    """
    Install Odoo modules in an environment.

    Args:
        env_name: The name of the environment.
        modules: Comma-separated list of modules to install (e.g., "sale,crm,web").
    """
    modules_list = [m.strip() for m in modules.split(",") if m.strip()]
    if not modules_list:
        return "Error: At least one module name is required."

    settings = _get_settings()
    team = _resolve_team(ctx)
    result = odoo_ops.install_odoo_modules(settings, team, env_name, *modules_list)
    exit_code = result["exit_code"]
    modules_str = ", ".join(result["modules"])
    output = result.get("output", "")
    if exit_code == 0:
        env_ops.restart_environment(settings, env_name)
        header = f"Success. Modules installed: {modules_str}. Container restarted. Exit code: 0."
    else:
        header = f"Error. Modules: {modules_str}. Exit code: {exit_code}."
    return _maybe_cache(
        output,
        header,
        "install_odoo_modules",
        f"env={env_name}, modules={modules}",
    )


@mcp.tool()
@handle_errors
@with_env_lock
def upgrade_odoo_modules(env_name: str, modules: str, ctx: Context = None) -> str:
    """
    Upgrade Odoo modules in an environment.

    Args:
        env_name: The name of the environment.
        modules: Comma-separated list of modules to upgrade (e.g., "sale,crm,web").
    """
    modules_list = [m.strip() for m in modules.split(",") if m.strip()]
    if not modules_list:
        return "Error: At least one module name is required."

    settings = _get_settings()
    team = _resolve_team(ctx)
    result = odoo_ops.upgrade_odoo_modules(settings, team, env_name, *modules_list)
    exit_code = result["exit_code"]
    modules_str = ", ".join(result["modules"])
    output = result.get("output", "")
    status = "Success" if exit_code == 0 else "Error"
    header = f"{status}. Modules: {modules_str}. Exit code: {exit_code}."
    return _maybe_cache(
        output,
        header,
        "upgrade_odoo_modules",
        f"env={env_name}, modules={modules}",
    )


@mcp.tool()
@handle_errors
def read_file_in_odoo(
    env_name: str, path: str, read_range: str = "", ctx: Context = None
) -> str:
    """
    Read a text file or list a directory inside the Odoo container for a specific environment.

    Use this tool to inspect files in the container without constructing shell commands.
    Common use cases:
    - Read Odoo source code (e.g. /usr/lib/python3/dist-packages/odoo/addons/sale/models/sale_order.py)
    - Inspect addon structure (e.g. /mnt/extra-addons/)
    - Check config files (e.g. /etc/odoo/odoo.conf)
    - Verify file presence after pull_and_apply

    If the path is a directory, returns a listing (like `ls -la`).
    If the path is a text file, returns its contents (first 100KB by default).
    Binary files are not supported — use run_odoo_command for binary operations.

    Prefer this tool over run_odoo_command with `cat` or `ls` commands.

    Args:
        env_name: The name of the environment.
        path: Absolute path inside the container (e.g. "/mnt/extra-addons/my_module/__manifest__.py").
        read_range: Optional line range in format "START:END" (e.g. "1:50", "100:200").
                    If omitted, returns the entire file (up to 100KB).
    """
    result = odoo_ops.read_file_in_environment(
        _get_settings(), env_name, path, read_range
    )
    if "error" in result:
        return f"Error: {result['error']}"
    return result["output"]


@mcp.tool()
@handle_errors
@with_env_lock
def write_file_in_odoo(
    env_name: str,
    path: str,
    content: str,
    user: str = "odoo",
    ctx: Context = None,
) -> str:
    """
    Write a text file inside the Odoo container.

    Creates parent directories if they don't exist. Overwrites the file if it
    already exists. Content is transferred via container stdin to avoid
    shell escaping issues.

    Common use cases:
    - Write CSV files for data import
    - Create/modify odoo.conf settings
    - Write one-off Python scripts for odoo shell execution
    - Place test fixture files (demo data, config)

    Do NOT use this to edit source code in the repository — all code changes
    must go through git commit → git push → pull_and_apply.

    Args:
        env_name: The name of the environment.
        path: Absolute path inside the container (e.g. "/tmp/import_data.csv").
        content: Text content to write to the file.
        user: OS user to own the file (default "odoo"). Use "root" for system paths.
    """
    result = odoo_ops.write_file_in_environment(
        _get_settings(), env_name, path, content, user
    )
    return f"File written: {result['path']} ({result['size']} bytes)"


@mcp.tool()
@handle_errors
@with_env_lock
def run_odoo_shell(
    env_name: str,
    python_code: str,
    ctx: Context = None,
) -> str:
    """
    Execute Python code in the Odoo shell context with full ORM access.

    The code runs inside `odoo shell` with access to `self.env`, all Odoo
    models, and the environment's database. Use `print()` to produce output
    that will be returned to you.

    Common use cases:
    - Test computed fields: print(self.env['sale.order'].search([]).mapped('amount_total'))
    - Create test records: self.env['res.partner'].create({'name': 'Test'})
    - Inspect models: print(self.env['ir.model.fields'].search([('model','=','sale.order')]).mapped('name'))
    - Debug business logic: check workflow transitions, access rights
    - Run data-fix scripts

    The code is executed in a single transaction that is committed at the end.
    If the code raises an exception, the transaction is rolled back and the
    traceback is returned.

    Args:
        env_name: The name of the environment.
        python_code: Python code to execute. Use print() for output.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = odoo_ops.run_odoo_shell(settings, team, env_name, python_code)
    exit_code = result["exit_code"]
    output = result.get("output", "")
    status = "Success" if exit_code == 0 else "Error"
    header = f"{status}. Exit code: {exit_code}."
    return _maybe_cache(output, header, "run_odoo_shell", f"env={env_name}")


@mcp.tool()
@handle_errors
def http_request_to_odoo(
    env_name: str,
    path: str,
    method: str = "GET",
    body: str = "",
    headers: str = "",
    session_id: str = "",
    ctx: Context = None,
) -> str:
    """
    Make an HTTP request to the running Odoo instance for a specific environment.

    Useful for testing web controllers, JSON-RPC API, REST endpoints, and
    verifying that Odoo responds correctly. The request is made from the
    host to the container's mapped port.

    Common use cases:
    - Health check: GET /web/health
    - JSON-RPC call: POST /jsonrpc with JSON body
    - Test a custom controller: GET /my/custom/endpoint
    - Verify access rights: check 200 vs 403 responses
    - Test REST API endpoints

    Args:
        env_name: The name of the environment.
        path: URL path (e.g. "/web/health", "/jsonrpc", "/my/invoices").
        method: HTTP method (default "GET"). One of GET, POST, PUT, DELETE.
        body: Request body as a string (typically JSON). Empty for GET requests.
        headers: Comma-separated KEY:VALUE pairs (e.g. "Content-Type:application/json,Accept:text/html").
        session_id: Odoo session ID for authenticated requests. Obtain by calling POST /web/session/authenticate first.
    """
    import json

    settings = _get_settings()
    team = _resolve_team(ctx)
    parsed_headers = None
    if headers:
        parsed_headers = dict(
            item.split(":", 1) for item in headers.split(",") if ":" in item
        )
    result = odoo_ops.http_request_to_odoo(
        settings, team, env_name, path, method, body, parsed_headers, session_id
    )
    status_code = result["status_code"]
    resp_headers = result.get("headers", {})
    resp_body = result.get("body", "")

    lines = [f"HTTP {status_code}"]
    header_items = list(resp_headers.items())[:20]
    if header_items:
        lines.append(f"Headers: {json.dumps(dict(header_items), indent=2)}")
    if resp_body:
        lines.append(f"\nBody:\n{resp_body[:50000]}")
    return "\n".join(lines)


@mcp.tool()
@handle_errors
def search_in_odoo(
    env_name: str,
    pattern: str,
    path: str = "/mnt/extra-addons",
    glob: str = "*.py",
    max_results: int = 50,
    ctx: Context = None,
) -> str:
    """
    Search for a pattern in files inside the Odoo container.

    Runs a recursive grep inside the container and returns matching lines
    with file paths and line numbers. Useful for finding field definitions,
    method implementations, model usage across addons.

    Common use cases:
    - Find where a field is defined: pattern="x_custom_field", path="/mnt/extra-addons"
    - Find model usage: pattern="class SaleOrder", path="/usr/lib/python3/dist-packages/odoo/addons"
    - Find all imports of a module: pattern="from odoo.addons.sale"
    - Find XML record: pattern='id="action_sale_order"', glob="*.xml"

    Args:
        env_name: The name of the environment.
        pattern: Search pattern (fixed string, case-sensitive). Regex is not supported to avoid escaping issues.
        path: Directory to search in (default "/mnt/extra-addons"). Use "/usr/lib/python3/dist-packages/odoo/addons" to search Odoo core.
        glob: File glob pattern (default "*.py"). Use "*.xml" for views/data, "*.js" for frontend, "*" for all files.
        max_results: Maximum number of matching lines to return (default 50).
    """
    result = odoo_ops.search_in_environment(
        _get_settings(), env_name, pattern, path, glob, max_results
    )
    output = result["output"]
    if not output:
        return f"No matches for '{pattern}' in {path} ({glob})."
    header = f"Matches: {result['matches']}"
    if result["truncated"]:
        header += f" (truncated to {max_results})"
    return f"{header}\n\n{output}"


@mcp.tool()
@handle_errors
def list_installed_modules(
    env_name: str,
    name_filter: str = "",
    state_filter: str = "installed",
    ctx: Context = None,
) -> str:
    """
    List Odoo modules and their states in an environment.

    Returns a table of module name, state, and installed version. By default
    shows only installed modules. Use state_filter="" to show all modules.

    Args:
        env_name: The name of the environment.
        name_filter: Filter modules by name (substring match, e.g. "sale" matches "sale", "sale_management", "pos_sale").
        state_filter: Filter by module state (default "installed"). Common values: "installed", "uninstalled", "to upgrade", "to install". Pass empty string to show all states.
    """
    import re as _re

    # Sanitize inputs: only allow alphanumeric, underscore, space, dot
    _safe = _re.compile(r"^[a-zA-Z0-9_ .]*$")
    if name_filter and not _safe.match(name_filter):
        return "Error: name_filter contains invalid characters."
    if state_filter and not _safe.match(state_filter):
        return "Error: state_filter contains invalid characters."

    query = "SELECT name, state, latest_version FROM ir_module_module"
    conditions = []
    if state_filter:
        conditions.append(f"state = '{state_filter}'")
    if name_filter:
        conditions.append(f"name ILIKE '%{name_filter}%'")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY name"

    settings = _get_settings()
    team = _resolve_team(ctx)
    result = odoo_ops.run_db_query(settings, team, env_name, query, "csv")
    return result["output"]


@mcp.tool()
@handle_errors
@with_env_lock
def run_odoo_command(
    env_name: str, command: str, user: str = "odoo", ctx: Context = None
) -> str:
    """
    Execute an arbitrary shell command inside the Odoo container for a specific environment.

    Args:
        env_name: The name of the environment.
        command: The shell command to execute (e.g. "ls /mnt/extra-addons", "python3 -c 'print(1)'").
        user: The OS user to run the command as (default "odoo"). Use "root" for privileged operations.
    """
    result = odoo_ops.run_command_in_environment(
        _get_settings(), env_name, command, user
    )
    exit_code = result["exit_code"]
    output = result.get("output", "")
    status = "Success" if exit_code == 0 else "Error"
    header = f"{status}. Exit code: {exit_code}."
    return _maybe_cache(
        output,
        header,
        "run_odoo_command",
        f"env={env_name}, command={command[:80]}",
    )


@mcp.tool()
@handle_errors
@with_env_lock
def reset_admin_password(
    env_name: str, new_password: str = "test", ctx: Context = None
) -> str:
    """
    Reset the admin user password in the environment's Odoo database.

    Hashes the password using passlib (pbkdf2_sha512) inside the Odoo container
    and updates the res_users record where login = 'admin'.

    Args:
        env_name: The name of the environment.
        new_password: The new password for the admin user (default: "test").
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = odoo_ops.reset_admin_password(settings, team, env_name, new_password)
    return f"Admin password has been reset successfully.\nLogin: {result['login']}\nNew password: {new_password}"


@mcp.tool()
@handle_errors
@with_env_lock
def run_db_query(
    env_name: str,
    query: str,
    output_format: str = "csv",
    max_rows: int = 100,
    ctx: Context = None,
) -> str:
    """
    Execute a SQL query against the environment's PostgreSQL database.

    Args:
        env_name: The name of the environment.
        query: SQL query to execute (e.g. "SELECT id, name FROM res_partner LIMIT 10").
        output_format: "csv" (default, compact for agent consumption) or "human" (pretty table — use when relaying results to the user).
        max_rows: Maximum rows to return (default 100). The query itself is not
                  modified — truncation happens on the output. If more rows are
                  available, a note is appended suggesting to add LIMIT to the query.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = odoo_ops.run_db_query(settings, team, env_name, query, output_format)
    output = result["output"]

    # Truncate by row count
    output_lines = output.splitlines()
    # First line is the header row
    if len(output_lines) > max_rows + 1:
        truncated = output_lines[: max_rows + 1]
        remaining = len(output_lines) - max_rows - 1
        truncated.append(f"... ({remaining} more rows, add LIMIT to your query)")
        output = "\n".join(truncated)

    return _maybe_cache(output, "", "run_db_query", f"env={env_name}")


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
    host_mode: bool = False,
    volumes: str = "",
    privileged: bool = False,
    net_admin: bool = False,
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
        host_mode: Run the container in host network mode instead of the shared Docker network. Use when the service needs direct host network access. Traefik routing still works.
        volumes: Comma-separated volume mounts (e.g. "mydata:/data,config:/etc/app:ro"). Each entry is volume_name:/container/path[:ro|rw]. Volumes must be created first via create_volume.
        privileged: Run the container in privileged mode (full host access). Use with care — implies all Linux capabilities. Mutually exclusive with net_admin (privileged already grants NET_ADMIN).
        net_admin: Add the NET_ADMIN Linux capability. Required for VPN/WireGuard, tun/tap devices, and iptables manipulation inside the container.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    parsed_env = None
    if env_vars:
        parsed_env = dict(
            item.split("=", 1) for item in env_vars.split(",") if "=" in item
        )
    parsed_volumes = volume_ops.parse_volume_mounts(volumes) if volumes else None
    cap_add = ["NET_ADMIN"] if net_admin else None
    result = service_ops.create_service(
        settings,
        team,
        name,
        image,
        port,
        hostname=hostname or None,
        env_vars=parsed_env,
        host_mode=host_mode,
        volumes=parsed_volumes,
        cap_add=cap_add,
        privileged=privileged,
    )
    vol_info = ""
    if parsed_volumes:
        vol_info = "\nVolumes: " + ", ".join(
            f"{v['volume']}:{v['mount_path']}:{v['mode']}" for v in parsed_volumes
        )
    return (
        f"Service created successfully!\n"
        f"Name: {result['name']}\n"
        f"Container: {result['container_name']}\n"
        f"Image: {result['image']}\n"
        f"URL: {result['url']}"
        f"{vol_info}"
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
def restart_service(name: str, ctx: Context = None) -> str:
    """
    Restart a managed auxiliary service container.

    Args:
        name: The name of the service to restart (e.g. "redis", "meilisearch").
    """
    result = service_ops.restart_service(_get_settings(), name)
    return f"Service '{result['name']}' restarted."


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
    preset_volumes = preset.get("volumes") or None
    result = service_ops.create_service(
        settings,
        team,
        name=preset["name"],
        image=preset["image"],
        port=preset["port"],
        hostname=preset.get("hostname") or None,
        env_vars=preset.get("env_vars") or None,
        host_mode=preset.get("host_mode", False),
        volumes=preset_volumes,
    )
    vol_info = ""
    if preset_volumes:
        vol_info = "\nVolumes: " + ", ".join(
            f"{v['volume']}:{v['mount_path']}:{v.get('mode', 'rw')}"
            for v in preset_volumes
        )
    return (
        f"Service restored from preset!\n"
        f"Name: {result['name']}\n"
        f"Container: {result['container_name']}\n"
        f"Image: {result['image']}\n"
        f"URL: {result['url']}"
        f"{vol_info}"
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


@mcp.tool()
@handle_errors
def run_service_command(
    name: str, command: str, user: str = "root", ctx: Context = None
) -> str:
    """
    Execute an arbitrary shell command inside a managed auxiliary service container.

    Args:
        name: The name of the service (e.g. "redis", "meilisearch").
        command: The shell command to execute (e.g. "redis-cli ping", "ls /data").
        user: The OS user to run the command as (default "root").
    """
    result = service_ops.run_command_in_service(_get_settings(), name, command, user)
    exit_code = result["exit_code"]
    output = result.get("output", "")
    status = "Success" if exit_code == 0 else "Error"
    header = f"{status}. Exit code: {exit_code}."
    return _maybe_cache(
        output,
        header,
        "run_service_command",
        f"service={name}, command={command[:80]}",
    )


# =============================================================================
# Volume management
# =============================================================================


@mcp.tool()
@handle_errors
@with_team_lock
def create_volume(
    name: str,
    description: str = "",
    ctx: Context = None,
) -> str:
    """
    Create a named Docker volume for use with services.

    Args:
        name: Short name for the volume (e.g. "redis-data", "meilisearch-data").
        description: Optional description of what this volume is for.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = volume_ops.create_volume(settings, team, name, description=description)
    desc_info = (
        f"\nDescription: {result['description']}" if result["description"] else ""
    )
    return (
        f"Volume created successfully!\n"
        f"Name: {result['name']}\n"
        f"Docker name: {result['docker_name']}"
        f"{desc_info}"
    )


@mcp.tool()
@handle_errors
def list_volumes(ctx: Context = None) -> str:
    """List all managed Docker volumes and their usage by services."""
    settings = _get_settings()
    team = _resolve_team(ctx)
    vols = volume_ops.list_volumes(settings, team)
    if not vols:
        return "No volumes found. Create one with create_volume."
    output = "Managed Volumes:\n"
    for vol in vols:
        used = ", ".join(vol["used_by"]) if vol["used_by"] else "not in use"
        desc = f" — {vol['description']}" if vol.get("description") else ""
        output += f"- {vol['name']}{desc}\n"
        output += f"  Docker name: {vol['docker_name']}\n"
        output += f"  Used by: {used}\n"
    return output


@mcp.tool()
@handle_errors
def inspect_volume(name: str, ctx: Context = None) -> str:
    """
    Get detailed information about a specific volume, including which services use it.

    Args:
        name: The name of the volume to inspect.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    vol = volume_ops.inspect_volume(settings, team, name)
    used = ", ".join(vol["used_by"]) if vol["used_by"] else "not in use"
    desc = f"\nDescription: {vol['description']}" if vol.get("description") else ""
    return (
        f"Volume: {vol['name']}\n"
        f"Docker name: {vol['docker_name']}"
        f"{desc}\n"
        f"Driver: {vol['driver']}\n"
        f"Created: {vol['created_at']}\n"
        f"Mountpoint: {vol['mountpoint']}\n"
        f"Used by: {used}"
    )


@mcp.tool()
@handle_errors
@with_team_lock
def delete_volume(name: str, ctx: Context = None) -> str:
    """
    Delete a managed Docker volume. Fails if the volume is in use by any service.

    Args:
        name: The name of the volume to delete.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = volume_ops.delete_volume(settings, team, name)
    return f"Volume '{result['name']}' deleted."


# =============================================================================
# Volume file operations
# =============================================================================


@mcp.tool()
@handle_errors
def read_file_in_volume(
    name: str,
    path: str,
    read_range: str = "",
    ctx: Context = None,
) -> str:
    """
    Read a text file or list a directory inside a Docker volume.

    Spins up a temporary helper container to access the volume contents.
    If the path is a directory, returns a listing (like ``ls -la``).
    If the path is a text file, returns its contents (up to 100KB by default).
    Binary files are detected and rejected.

    Args:
        name: The name of the volume (e.g. "redis-data").
        path: Path inside the volume (e.g. "data/dump.rdb", "config/redis.conf").
              Leading "/" is optional — paths are relative to the volume root.
        read_range: Optional line range "START:END" (e.g. "1:50", "100:200").
                    If omitted, returns the full file (up to 100KB).
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = volume_file_ops.read_file_in_volume(settings, team, name, path, read_range)
    if "error" in result:
        return f"Error: {result['error']}"
    return result["output"]


@mcp.tool()
@handle_errors
@with_team_lock
def write_file_in_volume(
    name: str,
    path: str,
    content: str,
    ctx: Context = None,
) -> str:
    """
    Write a text file inside a Docker volume.

    Creates parent directories if they don't exist. Overwrites the file if it
    already exists. Uses a temporary helper container to access the volume.

    Args:
        name: The name of the volume (e.g. "redis-data").
        path: Path inside the volume (e.g. "config/my.conf").
              Leading "/" is optional — paths are relative to the volume root.
        content: Text content to write to the file.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = volume_file_ops.write_file_in_volume(settings, team, name, path, content)
    return f"File written: {result['path']} ({result['size']} bytes)"


@mcp.tool()
@handle_errors
def search_in_volume(
    name: str,
    pattern: str,
    path: str = "",
    glob: str = "*",
    max_results: int = 50,
    ctx: Context = None,
) -> str:
    """
    Search for a pattern in files inside a Docker volume.

    Runs a recursive fixed-string grep inside a temporary helper container
    and returns matching lines with file paths and line numbers.

    Args:
        name: The name of the volume (e.g. "redis-data").
        pattern: Search pattern (fixed string, case-sensitive).
        path: Directory to search in, relative to volume root (default: entire volume).
        glob: File glob pattern (default "*"). Use "*.py", "*.xml", "*.conf", etc.
        max_results: Maximum number of matching lines to return (default 50).
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = volume_file_ops.search_in_volume(
        settings, team, name, pattern, path, glob, max_results
    )
    output = result["output"]
    if not output:
        return f"No matches for '{pattern}' in volume '{name}' ({glob})."
    header = f"Matches: {result['matches']}"
    if result["truncated"]:
        header += f" (truncated to {max_results})"
    return f"{header}\n\n{output}"


@mcp.tool()
@handle_errors
@with_team_lock
def delete_file_in_volume(
    name: str,
    path: str,
    ctx: Context = None,
) -> str:
    """
    Delete a file or directory inside a Docker volume.

    Uses a temporary helper container to access the volume.
    Cannot delete the volume root — use delete_volume to remove the entire volume.

    Args:
        name: The name of the volume (e.g. "redis-data").
        path: Path inside the volume to delete (e.g. "data/old-dump.rdb").
              Leading "/" is optional — paths are relative to the volume root.
    """
    settings = _get_settings()
    team = _resolve_team(ctx)
    result = volume_file_ops.delete_file_in_volume(settings, team, name, path)
    if "error" in result:
        return f"Error: {result['error']}"
    return f"Deleted: {result['path']}"


# =============================================================================
# CLI helpers
# =============================================================================


def _ensure_initialized(settings: Settings) -> None:
    """Ensure shared infrastructure and per-team directories exist (idempotent)."""
    _copy_bundled_configs()
    system_ops.init_system(settings)

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
                logger.info("[team.%s] Config: %s", team_id, odoo_conf_dest)

        # Seed team-level sanitization scripts
        sanitize_dest = os.path.join(team.data_dir, "odoo_sanitize")
        os.makedirs(sanitize_dest, exist_ok=True)
        bundled_sql = bundled_dir / "01_disable_mail.sql"
        dest_sql = os.path.join(sanitize_dest, "01_disable_mail.sql")
        if not os.path.isfile(dest_sql) and bundled_sql.is_file():
            shutil.copy2(str(bundled_sql), dest_sql)
            logger.info("[team.%s] Sanitize script: %s", team_id, dest_sql)

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
                        logger.info("[team.%s] Agent guide: %s", team_id, dest_file)

        logger.info(
            "Team %s initialized (workspaces=%s, templates=%s)",
            team_id,
            team.workspaces_dir,
            os.path.join(team.data_dir, "templates"),
        )

    global _instance_id  # noqa: PLW0603
    from oduflow.telemetry import record_startup

    _instance_id = record_startup(
        settings.etc_dir, _get_version(), settings.disable_telemetry
    )


def _run_upgrade(settings: Settings) -> None:
    """Overwrite bundled agent guides and sanitize scripts with latest versions."""
    import pathlib
    import shutil

    bundled_dir = pathlib.Path(__file__).resolve().parent / "templates"
    bundled_guides_dir = bundled_dir / "agent_guides"
    etc_dir = pathlib.Path(settings.etc_dir)

    # --- Collect files that will be written ---
    files_to_write: list[tuple[str, str, str]] = []  # (label, src, dest)

    # System-level: postgresql.conf
    pg_conf_src = bundled_dir / "postgresql.conf"
    pg_conf_dest = etc_dir / "postgresql.conf"
    if pg_conf_src.is_file():
        files_to_write.append(("[system]", str(pg_conf_src), str(pg_conf_dest)))

    # Per-team files
    for team_id, team in settings.teams.items():
        label = f"[team.{team_id}]"

        # odoo.conf
        bundled_odoo_conf = bundled_dir / "odoo.conf"
        if bundled_odoo_conf.is_file():
            dest = os.path.join(team.data_dir, "odoo.conf")
            files_to_write.append((label, str(bundled_odoo_conf), dest))

        # Agent guides
        agent_guides_dest = os.path.join(team.data_dir, "agent_guides")
        if bundled_guides_dir.is_dir():
            for guide_file in sorted(bundled_guides_dir.iterdir()):
                if guide_file.is_file() and guide_file.suffix == ".md":
                    dest = os.path.join(agent_guides_dest, guide_file.name)
                    files_to_write.append((label, str(guide_file), dest))

        # Sanitize scripts
        bundled_sql = bundled_dir / "01_disable_mail.sql"
        if bundled_sql.is_file():
            dest = os.path.join(team.data_dir, "odoo_sanitize", "01_disable_mail.sql")
            files_to_write.append((label, str(bundled_sql), dest))

    if not files_to_write:
        print("Nothing to upgrade — no bundled files found.")
        return

    # --- Filter to only new or changed files (compare by size) ---
    files_to_update: list[tuple[str, str, str, str]] = []  # (label, src, dest, tag)
    files_kept: list[tuple[str, str]] = []  # (label, dest)
    for label, src, dest in files_to_write:
        if not os.path.isfile(dest):
            files_to_update.append((label, src, dest, "new"))
        elif os.path.getsize(src) != os.path.getsize(dest):
            # Check if the deployed file is marked with # KEEP on the first line
            try:
                with open(dest, "r", encoding="utf-8", errors="replace") as f:
                    first_line = f.readline().strip()
                if first_line == "# KEEP":
                    files_kept.append((label, dest))
                    continue
            except OSError:
                pass
            files_to_update.append((label, src, dest, "changed"))

    if not files_to_update and not files_kept:
        print("\nAll bundled files are already up to date. Nothing to do.")
        return

    if not files_to_update and files_kept:
        print("\nAll changed files are marked with # KEEP. Nothing to overwrite.")
        for label, dest in files_kept:
            print(f"  {label} {dest} (kept)")
        return

    # --- Warning banner ---
    print()
    print("=" * 70)
    print("  WARNING: The following files will be OVERWRITTEN")
    print("  with bundled versions from this Oduflow release.")
    print("=" * 70)
    print()
    for label, _src, dest, tag in files_to_update:
        print(f"  {label} {dest} ({tag})")
    if files_kept:
        print()
        print(
            "  The following files are marked with # KEEP and will NOT be overwritten:"
        )
        for label, dest in files_kept:
            print(f"  {label} {dest} (kept)")
    print()
    print("  If you have made custom changes to any of these files,")
    print("  press Ctrl+C NOW and back them up before proceeding.")
    print()

    try:
        input("  Press Enter to continue or Ctrl+C to abort... ")
    except KeyboardInterrupt:
        print("\n\nAborted. No files were changed.")
        return

    # --- Overwrite ---
    count = 0
    for label, src, dest, tag in files_to_update:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)
        count += 1
        print(f"  Updated: {dest}")

    print(f"\nDone. Updated {count} file(s) across {len(settings.teams)} team(s).")


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
                    logger.info("Config: %s", dest)
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


def _run_template_from_env(
    settings: Settings, team: TeamSettings, branch: str, template_name: str = ""
) -> None:
    result = system_ops.publish_env_as_template(
        settings, team, env_name=branch, template_name=template_name
    )
    print(
        f"Environment '{result['env_name']}' saved as template '{template_name}'.\n"
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
            f"  {r['template_name']}: DB={db_status} ({r['template_db']}), SQL={'yes' if r['has_sql'] else 'no'}, Filestore={'yes' if r['has_filestore'] else 'no'}, Mode={overlay_status}{size_info}"
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
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    sub = parser.add_subparsers(dest="command", title="commands", metavar="")

    # --- System commands ---
    sub.add_parser("destroy", help="Destroy all shared infrastructure")
    sub.add_parser(
        "upgrade",
        help="Overwrite bundled agent guides and sanitize scripts with the latest version",
    )

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
    p_reload.add_argument(
        "--source",
        default="",
        help="Sync template from s3://... or local path before reloading",
    )
    p_reload.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress info logging (for cron)",
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
    logging.getLogger("docket").setLevel(logging.WARNING)

    # Bootstrap: if no config exists, copy the bundled default
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
        logger.info("Config created: %s", dest)

    global _settings
    _settings = _get_settings()
    logger.info("conf=%s  data=%s", _settings.etc_dir, _settings.base_data_dir)

    # --- Resolve team for CLI commands that need it ----------------

    def _cli_team() -> TeamSettings:
        team_id = getattr(args, "team", "1")
        return _settings.get_team(team_id)

    # --- Commands that need Settings --------------------------------

    if args.command is None:
        # No subcommand → start the MCP server
        _ensure_initialized(_settings)
        if args.transport == "stdio":
            _start_stdio()
        else:
            _start_http()
        return

    if args.command == "destroy":
        _run_destroy(_settings)
        return

    if args.command == "upgrade":
        _run_upgrade(_settings)
        return

    if args.command == "reload-template":
        if args.quiet:
            logging.getLogger("oduflow").setLevel(logging.WARNING)
        if args.source:
            from oduflow.sync import sync_template_from_source

            result = sync_template_from_source(
                _settings,
                _cli_team(),
                args.template_name,
                args.source,
            )
            msg = (
                f"Template DB {result['status']}.\nTemplate DB: {result['template_db']}"
            )
            if "restore_seconds" in result:
                msg += f"\nDB restore time: {result['restore_seconds']}s"
            if not args.quiet:
                print(msg)
        else:
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


def _start_stdio() -> None:
    """Start the MCP server (stdio transport)."""
    import asyncio

    try:
        asyncio.run(mcp.run_stdio_async())
    except KeyboardInterrupt:
        logger.info("Shutting down.")


def _start_http() -> None:
    """Start the MCP server (HTTP transport)."""
    from fastmcp.server.http import create_streamable_http_app

    settings = _get_settings()
    host = "0.0.0.0" if settings.routing_mode == "traefik" else settings.host
    port = settings.port

    auth = _build_auth(settings)

    app = create_streamable_http_app(mcp, "/mcp", auth=auth, stateless_http=True)

    from oduflow.web_ui import mount_web_ui

    mount_web_ui(app, _get_settings, _locks)

    for tid, team in settings.teams.items():
        if settings.routing_mode == "traefik":
            url = f"https://{team.hostname}/"
        else:
            url = f"http://{host}:{port}/"
        mcp_status = "MCP token ON" if team.auth_token else "MCP token OFF"
        oauth_status = "OAuth ON" if settings.oauth_enabled else "OAuth OFF"
        ui_status = "UI auth ON" if team.ui_password else "UI auth OFF"
        logger.info(
            "[team.%s] %s (%s, %s, %s)", tid, url, mcp_status, oauth_status, ui_status
        )

    import uvicorn

    uvicorn.run(app, host=host, port=port, ws="websockets-sansio")


def _build_auth(settings: Settings):  # type: ignore[no-untyped-def]
    """Build the auth provider from settings.

    Supports three modes (auto-detected):
    - Static tokens only (auth_token in team sections)
    - GitHub OAuth only (oauth_* in server/oauth section)
    - Both combined: static tokens checked first, GitHub OAuth fallback
    """
    # Collect static tokens
    tokens: dict[str, dict[str, object]] = {}
    for team_id, team in settings.teams.items():
        if team.auth_token:
            tokens[team.auth_token] = {"client_id": team_id, "scopes": []}

    has_tokens = bool(tokens)
    has_oauth = settings.oauth_enabled

    if has_oauth and has_tokens:
        # Both: GitHub OAuth provider with composite verifier
        return _build_github_auth(settings, static_tokens=tokens)

    if has_oauth:
        # GitHub OAuth only
        return _build_github_auth(settings)

    if has_tokens:
        # Static tokens only (current behavior)
        from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

        return StaticTokenVerifier(tokens=tokens)

    logger.warning("HTTP auth DISABLED (no auth_token or OAuth configured)")
    return None


def _build_github_auth(
    settings: Settings,
    static_tokens: dict[str, dict[str, object]] | None = None,
) -> object:
    """Build GitHubProvider with optional static token fallback."""
    from fastmcp.server.auth.providers.github import (
        GitHubProvider,
        GitHubTokenVerifier,
    )

    if static_tokens:
        from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

        static_verifier = StaticTokenVerifier(tokens=static_tokens)
        github_verifier = GitHubTokenVerifier()
        token_verifier = _CompositeTokenVerifier(static_verifier, github_verifier)

        from fastmcp.server.auth.oauth_proxy import OAuthProxy

        return OAuthProxy(
            upstream_authorization_endpoint="https://github.com/login/oauth/authorize",
            upstream_token_endpoint="https://github.com/login/oauth/access_token",
            upstream_client_id=settings.oauth_client_id,
            upstream_client_secret=settings.oauth_client_secret,
            token_verifier=token_verifier,
            base_url=settings.oauth_base_url,
            require_authorization_consent=False,
        )

    return GitHubProvider(
        client_id=settings.oauth_client_id,
        client_secret=settings.oauth_client_secret,
        base_url=settings.oauth_base_url,
        require_authorization_consent=False,
    )


class _CompositeTokenVerifier:
    """Token verifier that tries static tokens first, then GitHub API.

    Implements the TokenVerifier protocol (verify_token + required_scopes)
    without inheriting from it to avoid pulling in pydantic BaseModel.
    """

    required_scopes: list[str] | None = None

    def __init__(self, *verifiers: object) -> None:
        self.verifiers = verifiers

    async def verify_token(self, token: str):  # type: ignore[no-untyped-def]
        for verifier in self.verifiers:
            result = await verifier.verify_token(token)  # type: ignore[union-attr]
            if result is not None:
                return result
        return None


if __name__ == "__main__":
    main()
