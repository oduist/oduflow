# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Oduflow

AI-first Odoo development and CI tool. Provisions isolated, ephemeral Odoo environments on Docker (one per git branch) and exposes them to AI coding agents via MCP. Python 3.10+, built on FastMCP.

## Commands

```bash
# Install from source (editable)
pip install -e .

# Run the server (HTTP transport by default)
oduflow

# Lint
ruff check src/oduflow tests

# Format
ruff format src/oduflow tests

# Type check
mypy src/oduflow

# Tests (default: excludes heavyweight)
pytest

# Run only unit tests (no Docker required)
pytest -m "not integration and not heavyweight"

# Run integration tests (requires Docker)
pytest -m integration

# Run heavyweight tests (slow: full init/destroy cycles)
pytest -m heavyweight

# Run a single test
pytest tests/test_naming.py::test_slugify_branch -v

# Build & deploy docs
pip install -r requirements-docs.txt
mkdocs gh-deploy --force
```

## Architecture

```
MCP Clients (Cursor, Claude, etc.)
        │ MCP (Streamable HTTP)
        ▼
   server.py ── FastMCP + CLI entry point (32 tools)
        │         ├── @handle_errors decorator → ToolError
        │         └── LockManager (per-branch / per-team / system) → BusyError
        ├── web_ui.py ── Starlette dashboard + REST API + Basic auth
        ├── settings.py ── @dataclass Settings, loads from oduflow.toml (TOML)
        ├── locking.py ── LockManager with per-branch, per-team, system locks
        ├── git_ops.py ── Clone, credentials, manifest parsing
        ├── git_analysis.py ── Classify changed files → action (install/upgrade/restart/nothing)
        ├── naming.py ── Pure functions: slugify, DB names, paths
        ├── extra_addons.py ── Extra addon repo management (bare clones + worktrees)
        ├── port_registry.py ── Stable port allocation (ports.json)
        ├── env_credentials.py ── Per-environment PostgreSQL credentials
        ├── sanitizer.py ── DB sanitization (SQL/Python scripts)
        └── docker_ops/
            ├── client.py ── Docker SDK wrapper, UID/GID detection
            ├── system_ops.py ── init_system, destroy, template management
            ├── env_ops.py ── Environment create/delete, overlay filesystems
            ├── odoo_ops.py ── Module install/upgrade/test, exec, logs
            ├── service_ops.py ── Auxiliary services (Redis, Meilisearch, etc.)
            ├── service_presets.py ── Save/restore service configs
            └── stats.py ── Container and system metrics
```

**Key patterns:**
- Every MCP tool is a function in `server.py` decorated with `@mcp.tool()`, `@handle_errors`, and optionally `@with_branch_lock` or `@with_team_lock`
- Granular locking via `LockManager`: per-branch, per-team, and system locks; operations on different branches run in parallel
- Error hierarchy: `FlowError` → `BusyError | NotFoundError | ConflictError | PrerequisiteNotMetError | ExternalCommandError | ProtectedError` (in `errors.py`)
- Settings are a `@dataclass` loaded from `oduflow.toml` via `Settings.from_toml()`; multi-team via `[team.*]` sections
- Filestore isolation: small templates use plain copies; large ones use fuse-overlayfs (threshold: `overlay_threshold_mb`)
- File ownership: `os.chown()` on Linux, fallback to container-based `chown` on macOS

## Testing

- Test markers: `integration` (needs Docker), `heavyweight` (slow full cycles)
- Default `addopts` in `pyproject.toml` excludes heavyweight tests
- Mocking pattern: patch `oduflow.docker_ops.*` functions, not Docker SDK directly
- `conftest.py` at root ignores `src/**` for collection (tests live in `tests/`)

## Documentation

After committing changes to `docs/` or `mkdocs.yml`, auto-publish to GitHub Pages:
```bash
source .venv/bin/activate && mkdocs gh-deploy --force
```
Site: https://oduist.github.io/oduflow/

## Agent workflow
@AGENTS.md
