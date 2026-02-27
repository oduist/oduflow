# Installation

[TOC]

## System Requirements

- **Docker** (Docker Engine or Docker Desktop)
- **Python 3.10+**
- **Git**
- **fuse-overlayfs** (for filestore overlay mounting)

!!! note "macOS support"
    On macOS, Docker Desktop runs containers inside a Linux VM and projects
    files via VirtioFS. **fuse-overlayfs is not needed** — filestore overlays
    are skipped and a plain directory is used instead.
    File ownership (`chown`) is handled automatically: Oduflow detects the
    `PermissionError` that VirtioFS raises and falls back to running `chown`
    inside a throwaway container. No extra configuration is required.

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

Recommended — install via [uv](https://docs.astral.sh/uv/) (manages an isolated environment automatically):

```bash
uv tool install oduflow
```

Alternative — install via pip:

```bash
pip install oduflow
```

For local development:

```bash
git clone https://github.com/oduist/oduflow.git
cd oduflow
uv sync          # or: python -m venv .venv && pip install -e .
```

### Upgrade

```bash
uv tool upgrade oduflow
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
| `ODUFLOW_STATELESS_HTTP` | `true` | When `true`, the MCP HTTP transport runs in stateless mode (no session tracking). Set to `false` to enable session-based communication |

### Paths

| Variable | Default | Description |
|---|---|---|
| `ODUFLOW_INSTANCE_ID` | `1` | Instance identifier (1-9). Allows running multiple independent Oduflow instances. See [Multi-Instance Support](multi-instance.md) |
| `ODUFLOW_DATA_DIR` | `/srv/oduflow` | Base directory for all data (instance dirs are `instance_{ID}` subdirectories inside) |
| `ODUFLOW_ETC_DIR` | `/etc/oduflow` or `~/.oduflow/conf` | Config and credentials directory. Defaults to `/etc/oduflow` when writable (Docker), otherwise `~/.oduflow/conf` |
| `ODUFLOW_PORT_REGISTRY` | `$ODUFLOW_DATA_DIR/instance_{ID}/ports.json` | JSON file for stable port assignments |

Template folder structure: `$ODUFLOW_DATA_DIR/instance_{ID}/templates/<name>/dump.sql` (or `dump.pgdump`) and `$ODUFLOW_DATA_DIR/instance_{ID}/templates/<name>/filestore/`.

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

## Auto-start with systemd

On Linux servers, Oduflow can be registered as a systemd service so it starts automatically on boot.

### Prerequisites

```bash
# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install oduflow as a tool (as root)
uv tool install oduflow

# Initialize shared infrastructure and instance directories
oduflow init
oduflow init-instance
```

`init-instance` creates an environment file at `/etc/oduflow/instance_{ID}.env` (seeded from the bundled `.env.example`). Edit it to configure your instance — set `ODUFLOW_AUTH_TOKEN`, `EXTERNAL_HOST`, routing mode, etc.

### Install the service

```bash
oduflow systemd-install --instance 1
```

This will:

1. Generate a systemd unit file at `/etc/systemd/system/oduflow.service`
2. Run `systemctl daemon-reload`
3. Enable the service for auto-start on boot

For multi-instance setups, the service is named `oduflow-{ID}.service`:

```bash
oduflow systemd-install --instance 2
# → /etc/systemd/system/oduflow-2.service
```

### Manage the service

```bash
# Start
systemctl start oduflow

# Status
systemctl status oduflow

# Logs (follow)
journalctl -u oduflow -f

# Restart after config changes
systemctl restart oduflow
```

### Remove the service

```bash
oduflow systemd-uninstall --instance 1
```

This stops, disables, and removes the unit file.
