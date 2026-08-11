# Internals

## Architecture

```
┌──────────────────────────────────────────────────┐
│                   MCP Clients                    │
│         (Cursor, Cline, Amp, Claude, …)          │
└────────────────────┬─────────────────────────────┘
                     │  MCP (stdio or Streamable HTTP)
┌────────────────────▼─────────────────────────────┐
│  server.py — FastMCP transport layer             │
│  • Public MCP tool definitions                    │
│  • Per-branch / per-team / system locking        │
│  • Unified error handler (FlowError → ToolError) │
│  • Web UI mount (Starlette)                      │
│  • Bearer auth (MCP) / session+Basic auth (UI)   │
│  • Team resolution (token → Host → default)      │
└────────────────────┬─────────────────────────────┘
                     │
     ┌───────────────┼───────────────────┬────────────────────┐
     │               │                   │                    │
     ▼               ▼                   ▼                    ▼
 system_ops      env_ops             service_ops       production_ops
 (infrastructure (dev environment    (services,        (production deploy,
  + templates)    lifecycle/sync)     presets/volumes)  rollback/lifecycle)
     │               │                                        │
     │               ▼                                        ▼
     │           odoo_ops                              backup_ops / WAL-G
     │           (modules, tests,                      (snapshots, retention,
     │            shell, ORM, SQL)                      cluster PITR)
     │               │                                        │
     └───────────────┴────────────────────────────────────────┘
                     │
              Docker SDK (docker-py)
                     │
     ┌───────────────┼────────────────────┐
     ▼               ▼                    ▼
oduflow-{team}-net oduflow-db      oduflow-{team}-{branch}-odoo
  (per-team net)   (PostgreSQL)    (Odoo containers)
                                   oduflow-{team}-svc-{name}
                                   (Service containers)
```

### Key Architectural Decisions

| Decision | Rationale |
|---|---|
| Single process, single uvicorn worker | Designed for a single developer or small team; no shared-state problems |
| NATS/JetStream operation coordinator | Mutations survive transport timeouts; named resources serialize conflicts while unrelated work stays parallel |
| Docker SDK only (no subprocess for Docker) | Consistent error handling; `put_archive` replaces `docker cp` |
| fuse-overlayfs for filestore | Copy-on-write sharing of a large template filestore across all environments |
| Stable port registry (`ports.json`) | Port assignments survive container restarts; eliminates TOCTOU race conditions |
| Typed error hierarchy | `FlowError` base with `NotFoundError`, `BusyError`, `ConflictError`, `PrerequisiteNotMetError`, `ExternalCommandError`, `ProtectedError` — clients can distinguish error types |
| Traefik routing mode (optional) | Automatic HTTPS with Let's Encrypt for production-like setups |
| Dual dump format support | Accepts both plain SQL (`.sql`) and PostgreSQL custom format (`.pgdump`) dumps |
| Auto-detection of UID/GID | Resolves Odoo container's UID:GID from the image to set correct file permissions |
| TOML-based multi-team config | Per-team isolation with shared infrastructure; settings loaded from `oduflow.toml` |

## Project Structure

```
src/oduflow/
  server.py            # MCP transport: tool definitions, error handler, locking, CLI
  settings.py          # @dataclass Settings, loads from oduflow.toml (TOML)
  errors.py            # FlowError hierarchy (7 error classes)
  models.py            # EnvironmentRef dataclass
  naming.py            # Pure functions: slugify, db name, resource name, paths, URL sanitization
  locking.py           # LockManager with per-branch, per-team, and system locks
  git_ops.py           # Git clone, pull, credential management, manifest parsing
  git_analysis.py      # Classify changed files → install / upgrade / restart / refresh
  port_registry.py     # Stable port allocation with JSON persistence
  web_ui.py            # Starlette dashboard, REST/WS API, session+Basic auth middleware
  extra_addons.py      # Extra addon repo management (clone, worktree, odoo.conf generation)
  env_credentials.py   # Per-environment PostgreSQL credentials
  sanitizer.py         # DB sanitization (SQL/Python scripts)
  sync.py              # Sync template data from S3 or local path (aws s3 sync / rsync)
  licensing.py         # License verification and installation (RSA signatures)
  systemd.py           # Systemd service install/uninstall
  production_registry.py # Per-team production metadata and deploy history
  backup_ops.py        # Production snapshot/restore orchestration
  backup_scheduler.py  # Scheduled snapshots, base backups, and retention
  walg.py              # WAL-G archive/base-backup/PITR integration
  chunkstore/          # Deduplicated filestore snapshot engine
  agent_sessions.py    # Hosted-agent conversation selection/history

  docker_ops/
    client.py           # docker.from_env() wrapper + UID/GID auto-detection
    system_ops.py       # init_system / destroy_system / reload_template / init_template /
                        # save_env_as_template / delete_template / list_templates
    env_ops.py          # create / delete / start / stop / restart / update / list / status / pull /
                        # apt/pip auto-install / filestore overlay mount
    production_ops.py   # production create/deploy/rollback/lifecycle
    odoo_ops.py         # install / upgrade / test / logs / shell / ORM / SQL / search / run_command
    service_ops.py      # create / delete / update / list / logs for auxiliary services
    service_presets.py  # Save / restore / list / delete service preset configurations
    volume_ops.py       # Managed Docker volume lifecycle
    volume_file_ops.py  # Read/write/search/delete files in managed volumes
    stats.py            # Container and system CPU/RAM stats (parallel collection)

  templates/
    oduflow.toml          # Default TOML configuration (copied on first startup)
    odoo.conf             # Odoo configuration template (addons path, limits, security)
    postgresql.conf       # PostgreSQL tuning (shared_buffers, WAL, autovacuum, etc.)
    dashboard.html        # Web dashboard UI (single-page application)
    favicon.ico           # Dashboard favicon
    agent_guides/         # AI agent guides (copied to team data dirs on init)
      agent_instructions.md # Main agent instructions for Oduflow MCP tools
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
{data_dir}/team_{ID}/workspaces/{branch}/
  repo/                ← shallow git clone (--depth 1)
  filestore_upper/     ← overlay upper layer (branch-specific changes)
  filestore_work/      ← overlay work directory (required by overlayfs)
  filestore/           ← merged overlay mount (bound into the container)
  sessions/            ← Odoo session storage
```

When `template_name="none"` (no template), the filestore is a plain directory (no overlay).

You can verify active overlay mounts with `df -h` — each environment with a template gets its own `fuse-overlayfs` mount:

```
$ df -h
Filesystem                         Size  Used Avail Use% Mounted on
/dev/mapper/ubuntu--vg-ubuntu--lv   97G   74G   19G  81% /
fuse-overlayfs                      97G   74G   19G  81% /srv/oduflow/team_1/workspaces/manuf-plan/filestore
fuse-overlayfs                      97G   74G   19G  81% /srv/oduflow/team_1/workspaces/fixing-landing/filestore
```

## File Ownership (macOS vs Linux)

Odoo containers run as `uid=101 gid=101`. Oduflow must set this ownership on
workspace files so the container can read/write them. The behaviour differs
between platforms:

| | Linux | macOS (Docker Desktop) |
|---|---|---|
| **Docker runtime** | Native — UID/GID are shared between host and container | Runs inside a Linux VM; files are projected via VirtioFS |
| **Host file ownership** | Matches container UID (e.g. `101:101`) | Always shown as the macOS user regardless of in-container owner |
| **`os.chown` from host** | Works (when running as root) | Raises `PermissionError` — VirtioFS ignores host-side chown |

To handle both platforms transparently, Oduflow uses **`chown_recursive()`**
(`docker_ops/client.py`):

1. **Try host-side `os.chown`** — fast, works on Linux.
2. **On `PermissionError`** — fall back to `chown -R` inside a throwaway
   container with the target path bind-mounted. The chown happens inside the
   VM where it takes effect normally.

This means no manual ownership fixups are ever needed on either platform.

## Docker Resources

| Resource | Name | Description |
|---|---|---|
| **Network** | `oduflow-{team_id}-net` | Per-team isolated bridge network (only shared PostgreSQL and the Traefik bridge cross teams) |
| **DB container** | `oduflow-db` | PostgreSQL 15, shared across all environments |
| **DB volume** | `oduflow-db-data` | Persistent database storage |
| **NATS container** | `oduflow-nats` | Local JetStream operation queue |
| **NATS volume** | `oduflow-nats-data` | Durable commands, current state, and retained output |
| **Template DB** | `oduflow_template_{team_id}_{name}` | Created from the dump file, used as PostgreSQL template |
| **Environment DB** | `oduflow_{team_id}_{branch}` | Created from template DB via `CREATE DATABASE ... TEMPLATE` |
| **Odoo containers** | `oduflow-{team_id}-{branch}-odoo` | One per environment |
| **Service containers** | `oduflow-{team_id}-svc-{name}` | One per auxiliary service; also its internal DNS hostname on the team network |
| **Traefik** (optional) | `oduflow-traefik` | Reverse proxy with auto-HTTPS |
| **Traefik volume** (optional) | `oduflow-traefik-acme` | Let's Encrypt certificate storage |

All containers are labeled with `oduflow.managed=true` and `oduflow.team={team_id}` for discovery and management.

## Durable operations and concurrency

Mutations enter one NATS/JetStream work queue and declare named resources such
as `env:1:feature`, `template:1:clean`, or `production:1:erp`. The embedded
coordinator starts an operation only after all its resources are free.
Operations on different resources run in parallel; conflicts are FIFO per
resource instead of returning `BusyError`.

The command survives an MCP/HTTP timeout and a server restart. JetStream KV
stores current state, while Object Store retains large results and complete
tool output for `[jobs].retention_seconds` after terminal completion. MCP
callers use `wait=false` for an immediate server-generated ticket or
`wait_operation` to follow long work.

`LockManager` remains for direct bootstrap/maintenance paths and for narrow
in-process critical sections that do not enter the public operation queue.

## Error Handling

Oduflow uses a typed error hierarchy for clear error reporting:

| Error | Description |
|---|---|
| `FlowError` | Base error for all operations |
| `BusyError` | A direct maintenance critical section is already held |
| `NotFoundError` | Environment, service, or resource not found |
| `ConflictError` | Resource already exists (e.g. environment already running) |
| `PrerequisiteNotMetError` | System not initialized, Docker not running, or dependency missing |
| `ExternalCommandError` | Git, psql, or Docker command failed (includes command, exit code, output) |
| `ProtectedError` | Environment or extra repo is protected and cannot be deleted |

MCP clients receive errors as `ToolError` with a descriptive message. REST API clients receive JSON with `{"ok": false, "error": "..."}`.

## PostgreSQL Tuning

`resource_plan.py` computes one deterministic host-wide budget from CPU/RAM
detected from Docker (then host stats, with a conservative fallback) plus
`[production].enabled`. The dev PostgreSQL, production PostgreSQL, and
production Odoo renderers consume that plan rather than independently claiming
the host. The dev profile remains deliberately lean for many single-user Odoo
containers:

- In dev-only mode, `shared_buffers` is about 10% of RAM, floored at 128 MB and
  capped at 1 GB; production mode coordinates 5% dev + 20% production targets.
- `work_mem` is derived from the 100-connection ceiling and clamped to 4–16 MB.
- Parallel workers and autovacuum workers scale conservatively with CPU count.
- Planner costs assume SSD storage; statements slower than one second are logged.

Production keeps its separate 200-connection profile, parallelism, and WAL-G
archiving hooks while taking its memory/CPU inputs from the same plan. The plan
also assigns a 45% RAM budget to production Odoo worker sizing. See
[Production Hosting](production.md).

Generated configs carry a planner-version fingerprint. Startup reports stale
managed configs but preserves the `# KEEP` contract; `retune-postgres` is the
explicit preview/apply boundary because several PostgreSQL settings require a
restart and operator-authored configs must never be silently replaced. Applying
the plan also stages regenerated worker settings in existing production Odoo
containers, while leaving every restart under operator control.
