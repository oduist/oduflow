# Installation

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

## Install Oduflow

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
| `ODUFLOW_INSTANCE_ID` | `1` | Instance identifier (1-9). Allows running multiple independent Oduflow instances. See [Multi-Instance Support](multi-instance.md) |
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

### Filestore

| Variable | Default | Description |
|---|---|---|
| `ODUFLOW_OVERLAY_THRESHOLD_MB` | `50` | Template filestore size threshold (MB). Templates smaller than this use a simple copy per environment; larger templates use fuse-overlayfs (saves disk). The decision is stored in `metadata.json` at template creation time. |

### Database

| Variable | Default | Description |
|---|---|---|
| `ODOO_DB_USER` | `odoo` | PostgreSQL user for the shared database container |
| `ODOO_DB_PASSWORD` | `odoo` | PostgreSQL password |

### Debug

| Variable | Default | Description |
|---|---|---|
| `ODUFLOW_TRACE` | *(empty)* | Set to `1` to enable detailed trace logging for git analysis and environment operations (file classification, field change detection, pull actions) |

### Configuration File Overrides

When `oduflow init` runs, it copies the bundled `postgresql.conf` and `odoo.conf` to `/etc/oduflow/`. These files take **priority** over the bundled defaults — edit them to customize PostgreSQL tuning or Odoo settings globally:

```
/etc/oduflow/
  postgresql.conf      ← custom PostgreSQL tuning (used by oduflow-db)
  odoo.conf            ← custom Odoo defaults (used by new environments)
  traefik/             ← Traefik dynamic configuration (auto-generated per instance)
```

If a repository contains an `odoo.conf` at its root, it takes priority over both the bundled and `/etc/oduflow/` versions for that specific environment.
