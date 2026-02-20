# Internals

[TOC]

## Architecture

```
┌──────────────────────────────────────────────────┐
│                   MCP Clients                    │
│         (Cursor, Cline, Amp, Claude, …)          │
└────────────────────┬─────────────────────────────┘
                     │  MCP (Streamable HTTP / stdio)
┌────────────────────▼─────────────────────────────┐
│  server.py — FastMCP transport layer             │
│  • MCP tool definitions (32 tools)               │
│  • Global mutex for heavy operations             │
│  • Unified error handler (FlowError → ValueError)│
│  • Web UI mount (Starlette)                      │
│  • Bearer token auth (MCP) / Basic auth (Web UI) │
└────────────────────┬─────────────────────────────┘
                     │
     ┌───────────────┼───────────────────┐
     │               │                   │
     ▼               ▼                   ▼
 system_ops      env_ops             service_ops
 (init/destroy/  (create/delete/     (create/delete/
  template mgmt)  start/stop/         update/list/
                  restart/list/       logs)
                  pull/exec)
     │               │                   │
     │               ▼                   │
     │           odoo_ops                │
     │           (install/upgrade/       │
     │            test/logs/exec)        │
     │               │                   │
     └───────────────┼───────────────────┘
                     │
              Docker SDK (docker-py)
                     │
     ┌───────────────┼────────────────────┐
     ▼               ▼                    ▼
  oduflow-net    oduflow-db          oduflow-{branch}-odoo
  (network)      (PostgreSQL)        (Odoo containers)
                                     oduflow-svc-{name}
                                     (Service containers)
```

### Key Architectural Decisions

| Decision | Rationale |
|---|---|
| Single process, single uvicorn worker | Designed for a single developer or small team; no shared-state problems |
| `threading.Lock` mutex | Heavy operations (create/delete env, install modules) reject concurrent requests with `BusyError` instead of queuing |
| Docker SDK only (no subprocess for Docker) | Consistent error handling; `put_archive` replaces `docker cp` |
| fuse-overlayfs for filestore | Copy-on-write sharing of a large template filestore across all environments |
| Stable port registry (`ports.json`) | Port assignments survive container restarts; eliminates TOCTOU race conditions |
| Typed error hierarchy | `FlowError` base with `NotFoundError`, `BusyError`, `ConflictError`, `PrerequisiteNotMetError`, `ExternalCommandError` — clients can distinguish error types |
| Traefik routing mode (optional) | Automatic HTTPS with Let's Encrypt for production-like setups |
| Dual dump format support | Accepts both plain SQL (`.sql`) and PostgreSQL custom format (`.pgdump`) dumps |
| Auto-detection of UID/GID | Resolves Odoo container's UID:GID from the image to set correct file permissions |

## Project Structure

```
src/oduflow/
  server.py            # MCP transport: tool definitions, error handler, mutex, CLI
  settings.py          # @dataclass Settings with from_env() and validate()
  errors.py            # FlowError hierarchy (6 error classes)
  models.py            # EnvironmentRef dataclass
  naming.py            # Pure functions: slugify, db name, resource name, paths, URL sanitization
  git_ops.py           # Git clone, pull, credential management, manifest parsing
  git_analysis.py      # Classify changed files → install / upgrade / restart / refresh
  port_registry.py     # Stable port allocation with JSON persistence
  web_ui.py            # Starlette-based dashboard, REST API, Basic auth middleware
  extra_addons.py      # Extra addon repo management (clone, worktree, odoo.conf generation)
  licensing.py         # License verification and installation (RSA signatures)

  docker_ops/
    client.py           # docker.from_env() wrapper + UID/GID auto-detection
    system_ops.py       # init_system / destroy_system / reload_template / init_template /
                        # template_up / template_down / publish_env_as_template / drop_template / list_templates
    env_ops.py          # create / delete / start / stop / restart / rebuild / list / status / pull /
                        # apt/pip auto-install / filestore overlay mount
    odoo_ops.py         # install / upgrade / test / logs / exec_in_environment
    service_ops.py      # create / delete / update / list / logs for auxiliary services
    service_presets.py  # Save / restore / list / delete service preset configurations
    stats.py            # Container and system CPU/RAM stats (parallel collection)

  templates/
    odoo.conf             # Odoo configuration template (addons path, limits, security)
    postgresql.conf       # PostgreSQL tuning (shared_buffers, WAL, autovacuum, etc.)
    dashboard.html        # Web dashboard UI (single-page application)
    favicon.ico           # Dashboard favicon
    agent_guides/         # AI agent guides (copied to $ODUFLOW_HOME/agent_guides on init-instance)
      agent_guide.md      # Main agent instructions for Oduflow MCP tools
      odoo_15_guide.md    # Odoo 15 development standards
      odoo_16_guide.md    # Odoo 16 development standards
      odoo_17_guide.md    # Odoo 17 development standards
      odoo_18_guide.md    # Odoo 18 development standards
      odoo_19_guide.md    # Odoo 19 development standards

tests/                  # Unit and integration tests (pytest)
```

## Environment Workspace Structure

Each branch gets an isolated workspace:

```
$ODUFLOW_HOME/workspaces/{branch}/
  repo/                ← shallow git clone (--depth 1)
  filestore_upper/     ← overlay upper layer (branch-specific changes)
  filestore_work/      ← overlay work directory (required by overlayfs)
  filestore/           ← merged overlay mount (bound into the container)
  sessions/            ← Odoo session storage
```

When `template_name="none"` (no template), the filestore is a plain directory (no overlay).

## Docker Resources

| Resource | Name | Description |
|---|---|---|
| **Network** | `oduflow-net` | Shared bridge network for all containers |
| **DB container** | `oduflow-db` | PostgreSQL 15, shared across all environments |
| **DB volume** | `oduflow-db-data` | Persistent database storage |
| **Template DB** | `odoo_ref_<name>` | Created from the dump file, used as PostgreSQL template |
| **Odoo containers** | `oduflow-{branch}-odoo` | One per environment |
| **Service containers** | `oduflow-svc-{name}` | One per auxiliary service |
| **Traefik** (optional) | `oduflow-traefik` | Reverse proxy with auto-HTTPS |
| **Traefik volume** (optional) | `oduflow-traefik-acme` | Let's Encrypt certificate storage |

All containers are labeled with `oduflow.managed=true` for discovery and management.

## Error Handling

Oduflow uses a typed error hierarchy for clear error reporting:

| Error | HTTP Status | Description |
|---|:---:|---|
| `FlowError` | 400 | Base error for all operations |
| `BusyError` | 409 | Another mutexed operation is in progress |
| `NotFoundError` | 404 | Environment, service, or resource not found |
| `ConflictError` | 400 | Resource already exists (e.g. environment already running) |
| `PrerequisiteNotMetError` | 400 | System not initialized, Docker not running, or dependency missing |
| `ExternalCommandError` | 400 | Git, psql, or Docker command failed (includes command, exit code, output) |
| `ProtectedError` | 400 | Environment is protected and cannot be deleted |

MCP clients receive errors as `ValueError` with a descriptive message. REST API clients receive JSON with `{"ok": false, "error": "..."}`.

## PostgreSQL Tuning

The bundled `postgresql.conf` is optimized for a 2 vCPU / 4 GB RAM development server:

- **1 GB shared buffers** (25% of RAM)
- **16 MB work_mem** per query
- **256 MB maintenance_work_mem** for VACUUM and CREATE INDEX
- **WAL tuning**: 512 MB–2 GB WAL size, 15-minute checkpoint timeout
- **Aggressive autovacuum**: 30s naptime, 5% scale factor
- **Slow query logging**: queries over 1 second
- **HDD-optimized**: random_page_cost=4.0, effective_io_concurrency=2
