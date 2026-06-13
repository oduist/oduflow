# Agent Instructions

This file provides guidance to Claude Code (claude.ai/code) and other AI coding agents when working with code in this repository.

## What is Oduflow

AI-first Odoo development and CI tool. Provisions isolated, ephemeral Odoo environments on Docker (one per git branch) and exposes them to AI coding agents via MCP. Python 3.10+, built on FastMCP.

## Design Context

Any change to the web dashboard (`src/oduflow/templates/dashboard.html`, served by `web_ui.py`) must follow the project's design docs:

- `PRODUCT.md` — register (product), users, brand personality, anti-references, design principles.
- `DESIGN.md` — normative visual system ("The Engineer's Console", shared with oduflow.dev): color tokens, typography, components, do's and don'ts.

Read both before touching dashboard UI. Key hard rules: no external CDNs (all assets ship with the package), every `var(--*)` must be declared in `:root`, status is never conveyed by color alone, no emoji as UI affordances.

## Commands

```bash
# Install from source (editable)
pip install -e .

# Run the server (stdio transport by default)
oduflow

# Run in HTTP mode (for remote/multi-user)
oduflow --transport http

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
   server.py ── FastMCP + CLI entry point (43 tools)
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

## Plan First, Then Act

Before making any code changes, always:
1. Explain your understanding of the task
2. Present a detailed plan of changes (which files will be created/modified, what exactly will change)
3. Ask for explicit confirmation before proceeding with implementation

Do NOT write or modify any code until the user explicitly approves the plan.

## Env

If you run into any missing python dependency errors, try running your command with source .venv/bin/activate
to assume the python venv.

## Publishing Documentation

Documentation is published to GitHub Pages **automatically** by `.github/workflows/docs.yml`: on every push to `main` touching `docs/`, `mkdocs.yml`, or `requirements-docs.txt`, it installs `requirements-docs.txt` and runs `mkdocs gh-deploy --force` to update the `gh-pages` branch, which GitHub's `pages-build-deployment` then publishes live.

Do NOT run `mkdocs gh-deploy` (or otherwise deploy docs) from a working/feature branch — that would push unmerged content live. Just commit the `docs/`/`mkdocs.yml` changes as part of your branch; the site updates once they merge to `main`. A manual redeploy is available via the workflow's `workflow_dispatch` trigger.

The site is hosted at: https://docs.oduflow.dev/

## Publishing a New Version

When preparing a new release, update the product version before creating the tag:

1. Update `pyproject.toml` to the new version.
2. Update `docs/changelog.md` so the top section is titled with the new version and includes the release notes for the changes being published.
3. Commit and push those version/changelog changes to `main`.
4. Create the annotated tag (for example `git tag -a vX.Y.Z -m "vX.Y.Z"`) on the final `main` commit and push the tag.

Do not create or move a release tag until the version bump and changelog commit is already on `main`; the PyPI publishing workflow reads the package version from the tagged commit.

## Publishing Docker Image

When asked to publish a Docker image, build and push to Docker Hub:

```bash
# Read version from pyproject.toml, then:
docker build -t oduist/oduflow:<VERSION> -t oduist/oduflow:latest .
docker push oduist/oduflow:<VERSION>
docker push oduist/oduflow:latest
```

Registry: hub.docker.com, repository: `oduist/oduflow`
