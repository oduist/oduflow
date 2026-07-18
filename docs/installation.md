# Installation

## System Requirements

- **Docker** (Docker Engine or Docker Desktop)
- **Python 3.10+**
- **Git**
- **fuse-overlayfs** (Linux only, for filestore overlay mounting) — auto-installed on first launch; see below

!!! note "macOS support"
    On macOS, Docker Desktop runs containers inside a Linux VM and projects
    files via VirtioFS. **fuse-overlayfs is not needed** — filestore overlays
    are skipped and a plain directory is used instead.
    File ownership (`chown`) is handled automatically: Oduflow detects the
    `PermissionError` that VirtioFS raises and falls back to running `chown`
    inside a throwaway container. No extra configuration is required.

### Install fuse-overlayfs

On Linux, Oduflow **auto-installs `fuse-overlayfs` on first launch** if it is
missing — it runs `apt-get install -y fuse-overlayfs` when it starts as **root**
on a Debian/Ubuntu host (the Docker image already bundles it). This is
best-effort: if Oduflow is not running as root, `apt-get` is unavailable, or the
install fails, it logs a warning and you can install the package yourself:

```bash
sudo apt install fuse-overlayfs
```

The `/dev/fuse` device must be available (present by default on Ubuntu).

Oduflow mounts each environment's filestore with `fuse-overlayfs`'s `allow_other` option so the Odoo container's (non-root) user can read it. When Oduflow runs as **root** — the default and recommended setup — no further configuration is needed. Only if you run Oduflow as a **non-root user** must you uncomment `user_allow_other` in `/etc/fuse.conf`:

```bash
# Only needed when running Oduflow as a non-root user:
sudo sed -i 's/^#user_allow_other/user_allow_other/' /etc/fuse.conf
```

## Install Oduflow

### Run without installing

With [uv](https://docs.astral.sh/uv/) you can run Oduflow directly — no installation step needed. `uvx` downloads the package into a temporary environment and runs it:

```bash
uvx oduflow                      # stdio mode (default)
uvx oduflow --transport http     # HTTP server mode
```

This is the quickest way to try Oduflow or use it in CI pipelines.

### Permanent installation

Install via [uv](https://docs.astral.sh/uv/) (recommended — manages an isolated environment automatically):

```bash
uv tool install oduflow
```

Alternative — install via pip:

```bash
pip install oduflow
```

After installation, the `oduflow` command is available globally.

### From source

```bash
git clone https://github.com/oduist/oduflow.git
cd oduflow
uv sync          # or: python -m venv .venv && pip install -e .
```

### Upgrade

```bash
uv tool upgrade oduflow
```

During upgrade, Oduflow overwrites bundled files (agent guides, `postgresql.conf`, sanitize scripts, `odoo.conf`) with the latest versions. If you have customized any of these files and want to prevent them from being overwritten, add `# KEEP` as the **very first line** of the file:

```conf
# KEEP
# My custom postgresql.conf
listen_addresses = '*'
...
```

Files marked with `# KEEP` will be skipped during upgrade and listed as `(kept)` in the upgrade output.

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
allow_local_path = true     # allow live-mount (bind local checkout) environments
# allow_insecure_http = false  # serve /mcp over HTTP with NO auth (only behind your own proxy)
# trace = false             # verbose tracing for git analysis & env ops

# ── Routing ───────────────────────────────────────────
[routing]
mode = "port"               # "port" (direct host port) | "traefik" (reverse proxy with auto-HTTPS)
# acme_email = "admin@example.com"  # required when mode = "traefik" and tls = true
# tls = true                # traefik only. false = plain HTTP on :80, no ACME (behind a Cloudflare tunnel / TLS proxy)
# hostname = "localhost"    # port mode only: default host for teams without their own
                            # (traefik requires each team to set its own hostname)

# ── OAuth (optional) ──────────────────────────────────
# In traefik mode the self-hosted OAuth 2.1 Authorization Server is enabled
# automatically and runs on each team's own hostname (issuer derived per-request),
# so oauth_base_url is NOT needed. Set it only to pin a fixed issuer, or in port
# mode: the public URL of this instance (for Claude.ai and other OAuth MCP
# clients). OAuth client_id = team_<id> (non-secret); auth_token = client_secret = token.
[oauth]
# oauth_base_url = "https://oduflow.example.com"

# ── Database ──────────────────────────────────────────
[database]
user = "odoo"               # PostgreSQL user for the shared database container
password = "odoo"           # PostgreSQL password (auto-generated on first init; set to override)
image = "postgres:15"       # PostgreSQL Docker image

# ── Storage ───────────────────────────────────────────
[storage]
# data_dir = "/srv/oduflow"         # base directory for all data (default: /srv/oduflow or ~/.oduflow/data)
overlay_threshold_mb = 50            # template filestore size threshold (MB) — larger uses fuse-overlayfs, smaller uses copy

# ── Lifecycle ─────────────────────────────────────────
[lifecycle]
auto_stop_hours = 48        # auto-stop environments idle for N hours (no MCP/dashboard work); 0 disables
auto_delete_hours = 0       # auto-delete environments stopped for N hours; 0 disables (opt-in; DESTRUCTIVE, protected envs exempt)

# ── Coding agent (optional) ───────────────────────────
# One agent container per team (Claude Code + OpenAI Codex), driven from the
# dashboard (Agent Chat / Agent CLI). Opt-in per team via agent_enabled below.
# [agent]
# image = "oduist/oduflow-coder:latest"
# claude_model = ""         # optional Claude model override; empty = CLI default
# codex_model = ""          # optional Codex model override; empty = CLI default

# ── Teams ─────────────────────────────────────────────
# Each team gets isolated workspaces, templates, credentials, and services.
# At least one [team.*] section is required.

[team.1]
hostname = "localhost"               # port mode: http://{hostname}:{port}, traefik mode: https://{slug}.{hostname}
auth_token = ""                      # MCP bearer token (empty = MCP auth disabled)
ui_password = ""                     # Web UI password (empty = UI auth disabled)
port_range = [50000, 50100]          # port range for Odoo containers [start, end)
# agent_enabled = false              # enable the per-team coding agent (Agent Chat / Agent CLI)
# agent_default = "claude"           # "claude" | "codex" — default agent for consoles/chats
# [team.1.agent_env]                 # provider credentials injected into the agent container
# CLAUDE_CODE_OAUTH_TOKEN = ""
# ANTHROPIC_API_KEY = ""
# OPENAI_API_KEY = ""
```

### Server settings

| Key | Default | Description |
|---|---|---|
| `[server].host` | `0.0.0.0` | HTTP server bind address |
| `[server].port` | `8000` | HTTP server port |
| `[server].allow_local_path` | `true` | Allow live-mount (`local_path`) environments that bind-mount a local checkout instead of cloning. Set `false` to force git-clone delivery only |
| `[server].allow_insecure_http` | `false` | Serve the `/mcp` endpoint over plain HTTP with **no** authentication. Only enable behind your own authenticating proxy |
| `[server].trace` | `false` | Enable detailed trace logging for git analysis and environment operations |
| `[server].disable_telemetry` | `false` | Disable anonymous usage telemetry (see [Telemetry](#telemetry)) |

### Routing settings

| Key | Default | Description |
|---|---|---|
| `[routing].mode` | `port` | `port` — direct host port mapping; `traefik` — reverse proxy with auto-HTTPS |
| `[routing].acme_email` | *(empty)* | Let's Encrypt email for TLS certificates. Required when `mode = "traefik"` and `tls = true` |
| `[routing].tls` | `true` | Traefik only. `true`: Traefik terminates TLS (:443, HTTP→HTTPS redirect, Let's Encrypt). `false`: plain HTTP on :80 only, no redirect/ACME — for a TLS-terminating upstream (e.g. a Cloudflare tunnel). Public URLs stay `https://` either way |
| `[routing].hostname` | `localhost` | Default hostname for teams that don't set their own `hostname` |

### OAuth settings

| Key | Default | Description |
|---|---|---|
| `[oauth].oauth_base_url` | *(empty)* | Public URL of this Oduflow instance used as the OAuth issuer. Oduflow runs a self-hosted OAuth 2.1 Authorization Server (exposes `/.well-known/oauth-authorization-server`, `/authorize`, `/token`) so OAuth-based MCP clients like Claude.ai can connect; the OAuth `client_id` is the non-secret `team_<id>` (e.g. `team_1`) and each team's `auth_token` is the `client_secret` and the issued access token. **In traefik mode this is enabled automatically and the issuer is derived per-request from each team's own hostname — leave empty.** Set it to pin a fixed issuer, or in port mode. Empty + port mode = plain Bearer-token auth only. See [Authentication & Security](security.md) |

### Database settings

| Key | Default | Description |
|---|---|---|
| `[database].user` | `odoo` | PostgreSQL user for the shared database container |
| `[database].password` | `odoo` | PostgreSQL password. The bundled config omits it and one is auto-generated on first init; set explicitly to override |
| `[database].image` | `postgres:15` | PostgreSQL Docker image |

### Storage settings

| Key | Default | Description |
|---|---|---|
| `[storage].data_dir` | `/srv/oduflow` or `~/.oduflow/data` | Base directory for all data. Team data directories are `team_{ID}` subdirectories inside |
| `[storage].overlay_threshold_mb` | `50` | Template filestore size threshold (MB). Templates smaller than this use a simple copy per environment; larger templates use fuse-overlayfs. The decision is stored in `metadata.json` at template creation time |
| `[lifecycle].auto_stop_hours` | `48` | Auto-stop environments after N hours without work (env-scoped MCP calls or dashboard actions). `0` disables. Protected environments are exempt |
| `[lifecycle].auto_delete_hours` | `0` | Auto-delete stopped environments N hours after they stopped (manual stops count). Default `0` = **disabled** — auto-delete is opt-in and destructive; set a positive value to enable. Protected environments are exempt; `pull_and_apply` wakes a stopped environment automatically |

### Agent settings

The global `[agent]` section holds deployment-wide settings for the per-team coding agent (see [Coding Agent](agent.md)). Per-team enablement lives in the `[team.*]` sections below.

| Key | Default | Description |
|---|---|---|
| `[agent].image` | `oduist/oduflow-coder:latest` | Image for the per-team coding-agent container (Claude Code + OpenAI Codex) |
| `[agent].claude_model` | *(empty)* | Optional Claude model override for the agent; empty = CLI default |
| `[agent].codex_model` | *(empty)* | Optional Codex model override for the agent; empty = CLI default |

### Per-team settings

Each `[team.*]` section defines an isolated team with its own workspaces, templates, credentials, and services. At least one team is required.

| Key | Default | Description |
|---|---|---|
| `hostname` | `localhost` | Team hostname. In port mode: `http://{hostname}:{port}`. In traefik mode: `https://{slug}.{hostname}` |
| `auth_token` | *(empty)* | Bearer token for MCP HTTP auth. Empty = MCP auth disabled for this team |
| `ui_password` | *(empty)* | Password for Web UI Basic auth (user: `admin`). Separate from MCP auth token. Empty = UI auth disabled |
| `port_range` | `[50000, 50100]` | Port range for Odoo containers `[start, end)` — supports up to 100 concurrent environments |
| `agent_enabled` | `false` | Enable the per-team coding agent (dashboard Agent Chat / Agent CLI). Off by default |
| `agent_default` | `claude` | Which agent consoles/chats open by default: `claude` or `codex` |
| `[team.X.agent_env]` | *(empty)* | Sub-table of environment variables injected into the team's agent container — provider credentials (`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) and any custom vars |

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

If a repository contains an `odoo.conf` in its `.oduflow/` directory (`<repo>/.oduflow/odoo.conf`), it takes priority over both the bundled and system-level versions for that specific environment.

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

# Apply oduflow.toml changes WITHOUT downtime (hot reload — see below)
systemctl reload oduflow

# Full restart (only needed for host/port/routing/data_dir/database changes)
systemctl restart oduflow
```

### Apply config changes without a restart

Editing `oduflow.toml` no longer requires restarting the service. The running
server can **reload the file in place** — provisioning a new `[team.X]`, changing
a quota, lifecycle window, or agent setting takes effect immediately and does not
disrupt other teams:

```bash
# Validate the on-disk config first (exits non-zero on error; does not signal)
oduflow reload --check

# Apply it to the running server (sends SIGHUP)
oduflow reload
# …or, under systemd:
systemctl reload oduflow
```

The reload is **validate-before-apply**: if the new `oduflow.toml` is invalid the
running server is left untouched and keeps serving the previous config. The result
is reported in the server logs. A handful of settings are read only at startup and
still need a full `restart` to take effect — `[server] host`/`port`,
`[routing] mode`, `[server] allow_insecure_http`, `[storage] data_dir`, the
`[database]` credentials/image, and `[oauth] oauth_base_url`; the reload log warns
when a changed field requires one. Removing a `[team.X]` section stops serving that
team but never deletes its data.

Because Oduflow is just a *reload target*, any config-management tool can drive it —
for example a Salt/Ansible handler that renders `oduflow.toml`, gates on
`oduflow reload --check`, then runs `oduflow reload`.

### Remove the service

```bash
oduflow systemd-uninstall
```

This stops, disables, and removes the unit file.
