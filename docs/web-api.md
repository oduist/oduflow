# Web Dashboard & REST API

## Web Dashboard

![Web Dashboard — Agent Guides](img/agent_guides.png)

When running in HTTP mode, a web dashboard is available at the server root (`http://<host>:<port>/`). It provides:

- **Environment list** with status indicators (running / stopped / partial)
- **Environment actions**: Start / Stop / Restart / Update / Recreate / Protect / Delete
- **Environment creation** form (branch, repo URL, Odoo image, template, extra addons, environment variables)
- **Environment protection** — toggle to prevent accidental deletion
- **Live log viewer** for each environment
- **Interactive terminal** — WebSocket-based Odoo Python shell directly in the browser
- **Container and system resource stats** (CPU, RAM, load average)
- **Service management** — create, update, restart, delete, and view logs for auxiliary services
- **Extra addons management** — clone, pull, protect, and delete extra addon repositories
- **Git credential management** — list, add, delete, and validate stored git credentials
- **Template listing** — view available template profiles with their status
- **License management** — view current license and activate license keys
- **Coding agent** (opt-in, per team) — **Agent CLI** (the agent's terminal in the browser) and **Agent Chat** (a structured ACP chat) for each environment, when `agent_enabled` is set for the team. Hidden for live-mount (`local_path`) environments. See [Coding Agent](agent.md)

## REST API Endpoints

All endpoints return JSON with an `ok` field. Authentication via HTTP Basic auth when `ui_password` is set in `oduflow.toml` (user: `admin`, password: the configured value). This is separate from the MCP Bearer token auth (`auth_token`).

### Environments

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/environments` | List all environments |
| `POST` | `/api/environments/create` | Create a new environment (JSON body: `env_name`, `repo_url`, `odoo_image`, `template_name`, `extra_addons`, `auto_install_modules`, `env_vars`, `git_user`) |
| `POST` | `/api/environments/{branch}/start` | Start an environment |
| `POST` | `/api/environments/{branch}/stop` | Stop an environment |
| `POST` | `/api/environments/{branch}/restart` | Restart an environment |
| `POST` | `/api/environments/{branch}/update` | Re-create the container, preserving DB and filestore (JSON body, all optional: `env_vars`, `odoo_image`) |
| `POST` | `/api/environments/{branch}/sync` | Pull latest code and auto-install/upgrade/restart |
| `POST` | `/api/environments/{branch}/recreate` | Recreate an environment (delete + create with the same parameters) |
| `POST` | `/api/environments/{branch}/delete` | Delete an environment |
| `GET` | `/api/environments/{branch}/logs?n=200` | Get environment logs |
| `POST` | `/api/environments/{branch}/protect` | Protect environment from deletion |
| `POST` | `/api/environments/{branch}/unprotect` | Remove protection from environment |
| `POST` | `/api/environments/{branch}/storage/refresh` | Recompute the environment's DB size and workspace disk size (cached; served via `/api/stats` and `/api/usage`) |
| `WebSocket` | `/api/environments/{branch}/terminal` | Interactive Odoo Python shell via WebSocket (used by the Web Dashboard terminal) |

### Services

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/services` | List all managed services |
| `POST` | `/api/services/create` | Create a service. Pass either `port` or Traefik `routes: [{path, port, strip_prefix}]`, plus optional `hostname`, `env_vars`, `host_mode`, volumes and capabilities |
| `POST` | `/api/services/{name}/update` | Pull latest image and/or change settings. `routes` fully replaces the route list; `routes: []` plus `port` returns to catch-all mode |
| `POST` | `/api/services/{name}/restart` | Restart a service |
| `POST` | `/api/services/{name}/delete` | Delete a service |
| `GET` | `/api/services/{name}/logs?n=200` | Get service logs |

### Service Presets

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/service-presets` | List saved service presets |
| `POST` | `/api/service-presets/restore` | Restore a service configuration, including its single port or restricted HTTP routes |
| `POST` | `/api/service-presets/{name}/delete` | Delete a saved service preset |

### System

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/stats` | Container CPU/RAM stats + system metrics + cached per-environment DB/disk sizes |
| `GET` | `/api/usage` | Cached team storage usage (per environment + team totals) and the team's quotas — the read side for external billing/quota tooling |
| `POST` | `/api/usage/refresh` | Recompute storage for every environment plus team totals (heavy: walks every workspace); returns the same payload as `GET /api/usage` |
| `GET` | `/api/templates` | List available template profiles |

### Extra Addons

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/extra-repos` | List all cloned extra addons repositories |
| `POST` | `/api/extra-repos/add` | Clone an extra addons repo (JSON body: `name`, `repo_url`, `git_user`) |
| `POST` | `/api/extra-repos/{name}/pull` | Fetch latest changes from the remote for an extra repo |
| `POST` | `/api/extra-repos/{name}/protect` | Protect an extra repo from deletion |
| `POST` | `/api/extra-repos/{name}/unprotect` | Remove protection from an extra repo |
| `POST` | `/api/extra-repos/{name}/delete` | Delete a cloned extra addons repository |

### Credentials

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/credentials` | List all stored git credentials |
| `POST` | `/api/credentials/add` | Store git credentials for a repository (JSON body: `repo_url`) |
| `POST` | `/api/credentials/delete` | Delete a stored credential (JSON body: `host`, `username`) |
| `POST` | `/api/credentials/validate` | Validate a stored credential (JSON body: `host`, `username`) |

### Licensing

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/license` | Get current license information |
| `POST` | `/api/license/activate` | Activate a license key (JSON body: `key`) |

### Agent Guides

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/agent-guides` | List all available agent guides |
| `GET` | `/api/agent-guides/{filename}` | Get content of a specific agent guide |

### Coding Agent

The per-team coding agent (see [Coding Agent](agent.md)) exposes one status endpoint and two WebSocket surfaces. Available only when `agent_enabled` is set for the team.

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/agent` | Whether the coding agent is enabled for the team and its default type (`claude` / `codex`) |
| `WebSocket` | `/api/environments/{branch}/agent` | **Agent CLI** — the agent's interactive TUI, bridged from a PTY `docker exec` in the team's agent container (used by the dashboard console) |
| `WebSocket` | `/api/environments/{branch}/agent-acp` | **Agent Chat** — a line-framed relay to the agent's ACP adapter (`claude-code-acp` / `codex-acp`), rendered by the browser chat client with durable per-environment sessions |
