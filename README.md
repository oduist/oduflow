<p align="center">
  <a href="https://github.com/oduist/oduflow/actions/workflows/tests.yml"><img src="https://github.com/oduist/oduflow/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <img src="https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/Docker-Required-2496ED?logo=docker&logoColor=white" alt="Docker">
  <img src="https://img.shields.io/badge/Protocol-MCP-green" alt="MCP">
  <img src="https://img.shields.io/badge/License-Polyform%20NC-yellow" alt="Polyform Noncommercial License">
  <img src="https://img.shields.io/badge/Odoo-13.0--18.0-714B67?logo=odoo&logoColor=white" alt="Odoo">
</p>

# Oduflow

An **AI-first** Odoo development and CI tool, powered by **reusable database templates**. Oduflow provisions isolated, ephemeral Odoo environments on Docker — one per git branch — and exposes them to AI coding agents via [MCP](https://modelcontextprotocol.io/), creating a **closed feedback loop** that enables fully autonomous Odoo development.

### Beyond Vibe Coding: Spec-Driven Development

**Vibe coding** — chatting with an AI and eyeballing the output — was the first wave. It works for prototypes, but breaks down on real ERP systems where a module must install cleanly, pass tests, and work against production data.

**Spec-Driven Development (SDD)** is the next step: you write a precise specification of *what* the module should do, and the AI agent autonomously implements *how* — because it has a **closed feedback loop** with the running system:

```
┌─────────────────────────────────────────────────────┐
│                    AI Agent                          │
│          (Cursor, Cline, Amp, Claude, …)             │
└──────┬──────────────────────────────▲────────────────┘
       │ 1. Read spec                 │ 5. Read errors,
       │ 2. Write code                │    fix code,
       │ 3. Install module via MCP    │    retry
       │ 4. Click-test UI via         │
       │    Playwright MCP            │
┌──────▼──────────────────────────────┴────────────────┐
│               Oduflow (MCP Server)                    │
│  • install_odoo_modules → traceback or success        │
│  • test_environment → test pass/fail with details     │
│  • get_environment_logs → runtime errors              │
│  • upgrade_odoo_modules → upgrade output              │
├──────────────────────────────────────────────────────┤
│            + Playwright MCP / other tools              │
│  • Navigate Odoo UI, click buttons, fill forms        │
│  • Verify business logic end-to-end                   │
│  • Validate acceptance criteria from the spec         │
└──────────────────────────────────────────────────────┘
```

The agent writes code, installs the module, reads the traceback, fixes the error, retries — and when it installs cleanly, it can open the browser via [Playwright MCP](https://github.com/anthropics/mcp-playwright) to click through the UI, verify business flows, and validate acceptance criteria — **all without human intervention**.

| | Vibe Coding | Spec-Driven Development |
|---|---|---|
| **Input** | Conversational prompts | Formal specification with acceptance criteria |
| **Feedback** | Human eyeballs the code | System returns errors, test results, and UI state automatically |
| **Iteration** | Human copy-pastes errors back | Agent retries autonomously via MCP |
| **Scope** | Single files, prototypes | Full modules against real databases |
| **Verification** | "Looks right" | Module installs, tests pass, UI works on production data |

Create templates from production dumps, staging snapshots, or from scratch. Maintain **multiple named templates** side-by-side (e.g. per Odoo version, per client, per project phase) and spin up any combination of branch + database in seconds.

### Why not just use odoo.sh?

Odoo.sh is a great hosting platform, but its development workflow has real limitations:

| | odoo.sh | Oduflow |
|---|---|---|
| **Branch model** | Dev branches must be created manually in the UI; limited count on paid plans | Follows **GitFlow** — push a branch, get an environment automatically |
| **Database on dev branches** | Dev branches start **empty** (no data) | Every branch starts from a **real database template** — production dump, staging snapshot, or custom |
| **Staging** | Paid staging branches (copy of production); limited slots | Unlimited environments, each from **any template** — any combination of branch × database |
| **Provisioning speed** | Minutes to create a staging copy of a large DB | **Seconds**, regardless of DB size (PostgreSQL `CREATE DATABASE ... TEMPLATE` + overlayfs) |
| **Disk usage** | Full copy per staging branch | **Copy-on-write** — 10 branches sharing a 50 GB DB + filestore ≈ 50 GB total |
| **CI / testing** | Basic CI on push | **Smart pull** analyzes changed files and auto-installs/upgrades/restarts only what's needed |
| **AI integration** | None | **MCP-native** — AI coding agents (Cursor, Cline, Amp) can provision and manage environments programmatically |
| **Infrastructure** | Odoo SA cloud only | **Your machine** — local, VPS, or CI runner via Docker |

Oduflow is not a replacement for production hosting — it's the **developer workbench and CI layer** that platforms like odoo.sh don't provide.

---

## Table of Contents

- [The Problem](#the-problem)
- [How Oduflow Solves It](#how-oduflow-solves-it)
- [Key Features](#key-features)
- [Architecture](#architecture)
- [Project Structure](#project-structure)
- [System Requirements](#system-requirements)
- [Installation](#installation)
- [Quick Start](#quick-start)
- [Configuration Reference](#configuration-reference)
- [Template Management](#template-management)
- [Environment Management](#environment-management)
- [Smart Pull — Intelligent Change Detection](#smart-pull--intelligent-change-detection)
- [Auxiliary Services](#auxiliary-services)
- [Extra Addons Repositories](#extra-addons-repositories)
- [Executing Commands Inside Environments](#executing-commands-inside-environments)
- [Environment Protection](#environment-protection)
- [Web Dashboard & REST API](#web-dashboard--rest-api)
- [MCP Tools Reference](#mcp-tools-reference)
- [CLI Reference](#cli-reference)
- [Traefik Routing (Auto-HTTPS)](#traefik-routing-auto-https)
- [Multi-Instance Support](#multi-instance-support)
- [Authentication & Security](#authentication--security)
- [Use Cases & Workflows](#use-cases--workflows)
- [Environment Workspace Structure](#environment-workspace-structure)
- [Docker Resources](#docker-resources)
- [Error Handling](#error-handling)
- [License](#license)

---

## The Problem

Production Odoo databases can grow to tens or hundreds of gigabytes. The filestore (attachments, images, assets) is often even larger. Naively copying the full database and filestore for every feature branch is slow, wastes disk space, and doesn't scale.

## How Oduflow Solves It

Oduflow uses a **template architecture**: one database dump is restored once as a PostgreSQL template, and one filestore directory serves as a shared read-only layer.

- **Template database** (`odoo_ref_default`): the production dump is restored into a PostgreSQL template database. Creating a new environment is a `CREATE DATABASE ... TEMPLATE odoo_ref_default` — an instant, copy-on-write operation at the PostgreSQL level, regardless of database size. Multiple named templates are supported (e.g. `odoo_ref_myproject`, `odoo_ref_v17`).
- **Template filestore** via **fuse-overlayfs**: the production filestore is mounted as a read-only lower layer. Each environment gets a thin upper layer that stores only its own changes. A 50 GB filestore shared across 10 branches still takes ~50 GB on disk, not 500 GB.
- **Shallow git clones**: each branch gets a `--depth 1` clone, so even large repositories are cloned in seconds.

The result: provisioning a new environment from a 30+ GB production database takes **seconds, not hours**, and disk usage grows only by the delta of actual changes.

---

## Key Features

### Core
- **One command to provision** a fully working Odoo instance for any git branch
- **Instant environment creation** from large production databases via PostgreSQL templates and overlayfs
- **Minimal disk footprint** — environments share the template DB and filestore; only per-branch changes consume additional space
- **Template-free mode** — create environments from scratch (`template_name="none"`) when no production dump is available
- **Auto branch creation** — if a branch doesn't exist on the remote, Oduflow clones the default branch and creates the new branch automatically
- **Extra addons repositories** — mount shared addon repos (e.g. Odoo Enterprise) into environments via git worktrees; `addons_path` is auto-merged into `odoo.conf`
- **Environment protection** — protect environments from accidental deletion via a toggle in the dashboard or REST API

### Smart Automation
- **Smart pull** — `pull_environment_repository` analyzes changed files (manifest, Python fields, security XML, JS) and automatically decides whether to install, upgrade, restart, or do nothing
- **Auto-install dependencies** — `requirements.txt` (pip) and `apt_packages.txt` (apt) in the repository root are automatically installed when creating an environment
- **Custom odoo.conf** — if the repository contains an `odoo.conf` at its root, it is used instead of the default template
- **Field change detection** — Python files are analyzed for `fields.*` definition changes, triggering module upgrades only when necessary

### Infrastructure
- **Auxiliary services** — managed sidecar containers for Redis, Meilisearch, Elasticsearch, or any other service your Odoo setup needs
- **Traefik auto-HTTPS** — optional reverse proxy with Let's Encrypt certificates for production-like access
- **Stable port registry** — port assignments are persisted in `ports.json` and survive container restarts
- **Resource monitoring** — per-container CPU and RAM stats, plus system-level metrics (memory, load average)

### Integration
- **AI-agent friendly** — the server exposes tools via [Model Context Protocol (MCP)](https://modelcontextprotocol.io/), so LLM-based coding agents (Cursor, Cline, Amp, etc.) can provision and manage Odoo environments programmatically
- **Web dashboard** — a built-in HTML dashboard for managing environments from a browser
- **REST API** — full JSON API for programmatic control from any HTTP client
- **CLI tools** — every MCP tool can be called directly from the command line via `oduflow call`
- **Dual transport** — supports both HTTP (Streamable HTTP) and stdio MCP transports

---

## Architecture

```
┌──────────────────────────────────────────────────┐
│                   MCP Clients                    │
│         (Cursor, Cline, Amp, Claude, …)          │
└────────────────────┬─────────────────────────────┘
                     │  MCP (Streamable HTTP / stdio)
┌────────────────────▼─────────────────────────────┐
│  server.py — FastMCP transport layer             │
│  • MCP tool definitions (30 tools)               │
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

---

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
                        # template_up / template_down / promote_env / drop_template / list_templates
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
    agents_guide.md       # AI agent instructions (copied to $ODUFLOW_HOME on init-instance)

tests/                  # Unit and integration tests (pytest)
```

---

## System Requirements

- **Docker** (Docker Engine or Docker Desktop)
- **Python 3.10+**
- **Git**
- **fuse-overlayfs** (for filestore overlay mounting)

### Install fuse-overlayfs

```bash
sudo apt install fuse-overlayfs
```

The `/dev/fuse` device must be available (present by default on Ubuntu).

In `/etc/fuse.conf`, uncomment `user_allow_other` so the Docker daemon (root) can access FUSE mountpoints created by the user:

```bash
sudo sed -i 's/^#user_allow_other/user_allow_other/' /etc/fuse.conf
```

---

## Installation

```bash
pip install oduflow
```

For local development:

```bash
git clone https://github.com/oduist/oduflow.git
cd oduflow
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

---

## Quick Start

### 1. Configure

```bash
cp .env.example .env
# Edit .env — at minimum set paths and optionally ODUFLOW_AUTH_TOKEN
```

### 2. Initialize the system

Create the shared Docker network, PostgreSQL container, and Traefik reverse proxy:

```bash
oduflow init
```

To set up a template database, use `oduflow init-template` (see below).

### 3. Start the MCP server

```bash
oduflow
```

The server starts on `http://0.0.0.0:8000` by default (configurable via `ODUFLOW_HOST` / `ODUFLOW_PORT`).

### 4. Connect an MCP client

Point your MCP client (Cursor, Cline, Amp, etc.) to `http://<host>:8000/mcp`.

For stdio transport, set `ODUFLOW_TRANSPORT=stdio` and run `oduflow` as a subprocess.

---

## Configuration Reference

All settings are configured via environment variables. Oduflow uses [python-dotenv](https://pypi.org/project/python-dotenv/) and loads a `.env` file from the working directory on startup.

```bash
cp .env.example .env
```

### Server

| Variable | Default | Description |
|---|---|---|
| `ODUFLOW_TRANSPORT` | `http` | Transport mode: `http` or `stdio` |
| `ODUFLOW_HOST` | `0.0.0.0` | HTTP server bind address |
| `ODUFLOW_PORT` | `8000` | HTTP server port |
| `ODUFLOW_AUTH_TOKEN` | *(empty)* | Bearer token for MCP HTTP auth. Empty = MCP auth disabled |
| `ODUFLOW_UI_PASSWORD` | *(empty)* | Password for Web UI Basic auth (user: `admin`). Separate from MCP auth token. Empty = UI auth disabled |

### Paths

| Variable | Default | Description |
|---|---|---|
| `ODUFLOW_INSTANCE_ID` | `1` | Instance identifier (1-9). Allows running multiple independent Oduflow instances. See [MULTI_INSTANCE.md](MULTI_INSTANCE.md) |
| `ODUFLOW_HOME` | `/srv/oduflow_data_{INSTANCE_ID}` | Base directory for all data (dumps, workspaces, ports) |
| `ODUFLOW_WORKSPACES_DIR` | `$ODUFLOW_HOME/workspaces` | Root directory for environment workspaces |
| `ODUFLOW_PORT_REGISTRY` | `$ODUFLOW_HOME/ports.json` | JSON file for stable port assignments |

Template folder structure: `$ODUFLOW_HOME/templates/<name>/dump.sql` (or `dump.pgdump`) and `$ODUFLOW_HOME/templates/<name>/filestore/`.

### Git

| Variable | Default | Description |
|---|---|---|
| `ODUFLOW_DEFAULT_BRANCH` | `prod` | Base branch to clone from when the requested branch does not exist on the remote |
| `ODUFLOW_DEFAULT_TEMPLATE` | `default` | Default template profile for new environments |

### Network / Host

| Variable | Default | Description |
|---|---|---|
| `EXTERNAL_HOST` | `localhost` | Hostname or IP used to construct environment URLs |
| `PORT_RANGE_START` | `50000` | Start of the port range for Odoo containers (inclusive) |
| `PORT_RANGE_END` | `50100` | End of the port range (exclusive) — supports up to 100 concurrent environments |

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

---

## Template Management

Templates are the foundation of Oduflow's instant environment creation. A template consists of a PostgreSQL dump file and an optional filestore directory.

### Starting from Scratch (No Production Dump)

If you don't have a production database dump — for example, you're starting a new Odoo project or just want to try Oduflow — you can generate a clean template automatically.

#### Generate a clean template

```bash
oduflow init-template --odoo-image odoo:17.0
```

If a `dump.sql` or filestore already exists, the command will refuse to run. Use `--force` to overwrite:

```bash
oduflow init-template --odoo-image odoo:17.0 --force
```

This will:
1. Start a PostgreSQL container (if not already running)
2. Run a temporary Odoo container that initializes a fresh database with the `base` module
3. Dump the database to `$ODUFLOW_HOME/templates/default/dump.pgdump`
4. Extract the filestore to `$ODUFLOW_HOME/templates/default/filestore/`
5. Load the dump into the template database automatically

#### Install additional modules during generation

```bash
oduflow init-template --odoo-image odoo:17.0 --modules base,web,contacts,sale
```

#### Named templates for different projects

```bash
oduflow init-template --odoo-image odoo:17.0 --template-name myproject-v17
oduflow init-template --odoo-image odoo:15.0 --template-name legacy-v15
```

### From a Production Dump

Place your dump file at `$ODUFLOW_HOME/templates/default/dump.sql` (plain SQL) or `dump.pgdump` (PostgreSQL custom format) and optionally copy the filestore:

```bash
mkdir -p /srv/oduflow_data/templates/default/
cp /path/to/production.sql /srv/oduflow_data/templates/default/dump.sql
cp -r /path/to/filestore/ /srv/oduflow_data/templates/default/filestore/
oduflow init
```

### Editing the Template Database

Once you have a template, you can modify it interactively — install modules, configure settings, create demo data — and save the result back as the new template.

**Start the template editor:**

```bash
oduflow template-up --odoo-image odoo:17.0
```

This starts an Odoo container that works **directly** with the template database and filestore (no overlays, no copies). Open the printed URL in your browser, log in, and make any changes you need.

**Save and stop:**

```bash
oduflow template-down
```

This stops the container, dumps the updated database, and restores the PostgreSQL template flag. The filestore is already updated in place since it was mounted directly.

All environments created after this will be based on the updated template.

### Promoting a Branch to Template

When you've made significant changes in a branch environment (installed modules, created configurations), you can promote it to become the new template:

```bash
oduflow promote my-branch
oduflow promote my-branch --template-name myproject  # promote to a named template
```

This operation:
1. Dumps the branch database to a new template dump file
2. Reloads the template database from the new dump
3. Snapshots the branch's merged filestore
4. Unmounts all overlay filesystems across active environments
5. Replaces the template filestore with the snapshot
6. Remounts overlays for all active environments (clearing their upper dirs)
7. Restarts all active containers

> ⚠️ **Destructive operation**: All other environments lose their filestore deltas and are reset to the new baseline.

### Reloading a Template

Update the template from a newer production dump without touching the filestore:

```bash
oduflow reload-template --dump-path /path/to/new.dump
oduflow reload-template --template-name myproject --dump-path /path/to/new.dump
```

### Listing and Dropping Templates

```bash
# List all template profiles with their status
oduflow list-templates

# Drop a named template (removes DB + files from disk)
oduflow drop-template myproject
```

### Template Decision Matrix

| Scenario | Command |
|---|---|
| New project, no existing database | `oduflow init-template --odoo-image odoo:17.0` |
| Regenerate template from scratch | `oduflow init-template --odoo-image odoo:17.0 --force` |
| Named template for a specific project | `oduflow init-template --odoo-image odoo:17.0 --template-name myproject` |
| Have a production dump file | Place dump at `$ODUFLOW_HOME/templates/default/dump.sql` and run `oduflow init` |
| Need to install modules or configure the template | `oduflow template-up --odoo-image odoo:17.0` / `oduflow template-down` |
| Update the template from a newer production dump | `oduflow reload-template --dump-path /path/to/new.dump` |
| Promote a branch environment to template | `oduflow promote my-branch` |
| List all templates | `oduflow list-templates` |
| Drop a named template | `oduflow drop-template myproject` |

---

## Environment Management

### Creating Environments

```bash
# Create from existing branch with default template
oduflow call create_environment feature-login https://github.com/owner/repo.git odoo:17.0

# Create with a named template
oduflow call create_environment feature-login https://github.com/owner/repo.git odoo:17.0 myproject

# Create without a template (fresh Odoo with -i base)
oduflow call create_environment feature-login https://github.com/owner/repo.git odoo:17.0 none
```

When creating an environment, Oduflow:
1. **Clones the repository** — shallow clone (`--depth 1`) for speed
2. **Creates the database** — `CREATE DATABASE ... TEMPLATE odoo_ref_default` for instant copy, or empty DB when `template=none`
3. **Mounts the filestore overlay** — fuse-overlayfs with the template as lower layer
4. **Detects UID/GID** — runs `id` in the Odoo image to set correct file ownership
5. **Installs dependencies** — auto-installs from `apt_packages.txt` and `requirements.txt` if present in the repo
6. **Configures Odoo** — uses repo's `odoo.conf` if available, otherwise the default template
7. **Starts the container** — with `--dev=xml` for hot-reloading XML/QWeb changes
8. **Initializes base** — when `template=none`, runs `odoo -i base --stop-after-init`

#### Auto branch creation

If the requested branch doesn't exist on the remote, Oduflow automatically:
1. Clones the default branch (configured via `ODUFLOW_DEFAULT_BRANCH`)
2. Creates a new local branch with the requested name
3. Reports the branch was created from the default branch

#### Private repository authentication

For private repos, configure credentials first:

```bash
oduflow call setup_repo_auth https://user:PAT@github.com/owner/private-repo.git
```

Credentials are stored in the git credential store. Subsequent `create_environment` calls can use the clean URL without credentials.

#### Auto-dependency installation

Place these files in your repository root for automatic installation during environment creation:

**`requirements.txt`** — Python packages installed via pip:
```
phonenumbers==8.13.0
python-barcode==0.15.1
xlsxwriter>=3.0
```

**`apt_packages.txt`** — System packages installed via apt:
```
# Dependencies for wkhtmltopdf
libfontconfig1
libxrender1
xfonts-75dpi
```

### Lifecycle Management

```bash
# List all environments with status, URL, image, and repo info
oduflow call list_environments

# Check detailed status with CPU/RAM stats
oduflow call get_environment_status feature-login

# Stop an environment (preserves data)
oduflow call stop_environment feature-login

# Start a stopped environment
oduflow call start_environment feature-login

# Restart the Odoo container
oduflow call restart_environment feature-login

# Rebuild the container from scratch (keeps database and filestore)
oduflow call rebuild_environment feature-login

# Tear down everything (container, database, filestore, workspace)
oduflow call delete_environment feature-login
```

### Viewing Logs

```bash
# Last 100 lines (default)
oduflow call get_environment_logs feature-login

# Last 500 lines
oduflow call get_environment_logs feature-login 500
```

### Installing and Upgrading Modules

```bash
# Install modules (odoo -i)
oduflow call install_odoo_modules feature-login sale,crm,website

# Upgrade modules (odoo -u)
oduflow call upgrade_odoo_modules feature-login sale,crm
```

### Running Tests

```bash
oduflow call test_environment feature-login sale,crm
```

This runs `odoo --test-enable --stop-after-init -i <modules>` inside the container.

---

## Smart Pull — Intelligent Change Detection

The `pull_environment_repository` tool is one of Oduflow's most powerful features. It pulls the latest changes from the remote repository and **automatically determines the minimal action required**:

```bash
oduflow call pull_environment_repository feature-login
```

### How it works

After `git pull --rebase`, Oduflow compares `HEAD` before and after, then classifies every changed file:

| Changed File | Analysis | Action |
|---|---|---|
| `__manifest__.py` (new module) | No previous manifest exists | **Install** the module |
| `__manifest__.py` (version changed) | `version` key differs | **Upgrade** the module |
| `__manifest__.py` (data/assets/demo/qweb changed) | File lists in manifest changed | **Upgrade** the module |
| `*.py` with `fields.*` changes | Field definitions added/removed/modified | **Upgrade** the module |
| `*.py` (no field changes) | Business logic change | **Restart** the container |
| `security/*.xml` | Access control or record rules | **Upgrade** the module |
| `*.xml` (not in security/) | Views, actions, data | **Refresh** (hot-reloaded via `--dev=xml`) |
| `*.js` | Frontend assets | **Refresh** (hot-reloaded via `--dev=xml`) |

### Action priority

`install` > `upgrade` > `restart` > `refresh`

If any module needs installation, all pending upgrades are also executed. If only Python files changed (without field modifications), a container restart is sufficient. If only XML/JS changed, no server-side action is needed — just refresh the browser.

### Module detection

Oduflow walks up from each changed file to find the nearest `__manifest__.py`, correctly identifying which Odoo module a file belongs to, even in nested directory structures.

---

## Auxiliary Services

Oduflow can manage sidecar containers for auxiliary services your Odoo instance depends on — Redis, Meilisearch, Elasticsearch, RabbitMQ, or any other Docker-based service.

### Creating a Service

```bash
# Redis
oduflow call create_service redis redis:7 6379

# Meilisearch with environment variables
oduflow call create_service meilisearch getmeili/meilisearch:v1.6 7700 "" "MEILI_MASTER_KEY=abc123,MEILI_ENV=production"

# Elasticsearch
oduflow call create_service elasticsearch docker.elastic.co/elasticsearch/elasticsearch:8.11.0 9200 "" "discovery.type=single-node,ES_JAVA_OPTS=-Xms512m -Xmx512m"
```

Services are:
- Attached to the shared `oduflow-net` network (accessible by all Odoo containers)
- Given an `unless-stopped` restart policy
- Automatically routed through Traefik with HTTPS when in traefik mode
- Labeled for management (`oduflow.managed=true`, `oduflow.service=<name>`)

### Managing Services

```bash
# List all services with status, ports, URLs, and env vars
oduflow call list_services

# View service logs
oduflow call get_service_logs redis 200

# Update a service (pull latest image, recreate container with same settings)
oduflow call update_service meilisearch

# Delete a service
oduflow call delete_service redis
```

### Service Update Flow

The `update_service` operation:
1. Captures the current image name, environment variables, port, and hostname
2. Pulls the latest version of the image
3. Compares image digests — if unchanged, reports "already up-to-date"
4. If the image changed: stops the old container, removes it, and creates a new one with identical settings

### Service Presets

Every time a service is created, its configuration (image, port, hostname, environment variables) is automatically saved as a **preset** in `$ODUFLOW_HOME/service_presets.json`. This allows you to restore a service after deletion without re-entering its configuration.

```bash
# List saved presets
oduflow call list_service_presets

# Restore a previously deleted service
oduflow call restore_service redis

# Remove a saved preset
oduflow call delete_service_preset redis
```

---

## Extra Addons Repositories

Oduflow supports mounting **extra addon repositories** (e.g. Odoo Enterprise, third-party themes) into environments. Extra repos are cloned once at the instance level and shared across environments via git worktrees.

### Architecture

```
$ODUFLOW_HOME/
  shared_repos/
    enterprise/          ← bare git clone (shared)
    custom-themes/       ← bare git clone (shared)
  workspaces/
    feature-x/
      repo/              ← main project repo (existing)
      extra/
        enterprise/      ← git worktree (branch 17.0)
        custom-themes/   ← git worktree (branch main)
```

### Setting Up Extra Repos

Clone an extra repository once (it will be available for all environments):

```bash
# Via CLI
oduflow call add_extra_repo enterprise https://github.com/odoo/enterprise.git

# Private repos — configure auth first
oduflow call setup_repo_auth https://user:PAT@github.com/odoo/enterprise.git
oduflow call add_extra_repo enterprise https://github.com/odoo/enterprise.git
```

### Using Extra Addons in Environments

When creating an environment, specify which extra repos to mount:

```bash
# Mount enterprise addons on branch 17.0
oduflow call create_environment feature-x https://github.com/company/addons.git odoo:17.0 default enterprise 17.0

# Mount multiple extra repos
oduflow call create_environment feature-x https://github.com/company/addons.git odoo:17.0 default "enterprise,custom-themes" 17.0
```

Oduflow automatically:
1. Creates a git worktree for each extra repo at the specified branch
2. Mounts the worktree **read-only** into the container as `/mnt/extra-addons-{name}`
3. Generates a merged `odoo.conf` with all extra paths added to `addons_path`

### Managing Extra Repos

```bash
# List all cloned extra repos with available branches
oduflow call list_extra_repos

# Delete an extra repo (fails if any environment references it)
oduflow call delete_extra_repo enterprise
```

Extra repos can also be managed from the **Web Dashboard** under the "Extra Addons" tab.

> **Note:** Extra addons are mounted read-only and are NOT updated by `pull_environment_repository`. To update extra addons, delete and recreate the environment.

---

## Executing Commands Inside Environments

Run arbitrary shell commands inside the Odoo container:

```bash
# List addon files
oduflow call exec_in_environment feature-login "ls /mnt/extra-addons"

# Check Python version
oduflow call exec_in_environment feature-login "python3 --version"

# Run a Python script
oduflow call exec_in_environment feature-login "python3 -c 'import odoo; print(odoo.release.version)'"

# Install a package as root
oduflow call exec_in_environment feature-login "pip3 install phonenumbers" root

# Debug database
oduflow call exec_in_environment feature-login "psql -h oduflow-db -U odoo -d oduflow_feature-login -c 'SELECT count(*) FROM res_partner;'"
```

The `user` parameter defaults to `odoo`. Use `root` for privileged operations (installing packages, modifying system files).

---

## Environment Protection

Environments can be **protected** from accidental deletion. A protected environment cannot be deleted until protection is removed.

Protection state is stored as a `.protected` marker file in the environment's workspace directory, so it survives container rebuilds and restarts.

### Via REST API

```bash
# Protect an environment
curl -X POST http://localhost:8000/api/environments/feature-login/protect

# Unprotect an environment
curl -X POST http://localhost:8000/api/environments/feature-login/unprotect
```

### Via Web Dashboard

Click the **🔓 Protect** button on any environment card to toggle protection. When protected:
- The button shows **🔒 Protected** (highlighted)
- The **Delete** button is disabled
- Attempting to delete via MCP or API returns a `ProtectedError`

---

## Web Dashboard & REST API

### Web Dashboard

When running in HTTP mode, a web dashboard is available at the server root (`http://<host>:<port>/`). It provides:

- **Environment list** with status indicators (running / stopped / partial)
- **Environment actions**: Start / Stop / Restart / Protect / Delete
- **Environment creation** form (branch, repo URL, Odoo image, template, extra addons)
- **Environment protection** — toggle to prevent accidental deletion
- **Live log viewer** for each environment
- **Container and system resource stats** (CPU, RAM, load average)
- **Service management** — create, update, delete, and view logs for auxiliary services
- **Extra addons management** — clone, list, and delete extra addon repositories
- **Template listing** — view available template profiles with their status
- **License management** — view current license and activate license keys

### REST API Endpoints

All endpoints return JSON with an `ok` field. Authentication via HTTP Basic auth when `ODUFLOW_AUTH_TOKEN` is set (user: `admin`, password: the token value).

#### Environments

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/environments` | List all environments |
| `POST` | `/api/environments/create` | Create a new environment (JSON body: `branch_name`, `repo_url`, `odoo_image`, `template_name`) |
| `POST` | `/api/environments/{branch}/start` | Start an environment |
| `POST` | `/api/environments/{branch}/stop` | Stop an environment |
| `POST` | `/api/environments/{branch}/restart` | Restart an environment |
| `POST` | `/api/environments/{branch}/delete` | Delete an environment |
| `GET` | `/api/environments/{branch}/logs?n=200` | Get environment logs |
| `POST` | `/api/environments/{branch}/protect` | Protect environment from deletion |
| `POST` | `/api/environments/{branch}/unprotect` | Remove protection from environment |

#### Services

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/services` | List all managed services |
| `POST` | `/api/services/create` | Create a service (JSON body: `name`, `image`, `port`, `hostname`, `env_vars`) |
| `POST` | `/api/services/{name}/update` | Update (pull latest image & recreate) |
| `POST` | `/api/services/{name}/delete` | Delete a service |
| `GET` | `/api/services/{name}/logs?n=200` | Get service logs |

#### Service Presets

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/service-presets` | List saved service presets |
| `POST` | `/api/service-presets/{name}/restore` | Restore a service from a saved preset |
| `POST` | `/api/service-presets/{name}/delete` | Delete a saved service preset |

#### System

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/stats` | Container CPU/RAM stats + system metrics |
| `GET` | `/api/templates` | List available template profiles |

#### Extra Addons

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/extra-repos` | List all cloned extra addons repositories |
| `POST` | `/api/extra-repos/add` | Clone an extra addons repo (JSON body: `name`, `repo_url`) |
| `POST` | `/api/extra-repos/{name}/delete` | Delete a cloned extra addons repository |

#### Licensing

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/license` | Get current license information |
| `POST` | `/api/license/activate` | Activate a license key (JSON body: `key`) |

#### Agent Guide

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/agents-guide` | Get AI agent instructions (markdown) |

---

## MCP Tools Reference

All tools are accessible via MCP clients (Cursor, Cline, Amp, etc.) and the CLI (`oduflow call`). A subset is also available via the [REST API](#rest-api-endpoints).

| Tool | Mutex | Description |
|---|:---:|---|
| **Environment Management** | | |
| `create_environment` | ✓ | Provision an Odoo environment for a branch (clone, DB, container, filestore) |
| `delete_environment` | ✓ | Tear down all resources for a branch |
| `list_environments` | | List all managed environments with status and URLs |
| `get_environment_status` | | Container status, CPU and RAM stats for a branch |
| `start_environment` | | Start a stopped environment |
| `stop_environment` | | Stop a running environment |
| `restart_environment` | | Restart the Odoo container |
| `rebuild_environment` | ✓ | Re-create the container from the same image, preserving DB and filestore |
| **Odoo Operations** | | |
| `pull_environment_repository` | ✓ | Git pull + smart analysis → auto install/upgrade/restart |
| `install_odoo_modules` | ✓ | Install Odoo modules (`-i`) |
| `upgrade_odoo_modules` | ✓ | Upgrade Odoo modules (`-u`) |
| `test_environment` | ✓ | Run Odoo tests for specific modules |
| `get_environment_logs` | | Retrieve recent container logs |
| `exec_in_environment` | ✓ | Execute an arbitrary shell command inside the Odoo container |
| **Template Management** | | |
| `promote_environment` | ✓ | ⚠️ Promote a branch DB + filestore to become a new template |
| `list_templates` | | List available template profiles |
| `drop_template` | ✓ | ⚠️ Drop a template profile (DB + files) |
| **Auxiliary Services** | | |
| `create_service` | ✓ | Create a managed service container (e.g. Redis, Meilisearch) |
| `delete_service` | ✓ | Stop and remove a service container |
| `update_service` | ✓ | Pull latest image and recreate the service |
| `list_services` | | List all managed service containers |
| `get_service_logs` | | Retrieve service container logs |
| **Service Presets** | | |
| `list_service_presets` | | List saved service presets (configurations that can be restored) |
| `restore_service` | ✓ | Restore a service from a saved preset |
| `delete_service_preset` | | Remove a saved service preset |
| **Repository Auth** | | |
| `setup_repo_auth` | ✓ | Cache git credentials for a private repository |
| **Extra Addons** | | |
| `add_extra_repo` | ✓ | Clone an extra addons repository (e.g. Odoo Enterprise) for use with environments |
| `list_extra_repos` | | List all cloned extra addons repositories |
| `delete_extra_repo` | ✓ | Delete a cloned extra addons repository |
| **Agent Guide** | | |
| `get_agents_guide` | | Get AI agent instructions for using Oduflow MCP tools |

> **Mutex** (✓): these tools acquire a global lock. If another mutexed operation is in progress, the call is rejected with `BusyError` ("Another operation is in progress. Try again later.").

---

## CLI Reference

### Server & System Commands

```bash
# Start the MCP server (default command)
oduflow

# Initialize shared infrastructure (network, DB, Traefik)
oduflow init

# Initialize per-instance directories (workspaces, templates)
oduflow init-instance

# Destroy all shared infrastructure (requires no active environments)
oduflow destroy
```

### Template Commands

```bash
# Generate a clean template from a Docker image
oduflow init-template --odoo-image odoo:17.0 [--modules base,web,sale] [--template-name myproject] [--force]

# Start interactive template editor
oduflow template-up --odoo-image odoo:17.0 [--template-name myproject]

# Stop template editor and save changes
oduflow template-down [--template-name myproject]

# Reload template DB from a dump file
oduflow reload-template [--template-name default] [--dump-path /path/to/new.dump]

# Promote a branch to become the new template
oduflow promote <branch> [--template-name default]

# List all template profiles
oduflow list-templates

# Drop a template profile
oduflow drop-template <template_name>
```

### Service Commands

```bash
# List all managed services
oduflow list-services
```

### Tool Introspection

```bash
# List all registered MCP tools with parameters
oduflow list [--verbose]
```

### Direct Tool Invocation

You can invoke any registered MCP tool directly from the terminal using `oduflow call`, without running the server or connecting an MCP client. This is useful for scripting, debugging, and manual operations.

```bash
# List all available tools with their parameters
oduflow call

# Call a tool with positional arguments (mapped to parameters in order)
oduflow call create_environment dev https://github.com/owner/repo.git odoo:17.0
oduflow call delete_environment dev
oduflow call list_environments
oduflow call get_environment_logs main 50
oduflow call exec_in_environment dev "ls /mnt/extra-addons"
oduflow call create_service redis redis:7 6379

# Call a tool with JSON-encoded arguments
oduflow call create_environment '{"branch_name":"dev","repo_url":"https://github.com/owner/repo.git","odoo_image":"odoo:17.0","template_name":"myproject"}'

# Type coercion is automatic: int, bool, and float parameters are cast from strings
oduflow call get_environment_logs dev 500
```

---

## Traefik Routing (Auto-HTTPS)

By default Oduflow uses **port mode**: each environment gets a dedicated host port (e.g. `http://server:50001`). This is simple and works well for local or single-developer setups.

For production-like access with HTTPS, Oduflow can deploy a **Traefik** reverse proxy that gives every environment its own subdomain with an automatically issued Let's Encrypt certificate.

### Setup

1. **Configure a wildcard DNS record.** Point `*.dev.example.com` to your server's IP address:

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

3. **Run `oduflow init`** (or restart the server). Oduflow will create a Traefik v3.6 container that:
   - Listens on ports 80 and 443
   - Automatically redirects HTTP to HTTPS
   - Obtains a separate TLS certificate from Let's Encrypt for each environment subdomain via HTTP-01 challenge
   - Routes requests to the correct Odoo container based on the subdomain
   - Also routes the Oduflow server itself via `ODUFLOW_BASE_DOMAIN`

### How certificates work

Traefik requests a **per-subdomain certificate** from Let's Encrypt each time a new environment is created. This works out of the box with any DNS provider since it uses HTTP-01 validation (Traefik responds to the ACME challenge on port 80).

Wildcard certificates (`*.dev.example.com`) via DNS-01 validation are also possible but require additional Traefik configuration with a provider-specific plugin.

### Service routing with Traefik

Auxiliary services also get Traefik routing. A service named `meilisearch` with base domain `dev.example.com` becomes accessible at `https://meilisearch.dev.example.com`. Custom hostnames are also supported.

---

## Multi-Instance Support

Oduflow supports running **multiple independent instances** on a single machine. Each instance has its own environments, templates, services, and port registry, while sharing the Docker network and PostgreSQL container.

Set `ODUFLOW_INSTANCE_ID` (1-9) and optionally `ODUFLOW_HOME` to isolate instances:

```bash
# Instance 1
export ODUFLOW_INSTANCE_ID=1
oduflow init-instance

# Instance 2 (separate terminal / process)
export ODUFLOW_INSTANCE_ID=2
export ODUFLOW_PORT=8001
oduflow init-instance
```

Databases are namespaced by instance ID (e.g. `oduflow_1_main`, `oduflow_2_main`), and containers are labeled with `oduflow.instance={ID}` for filtering.

For the full guide — lifecycle, naming conventions, shared vs. per-instance resources — see **[MULTI_INSTANCE.md](MULTI_INSTANCE.md)**.

---

## Authentication & Security

### MCP HTTP Auth

When `ODUFLOW_AUTH_TOKEN` is set, the MCP endpoint (`/mcp`) requires a Bearer token:

```
Authorization: Bearer <your-token>
```

This is implemented via FastMCP's `StaticTokenVerifier`.

### Web Dashboard Auth

The web dashboard and REST API use HTTP Basic authentication with a **separate** password:

- **Username**: `admin`
- **Password**: value of `ODUFLOW_UI_PASSWORD`

This is independent from the MCP Bearer token (`ODUFLOW_AUTH_TOKEN`). Credentials are compared using `hmac.compare_digest` to prevent timing attacks.

### When auth is disabled

MCP auth and Web UI auth are configured independently:

- If `ODUFLOW_AUTH_TOKEN` is empty, the MCP endpoint runs without authentication
- If `ODUFLOW_UI_PASSWORD` is empty, the web dashboard runs without authentication

Warnings are logged on startup for each:

```
WARNING  HTTP auth DISABLED (ODUFLOW_AUTH_TOKEN not set)
WARNING  Web UI auth DISABLED (ODUFLOW_UI_PASSWORD not set)
```

### Git credentials

Private repository credentials are stored in the git credential store (`~/.git-credentials`) via the `setup_repo_auth` tool. The clean URL (without credentials) is always used in Docker labels and logs — credentials are never exposed.

### Odoo security defaults

The bundled `odoo.conf` template includes these security settings:
- `admin_passwd` set to a random value (prevents database manager access)
- `list_db = False` (hides database selector)
- `without_demo = all` (no demo data)
- `max_cron_threads = 0` (disables cron in dev environments)

---

## Use Cases & Workflows

### 🚀 Feature Branch Development

The most common workflow — test your changes against real production data:

```bash
# Create an environment for your feature branch
oduflow call create_environment feature-login https://github.com/company/odoo-addons.git odoo:17.0

# Make changes, push to remote, then pull into the environment
oduflow call pull_environment_repository feature-login
# Oduflow automatically installs/upgrades/restarts as needed

# When done, tear it down
oduflow call delete_environment feature-login
```

### 🐛 Bug Reproduction

Reproduce a production bug with real data:

```bash
# Spin up an environment with production data
oduflow call create_environment bug-12345 https://github.com/company/odoo-addons.git odoo:17.0

# Debug inside the container
oduflow call exec_in_environment bug-12345 "python3 -c 'import odoo; ...'"

# Check the database directly
oduflow call exec_in_environment bug-12345 "psql -h oduflow-db -U odoo -d oduflow_bug-12345 -c 'SELECT * FROM sale_order WHERE id=42;'"
```

### 🧪 Module Testing

Run Odoo tests in an isolated environment:

```bash
oduflow call create_environment test-suite https://github.com/company/odoo-addons.git odoo:17.0
oduflow call test_environment test-suite sale_custom,invoice_custom
oduflow call delete_environment test-suite
```

### 🌱 Greenfield Project (No Production Database)

Start a new Odoo project from scratch:

```bash
# Generate a clean template with common modules
oduflow init-template --odoo-image odoo:17.0 --modules base,web,contacts,sale,purchase,stock

# Customize the template interactively
oduflow template-up --odoo-image odoo:17.0
# → Install additional modules, configure settings, create demo users in the browser
oduflow template-down

# Now create environments that start with your customized setup
oduflow call create_environment dev https://github.com/company/new-project.git odoo:17.0
```

### 🔄 Multiple Odoo Versions

Manage environments across different Odoo versions using named templates:

```bash
# Set up templates for different versions
oduflow init-template --odoo-image odoo:15.0 --template-name v15
oduflow init-template --odoo-image odoo:17.0 --template-name v17

# Create environments targeting specific versions
oduflow call create_environment legacy-fix https://github.com/company/v15-addons.git odoo:15.0 v15
oduflow call create_environment new-feature https://github.com/company/v17-addons.git odoo:17.0 v17
```

### 🤖 AI-Assisted Development

Let your AI coding agent manage Odoo environments. Configure your MCP client (Cursor, Cline, Amp) to connect to `http://<host>:8000/mcp`, then:

> *"Create an Odoo 17 environment for the `feature-payment-gateway` branch from our repo. Install the `sale` and `payment` modules, then run the tests."*

The agent will call the appropriate MCP tools in sequence:
1. `create_environment` → provision the environment
2. `install_odoo_modules` → install the requested modules
3. `test_environment` → run the test suite
4. Report results back

#### Connecting Your Agent to Oduflow MCP

Add the Oduflow MCP server to your agent's configuration. The exact format depends on the client:

**Cursor / Windsurf** (`.cursor/mcp.json` or `.windsurf/mcp.json`):

```json
{
  "mcpServers": {
    "oduflow": {
      "type": "http",
      "url": "https://<your-oduflow-host>/mcp",
      "headers": {
        "Authorization": "Bearer test"
      }
    }
  }
}
```

**Amp** (`.amp/settings.json`):

```json
{
  "mcpServers": {
    "oduflow": {
      "type": "http",
      "url": "https://<your-oduflow-host>/mcp",
      "headers": {
        "Authorization": "Bearer test"
      }
    }
  }
}
```

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "oduflow": {
      "type": "http",
      "url": "https://<your-oduflow-host>/mcp",
      "headers": {
        "Authorization": "Bearer test"
      }
    }
  }
}
```

Replace `<your-oduflow-host>` with your Oduflow server address (e.g. `localhost:8000` or `oduflow.example.com`). The Bearer token must match the `ODUFLOW_AUTH_TOKEN` configured on the server.

#### Recommended Agent Rule (Cursor / Windsurf / Amp)

You can add the following rule to your AI coding agent to automate environment lifecycle management:

```
---
description: "Manage Odoo dev environments via the Flow MCP server"
alwaysApply: true
---
```

**Initialization**
1. **Check**: Call `list_environments`. If an environment matching the current branch already exists, use it.
2. **Create**: If not, use `create_environment`:
   - `branch_name`: `<current branch>`
   - `repo_url`: `<repository URL>` (HTTPS)
   - `odoo_image`: `odoo19_prod` (IMPORTANT: always use this image)
3. **Auth**: On a 401/403 error, suggest `setup_repo_auth`.
4. When creating or finding an existing environment, add the environment URL to `{@artifacts_path}/report.md`.

**Sync & Work Cycle**
1. **Push**: Run `git push` when the task is complete.
2. **Pull**: After every `push` (yours or user-requested), ALWAYS call `pull_environment_repository`.
3. **Automation**: The Flow server decides whether a restart or module upgrade is needed. You do NOT need to call `restart_environment` or `upgrade_odoo_modules`.

**Teardown**
- Only delete the environment via `delete_environment` if the task status is **Done** or **Canceled**.
- Do not recreate the environment to fix errors without the user's consent.

**Important**
- One task = one branch = one environment.
- Always display the environment URL to the user when creating an environment.

### 📊 Environment with Auxiliary Services

Set up a full-stack development environment:

```bash
# Create the Odoo environment
oduflow call create_environment dev https://github.com/company/odoo-addons.git odoo:17.0

# Add Redis for caching
oduflow call create_service redis redis:7 6379

# Add Meilisearch for full-text search
oduflow call create_service meilisearch getmeili/meilisearch:v1.6 7700 "" "MEILI_MASTER_KEY=devkey123"
```

All services share the `oduflow-net` Docker network and can communicate using container names as hostnames (e.g. `oduflow-svc-redis:6379`).

### 🔧 CI/CD Pipeline Integration

Use `oduflow call` in your CI pipeline:

```yaml
# .github/workflows/test.yml
steps:
  - name: Create test environment
    run: oduflow call create_environment ci-${{ github.sha }} ${{ github.repository }} odoo:17.0

  - name: Install and test
    run: |
      oduflow call install_odoo_modules ci-${{ github.sha }} my_module
      oduflow call test_environment ci-${{ github.sha }} my_module

  - name: Cleanup
    if: always()
    run: oduflow call delete_environment ci-${{ github.sha }}
```

### 📦 Importing a Template from Odoo or Another Workspace

You can create a template from an Odoo database backup or by copying a template directory from another Oduflow instance.

**From Odoo Database Manager:**

1. Go to `/web/database/manager` in your Odoo instance
2. Download a backup — **make sure to include the filestore** (the checkbox must be enabled, otherwise the template will be missing all attachments, images, and assets)
3. Extract the archive — it contains a `dump.sql` file and a `filestore/` directory
4. Place them into the template directory:

```bash
mkdir -p $ODUFLOW_HOME/templates/myproject
# Copy or move the extracted files
cp dump.sql $ODUFLOW_HOME/templates/myproject/
cp -r filestore $ODUFLOW_HOME/templates/myproject/
```

5. Load the template into PostgreSQL:

```bash
oduflow reload-template myproject
```

**From another Oduflow workspace:**

Simply copy the entire template directory and reload:

```bash
cp -r /other/oduflow/templates/myproject $ODUFLOW_HOME/templates/myproject
oduflow reload-template myproject
```

> ⚠️ The SQL dump is loaded into the shared PostgreSQL instance by `reload-template`. Without this step, the template will appear in the list but show **DB NOT LOADED** and cannot be used to create environments.

### 🏗️ Template Evolution

Evolve your template as the project grows:

```bash
# 1. Create an environment for template changes
oduflow call create_environment template-update https://github.com/company/odoo-addons.git odoo:17.0

# 2. Install new modules
oduflow call install_odoo_modules template-update accounting,hr,project

# 3. Verify everything works
oduflow call test_environment template-update accounting,hr,project

# 4. Promote to become the new template
oduflow call promote_environment template-update

# 5. All future environments will include these modules pre-installed
```

---

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

---

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

---

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

---

## PostgreSQL Tuning

The bundled `postgresql.conf` is optimized for a 2 vCPU / 4 GB RAM development server:

- **1 GB shared buffers** (25% of RAM)
- **16 MB work_mem** per query
- **256 MB maintenance_work_mem** for VACUUM and CREATE INDEX
- **WAL tuning**: 512 MB–2 GB WAL size, 15-minute checkpoint timeout
- **Aggressive autovacuum**: 30s naptime, 5% scale factor
- **Slow query logging**: queries over 1 second
- **HDD-optimized**: random_page_cost=4.0, effective_io_concurrency=2

---

## License

Oduflow is source-available under the [Polyform Noncommercial License 1.0.0](LICENSE). For business use or integrator licenses, visit [oduflow.dev](https://oduflow.dev).
