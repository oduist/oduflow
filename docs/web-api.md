# Web Dashboard & REST API

## Web Dashboard

![Web Dashboard — Agent Guides](img/agent_guides.png)

When running in HTTP mode, a web dashboard is available at the server root (`http://<host>:<port>/`). It provides:

- **Environment list** with status indicators (running / stopped / partial)
- **Environment actions**: Start / Stop / Restart / Recreate / Protect / Delete
- **Environment creation** form (branch, repo URL, Odoo image, template, extra addons)
- **Environment protection** — toggle to prevent accidental deletion
- **Live log viewer** for each environment
- **Interactive terminal** — WebSocket-based Odoo Python shell directly in the browser
- **Container and system resource stats** (CPU, RAM, load average)
- **Service management** — create, update, restart, delete, and view logs for auxiliary services
- **Extra addons management** — clone, pull, protect, and delete extra addon repositories
- **Git credential management** — list, add, delete, and validate stored git credentials
- **Template listing** — view available template profiles with their status
- **License management** — view current license and activate license keys

## REST API Endpoints

All endpoints return JSON with an `ok` field. Authentication via HTTP Basic auth when `ui_password` is set in `oduflow.toml` (user: `admin`, password: the configured value). This is separate from the MCP Bearer token auth (`auth_token`).

### Environments

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/environments` | List all environments |
| `POST` | `/api/environments/create` | Create a new environment (JSON body: `env_name`, `repo_url`, `odoo_image`, `template_name`) |
| `POST` | `/api/environments/{branch}/start` | Start an environment |
| `POST` | `/api/environments/{branch}/stop` | Stop an environment |
| `POST` | `/api/environments/{branch}/restart` | Restart an environment |
| `POST` | `/api/environments/{branch}/sync` | Pull latest code and auto-install/upgrade/restart |
| `POST` | `/api/environments/{branch}/recreate` | Recreate an environment (delete + create with the same parameters) |
| `POST` | `/api/environments/{branch}/delete` | Delete an environment |
| `GET` | `/api/environments/{branch}/logs?n=200` | Get environment logs |
| `POST` | `/api/environments/{branch}/protect` | Protect environment from deletion |
| `POST` | `/api/environments/{branch}/unprotect` | Remove protection from environment |
| `WebSocket` | `/api/environments/{branch}/terminal` | Interactive Odoo Python shell via WebSocket (used by the Web Dashboard terminal) |

### Services

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/services` | List all managed services |
| `POST` | `/api/services/create` | Create a service (JSON body: `name`, `image`, `port`, `hostname`, `env_vars`, `host_mode`, `volumes`, `privileged`, `net_admin`) |
| `POST` | `/api/services/{name}/update` | Update a service — pull latest image and/or change settings (JSON body, all optional: `env_vars`, `image`, `port`, `hostname`, `host_mode`, `volumes`, `privileged`, `net_admin`); recreates on any change |
| `POST` | `/api/services/{name}/restart` | Restart a service |
| `POST` | `/api/services/{name}/delete` | Delete a service |
| `GET` | `/api/services/{name}/logs?n=200` | Get service logs |

### Service Presets

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/service-presets` | List saved service presets |
| `POST` | `/api/service-presets/restore` | Restore a service from a saved preset (JSON body: `name`, `image`, `port`, `hostname`, `env_vars`, `host_mode`, `volumes`, `privileged`, `net_admin`) |
| `POST` | `/api/service-presets/{name}/delete` | Delete a saved service preset |

### System

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/stats` | Container CPU/RAM stats + system metrics |
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
