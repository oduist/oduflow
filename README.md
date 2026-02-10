# Oduflow

A git-flow oriented tool for Odoo development built around a **single production database**. Oduflow provisions isolated, ephemeral Odoo environments on Docker — one per git branch — so you can develop and test against a real copy of production data without duplicating hundreds of gigabytes for each branch.

### The problem

Production Odoo databases can grow to tens or hundreds of gigabytes. The filestore (attachments, images, assets) is often even larger. Naively copying the full database and filestore for every feature branch is slow, wastes disk space, and doesn't scale.

### How Oduflow solves it

Oduflow uses a **reference architecture**: one database dump is restored once as a PostgreSQL template, and one filestore directory serves as a shared read-only layer.

- **Reference database** (`odoo_ref`): the production dump is restored into a PostgreSQL template database. Creating a new environment is a `CREATE DATABASE ... TEMPLATE odoo_ref` — an instant, copy-on-write operation at the PostgreSQL level, regardless of database size.
- **Reference filestore** via **fuse-overlayfs**: the production filestore is mounted as a read-only lower layer. Each environment gets a thin upper layer that stores only its own changes. A 50 GB filestore shared across 10 branches still takes ~50 GB on disk, not 500 GB.
- **Shallow git clones**: each branch gets a `--depth 1` clone, so even large repositories are cloned in seconds.

The result: provisioning a new environment from a 30+ GB production database takes seconds, not hours, and disk usage grows only by the delta of actual changes.

## Key features

- **One command to provision** a fully working Odoo instance for any git branch.
- **Instant environment creation** from large production databases via PostgreSQL templates and overlayfs.
- **Minimal disk footprint**: environments share the reference DB and filestore; only per-branch changes consume additional space.
- **Smart pull**: `pull_environment_repository` analyzes changed files (manifest, Python fields, security XML, JS) and automatically decides whether to install, upgrade, restart, or do nothing.
- **AI-agent friendly**: the server exposes tools via [Model Context Protocol (MCP)](https://modelcontextprotocol.io/), so LLM-based coding agents (Cursor, Cline, Amp, etc.) can provision and manage Odoo environments programmatically.
- **Web dashboard**: a built-in HTML dashboard for managing environments from a browser (start / stop / restart / delete / logs / stats).

## Architecture

```
┌──────────────────────────────────────────────────┐
│                   MCP Clients                    │
│         (Cursor, Cline, Amp, Claude, …)          │
└────────────────────┬─────────────────────────────┘
                     │  MCP (Streamable HTTP / stdio)
┌────────────────────▼─────────────────────────────┐
│  server.py — FastMCP transport layer             │
│  • MCP tool definitions                          │
│  • Global mutex for heavy operations             │
│  • Unified error handler (FlowError → ValueError)│
│  • Web UI mount (Starlette)                      │
└────────────────────┬─────────────────────────────┘
                     │
     ┌───────────────┼───────────────┐
     │               │               │
     ▼               ▼               ▼
 system_ops      env_ops         odoo_ops
 (init/destroy)  (create/delete/ (install/upgrade/
                  start/stop/     test/logs)
                  restart/list/
                  pull)
     │               │               │
     └───────────────┼───────────────┘
                     │
              Docker SDK (docker-py)
                     │
     ┌───────────────┼───────────────┐
     ▼               ▼               ▼
  oduflow-net    oduflow-db      oduflow-{branch}-odoo
  (network)      (PostgreSQL)    (Odoo containers)
```

### Key architectural decisions

| Decision | Rationale |
|---|---|
| Single process, single uvicorn worker | Designed for a single developer; no shared-state problems |
| `threading.Lock` mutex | Heavy operations (create/delete env, install modules) reject concurrent requests with `BusyError` instead of queuing |
| Docker SDK only (no subprocess for Docker) | Consistent error handling; `put_archive` replaces `docker cp` |
| fuse-overlayfs for filestore | Copy-on-write sharing of a large reference filestore across all environments |
| Stable port registry (`ports.json`) | Port assignments survive container restarts; eliminates TOCTOU race conditions |
| Typed error hierarchy | `FlowError` base with `NotFoundError`, `BusyError`, `ConflictError`, etc. — clients can distinguish error types |
| Traefik routing mode (optional) | Automatic HTTPS with Let's Encrypt for production-like setups |

## Project structure

```
src/oduflow/
  server.py            # MCP transport: tool definitions, error handler, mutex, CLI
  settings.py          # @dataclass Settings with from_env() and validate()
  errors.py            # FlowError hierarchy
  models.py            # EnvironmentRef dataclass
  naming.py            # Pure functions: slugify, db name, resource name, paths
  git_ops.py           # Git clone, pull, credential management
  git_analysis.py      # Classify changed files → install / upgrade / restart / refresh
  port_registry.py     # Stable port allocation with JSON persistence
  web_ui.py            # Starlette-based dashboard and REST API

  docker_ops/
    client.py           # docker.from_env() wrapper
    system_ops.py       # init_system / destroy_system / reload_template_db
    env_ops.py          # create / delete / start / stop / restart / list / status / pull
    odoo_ops.py         # install / upgrade / test / logs
    stats.py            # Container and system CPU/RAM stats

templates/
  odoo.conf             # Odoo configuration template
  postgresql.conf       # PostgreSQL tuning (shared_buffers, WAL, autovacuum, etc.)
  dashboard.html        # Web dashboard UI

tests/                  # Unit and integration tests (pytest)
```

## MCP Tools

| Tool | Mutex | Description |
|---|:---:|---|
| `setup_repo_auth` | ✓ | Cache git credentials for a private repository |
| `create_environment` | ✓ | Provision an Odoo environment for a branch (clone, DB, container, filestore) |
| `delete_environment` | ✓ | Tear down all resources for a branch |
| `list_environments` | | List all managed environments with status and URLs |
| `get_environment_status` | | Container status, CPU and RAM stats for a branch |
| `start_environment` | | Start a stopped environment |
| `stop_environment` | | Stop a running environment |
| `restart_environment` | | Restart the Odoo container |
| `pull_environment_repository` | ✓ | Git pull + smart analysis → auto install/upgrade/restart |
| `install_odoo_modules` | ✓ | Install Odoo modules (`-i`) |
| `upgrade_odoo_modules` | ✓ | Upgrade Odoo modules (`-u`) |
| `test_environment` | ✓ | Run Odoo tests for specific modules |
| `get_environment_logs` | | Retrieve recent container logs |

## Web Dashboard

When running in HTTP mode, a web dashboard is available at the server root (`http://<host>:<port>/`). It provides:

- Environment list with status indicators
- Start / Stop / Restart / Delete actions
- Live log viewer
- Container and system resource stats (CPU, RAM)

REST API endpoints under `/api/environments/`.

## System requirements

- Docker
- Python 3.10+
- Git
- fuse-overlayfs (for filestore overlay mounting)

Install fuse-overlayfs:

```bash
sudo apt install fuse-overlayfs
```

The `/dev/fuse` device must be available (present by default on Ubuntu).

In `/etc/fuse.conf`, uncomment `user_allow_other` so the Docker daemon (root) can access FUSE mountpoints created by the user:

```bash
sudo sed -i 's/^#user_allow_other/user_allow_other/' /etc/fuse.conf
```

## Installation

```bash
git clone https://github.com/oduist/flow.git
cd flow
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Quick start

### 1. Configure

```bash
cp .env.example .env
# Edit .env — at minimum set paths and optionally ODUFLOW_AUTH_TOKEN
```

### 2. Initialize the system

Create the shared Docker network, PostgreSQL container, and template database:

```bash
oduflow --init --dump-path /path/to/your/odoo_ref.dump
```

Options:
- `--dump-path` — path to the PostgreSQL dump file (overrides `ODUFLOW_DUMP_PATH`)
- `--version` — Odoo version, default `15.0`
- `--force` — recreate the template DB even if it already exists

### 3. Start the MCP server

```bash
oduflow
```

The server starts on `http://0.0.0.0:8000` by default (configurable via `ODUFLOW_HOST` / `ODUFLOW_PORT`).

### 4. Connect an MCP client

Point your MCP client (Cursor, Cline, etc.) to `http://<host>:8000/mcp`.

For stdio transport, set `ODUFLOW_TRANSPORT=stdio` and run `oduflow` as a subprocess.

### Other CLI commands

```bash
# Reload the template DB from a new dump (safe while environments are running)
oduflow --reload-dump --dump-path /path/to/new.dump

# Destroy all shared infrastructure (requires all environments to be deleted first)
oduflow --destroy
```

### Calling MCP tools from the command line

You can invoke any registered MCP tool directly from the terminal using `oduflow call`, without running the server or connecting an MCP client. This is useful for scripting, debugging, and manual operations.

```bash
# List all available tools with their parameters
oduflow call

# Call a tool with positional arguments (mapped to parameters in order)
oduflow call create_environment dev https://github.com/owner/repo.git odoo:17.0
oduflow call delete_environment dev
oduflow call list_environments
oduflow call get_environment_logs main 50

# Call a tool with JSON-encoded arguments
oduflow call create_environment '{"branch_name":"dev","repo_url":"https://github.com/owner/repo.git","odoo_image":"odoo:17.0"}'
```

## Starting from scratch (no production dump)

If you don't have a production database dump — for example, you're starting a new Odoo project or just want to try Oduflow — you can generate a clean reference database automatically.

### Generate a clean reference

```bash
oduflow --init-dump --odoo-image odoo:17.0
```

This will:
1. Start a PostgreSQL container
2. Run a temporary Odoo container that initializes a fresh database with the `base` module
3. Dump the database to `~/.oduflow/odoo_ref.dump`
4. Extract the filestore to `~/.oduflow/odoo_ref_data/`
5. Run `--init` automatically with the generated dump

You can install additional modules during generation:

```bash
oduflow --init-dump --odoo-image odoo:17.0 --modules base,web,contacts,sale
```

After this, `oduflow` is ready — start the server and create environments as usual.

### Editing the reference database

Once you have a reference database (from `--init-dump` or from a production dump), you can modify it interactively — install modules, configure settings, create demo data — and save the result back as the new reference.

**Start the reference editor:**

```bash
oduflow --ref-up --odoo-image odoo:17.0
```

This starts an Odoo container that works **directly** with the template database and filestore (no overlays, no copies). Open the printed URL in your browser, log in, and make any changes you need.

**Save and stop:**

```bash
oduflow --ref-down
```

This stops the container, dumps the updated database to `~/.oduflow/odoo_ref.dump`, and restores the template flag. The filestore is already updated in place since it was mounted directly.

All environments created after this will be based on the updated reference.

### When to use what

| Scenario | Command |
|---|---|
| New project, no existing database | `oduflow --init-dump --odoo-image odoo:17.0` |
| Have a production dump file | `oduflow --init --dump-path /path/to/dump` |
| Need to install modules or configure the reference | `oduflow --ref-up` / `oduflow --ref-down` |
| Update the reference from a newer production dump | `oduflow --reload-dump --dump-path /path/to/new.dump` |

## Configuration

All settings are configured via environment variables. Oduflow uses [python-dotenv](https://pypi.org/project/python-dotenv/) and loads a `.env` file from the working directory on startup. Copy the example and edit it:

```bash
cp .env.example .env
```

### Server

| Variable | Default | Description |
|---|---|---|
| `ODUFLOW_TRANSPORT` | `http` | Transport mode: `http` or `stdio` |
| `ODUFLOW_HOST` | `0.0.0.0` | HTTP server bind address |
| `ODUFLOW_PORT` | `8000` | HTTP server port |
| `ODUFLOW_AUTH_TOKEN` | *(empty)* | Bearer token for MCP HTTP auth and Basic auth password for the web dashboard. Empty = auth disabled |

### Paths

| Variable | Default | Description |
|---|---|---|
| `ODUFLOW_WORKSPACES_DIR` | `~/.oduflow/workspaces` | Root directory for environment workspaces |
| `ODUFLOW_DUMP_PATH` | `~/.oduflow/odoo_ref.dump` | Path to the reference database dump file |
| `ODUFLOW_REF_DATA_PATH` | `~/.oduflow/odoo_ref_data` | Directory with the reference filestore (read-only lower layer for overlay) |
| `ODUFLOW_PORT_REGISTRY` | `~/.oduflow/ports.json` | JSON file for stable port assignments |

### Git

| Variable | Default | Description |
|---|---|---|
| `ODUFLOW_DEFAULT_BRANCH` | `prod` | Base branch to clone from when the requested branch does not exist on the remote |

### Network / Host

| Variable | Default | Description |
|---|---|---|
| `EXTERNAL_HOST` | `localhost` | Hostname or IP used to construct environment URLs |
| `PORT_RANGE_START` | `50000` | Start of the port range for Odoo containers (inclusive) |
| `PORT_RANGE_END` | `50100` | End of the port range (exclusive) |

### Routing

| Variable | Default | Description |
|---|---|---|
| `ODUFLOW_ROUTING_MODE` | `port` | `port` — direct host port mapping; `traefik` — reverse proxy with auto-HTTPS |
| `ODUFLOW_BASE_DOMAIN` | *(empty)* | Base domain for Traefik routing (e.g. `dev.example.com`). Required when `ODUFLOW_ROUTING_MODE=traefik` |
| `ODUFLOW_ACME_EMAIL` | *(empty)* | Let's Encrypt email for TLS certificates. Required when `ODUFLOW_ROUTING_MODE=traefik` |

### Database

| Variable | Default | Description |
|---|---|---|
| `ODOO_DB_USER` | `odoo` | PostgreSQL user for the shared database container |
| `ODOO_DB_PASSWORD` | `odoo` | PostgreSQL password |

## Traefik routing (auto-HTTPS)

By default Oduflow uses **port mode**: each environment gets a dedicated host port (e.g. `http://server:50001`). This is simple and works well for local or single-developer setups.

For production-like access with HTTPS, Oduflow can deploy a **Traefik** reverse proxy that gives every environment its own subdomain with an automatically issued Let's Encrypt certificate.

### Setup

1. **Configure a wildcard DNS record.** Point `*.dev.example.com` to your server's IP address. This is an `A` (or `AAAA`) record at your DNS provider:

   ```
   *.dev.example.com  →  A  →  203.0.113.10
   ```

   Every environment will get a subdomain: `feature-login.dev.example.com`, `fix-invoice.dev.example.com`, etc.

2. **Set the environment variables** in `.env`:

   ```bash
   ODUFLOW_ROUTING_MODE=traefik
   ODUFLOW_BASE_DOMAIN=dev.example.com
   ODUFLOW_ACME_EMAIL=admin@example.com
   ```

3. **Run `oduflow --init`** (or restart the server). Oduflow will create a Traefik container that listens on ports 80 and 443, automatically redirects HTTP to HTTPS, and obtains a separate TLS certificate from Let's Encrypt for each environment subdomain via HTTP-01 challenge.

### How certificates work

Traefik requests a **per-subdomain certificate** from Let's Encrypt each time a new environment is created. This works out of the box with any DNS provider since it uses HTTP-01 validation (Traefik responds to the ACME challenge on port 80).

It is also possible to use a **wildcard certificate** (`*.dev.example.com`) instead of per-subdomain certificates. However, wildcard certificates require **DNS-01 validation**, which means Traefik must be able to create TXT records in your DNS zone via API. This depends on your DNS provider's support and requires additional Traefik configuration (`dnsChallenge` with a provider-specific plugin). The current Oduflow setup does not include this, but it can be added if needed.

### Traefik variables summary

| Variable | Required | Description |
|---|:---:|---|
| `ODUFLOW_ROUTING_MODE` | yes | Set to `traefik` |
| `ODUFLOW_BASE_DOMAIN` | yes | Your base domain (e.g. `dev.example.com`) |
| `ODUFLOW_ACME_EMAIL` | yes | Email for Let's Encrypt registration |

## Environment workspace structure

Each branch gets an isolated workspace:

```
~/.oduflow/workspaces/{branch}/
  repo/                ← shallow git clone
  filestore_upper/     ← overlay upper layer (branch-specific changes)
  filestore_work/      ← overlay work directory
  filestore/           ← merged overlay mount (bound into the container)
  sessions/            ← Odoo session storage
```

## Docker resources

- **Network**: `oduflow-net` — shared bridge network for all containers
- **DB container**: `oduflow-db` — PostgreSQL 15, shared across all environments
- **DB volume**: `oduflow-db-data` — persistent database storage
- **Template DB**: `odoo_ref` — created from the dump file, used as a PostgreSQL template for new environments
- **Traefik** (optional): `oduflow-traefik` container with `oduflow-traefik-acme` volume for TLS certificates

## License

Oduflow is source-available under the [Polyform Noncommercial License 1.0.0](LICENSE). For business use or integrator licenses, visit [oduflow.dev](https://oduflow.dev).
