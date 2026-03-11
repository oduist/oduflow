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

All settings are configured via a TOML file. Oduflow searches for `oduflow.toml` in the following order:

1. `ODUFLOW_TOML` environment variable (explicit path)
2. `/etc/oduflow/oduflow.toml`
3. `~/.oduflow/conf/oduflow.toml`
4. `~/.oduflow/oduflow.toml`

If no config file exists when Oduflow starts, the bundled default is automatically copied to the appropriate location.

### Minimal configuration

```toml
[team.1]
hostname = "localhost"
```

### Full configuration reference

```toml
# ── Server ────────────────────────────────────────────
[server]
host = "0.0.0.0"           # HTTP server bind address
port = 8000                 # HTTP server port
# trace = false             # verbose tracing for git analysis & env ops

# ── Routing ───────────────────────────────────────────
[routing]
mode = "port"               # "port" (direct host port) | "traefik" (reverse proxy with auto-HTTPS)
# acme_email = "admin@example.com"  # required when mode = "traefik"

# ── Database ──────────────────────────────────────────
[database]
user = "odoo"               # PostgreSQL user for the shared database container
password = "odoo"           # PostgreSQL password
image = "postgres:15"       # PostgreSQL Docker image

# ── Storage ───────────────────────────────────────────
[storage]
# data_dir = "/srv/oduflow"         # base directory for all data (default: /srv/oduflow or ~/.oduflow/data)
overlay_threshold_mb = 50            # template filestore size threshold (MB) — larger uses fuse-overlayfs, smaller uses copy

# ── Teams ─────────────────────────────────────────────
# Each team gets isolated workspaces, templates, credentials, and services.
# At least one [team.*] section is required.

[team.1]
hostname = "localhost"               # port mode: http://{hostname}:{port}, traefik mode: https://{slug}.{hostname}
auth_token = ""                      # MCP bearer token (empty = MCP auth disabled)
ui_password = ""                     # Web UI password (empty = UI auth disabled)
port_range = [50000, 50100]          # port range for Odoo containers [start, end)
```

### Server settings

| Key | Default | Description |
|---|---|---|
| `[server].host` | `0.0.0.0` | HTTP server bind address |
| `[server].port` | `8000` | HTTP server port |
| `[server].trace` | `false` | Enable detailed trace logging for git analysis and environment operations |
| `[server].disable_telemetry` | `false` | Disable anonymous usage telemetry (see [Telemetry](#telemetry)) |

### Routing settings

| Key | Default | Description |
|---|---|---|
| `[routing].mode` | `port` | `port` — direct host port mapping; `traefik` — reverse proxy with auto-HTTPS |
| `[routing].acme_email` | *(empty)* | Let's Encrypt email for TLS certificates. Required when `mode = "traefik"` |

### Database settings

| Key | Default | Description |
|---|---|---|
| `[database].user` | `odoo` | PostgreSQL user for the shared database container |
| `[database].password` | `odoo` | PostgreSQL password |
| `[database].image` | `postgres:15` | PostgreSQL Docker image |

### Storage settings

| Key | Default | Description |
|---|---|---|
| `[storage].data_dir` | `/srv/oduflow` or `~/.oduflow/data` | Base directory for all data. Team data directories are `team_{ID}` subdirectories inside |
| `[storage].overlay_threshold_mb` | `50` | Template filestore size threshold (MB). Templates smaller than this use a simple copy per environment; larger templates use fuse-overlayfs. The decision is stored in `metadata.json` at template creation time |

### Per-team settings

Each `[team.*]` section defines an isolated team with its own workspaces, templates, credentials, and services. At least one team is required.

| Key | Default | Description |
|---|---|---|
| `hostname` | `localhost` | Team hostname. In port mode: `http://{hostname}:{port}`. In traefik mode: `https://{slug}.{hostname}` |
| `auth_token` | *(empty)* | Bearer token for MCP HTTP auth. Empty = MCP auth disabled for this team |
| `ui_password` | *(empty)* | Password for Web UI Basic auth (user: `admin`). Separate from MCP auth token. Empty = UI auth disabled |
| `port_range` | `[50000, 50100]` | Port range for Odoo containers `[start, end)` — supports up to 100 concurrent environments |

Team data is stored at `{data_dir}/team_{ID}/`:

```
team_{ID}/
├── workspaces/           # Per-branch environments
├── templates/            # Reusable database snapshots
├── shared_repos/         # Extra addon repositories (bare clones)
├── ports.json            # Port registry
├── .git-credentials      # Git credentials for this team
└── agent_guides/         # AI agent guides (markdown)
```

### Configuration file overrides

On startup, Oduflow copies the bundled `postgresql.conf` and `odoo.conf` to the config directory (if they don't already exist). These files take **priority** over the bundled defaults — edit them to customize PostgreSQL tuning or Odoo settings globally:

```
/etc/oduflow/             (or ~/.oduflow/conf/)
  oduflow.toml            ← main configuration file
  postgresql.conf         ← custom PostgreSQL tuning (used by oduflow-db)
  odoo.conf               ← custom Odoo defaults (used by new environments)
  license.key             ← license file (optional)
  traefik/                ← Traefik dynamic configuration (auto-generated)
```

If a repository contains an `odoo.conf` at its root, it takes priority over both the bundled and system-level versions for that specific environment.

## Telemetry

Oduflow collects **anonymous** usage telemetry to help us understand adoption and prioritize development. Two events are sent:

- **`first_run`** — sent once on the very first startup (when the instance ID is created).
- **`env_created`** — sent each time a new environment is provisioned.

Each event contains only:

- The event name
- The oduflow version
- A random instance ID (UUID)

**No** personal data, hostnames, IP addresses, branch names, repository URLs, or environment details are collected.

### Opt out

Add to your `oduflow.toml`:

```toml
[server]
disable_telemetry = true
```

## Auto-start with systemd

On Linux servers, Oduflow can be registered as a systemd service so it starts automatically on boot.

### Prerequisites

```bash
# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install oduflow as a tool (as root)
uv tool install oduflow

# Create the configuration file (optional — Oduflow auto-creates a default oduflow.toml on first start)
```

### Install the service

```bash
oduflow systemd-install
```

This will:

1. Generate a systemd unit file at `/etc/systemd/system/oduflow.service`
2. Run `systemctl daemon-reload`
3. Enable the service for auto-start on boot

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
oduflow systemd-uninstall
```

This stops, disables, and removes the unit file.
