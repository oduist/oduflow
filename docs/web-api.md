# Web Dashboard & REST API

[TOC]

## Web Dashboard

![Web Dashboard — Agent Guides](img/agent_guides.png)

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

## REST API Endpoints

All endpoints return JSON with an `ok` field. Authentication via HTTP Basic auth when `ODUFLOW_AUTH_TOKEN` is set (user: `admin`, password: the token value).

### Environments

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/environments` | List all environments |
| `POST` | `/api/environments/create` | Create a new environment (JSON body: `branch_name`, `repo_url`, `odoo_image`, `template_name`) |
| `POST` | `/api/environments/{branch}/start` | Start an environment |
| `POST` | `/api/environments/{branch}/stop` | Stop an environment |
| `POST` | `/api/environments/{branch}/restart` | Restart an environment |
| `POST` | `/api/environments/{branch}/sync` | Pull latest code and auto-install/upgrade/restart |
| `POST` | `/api/environments/{branch}/delete` | Delete an environment |
| `GET` | `/api/environments/{branch}/logs?n=200` | Get environment logs |
| `POST` | `/api/environments/{branch}/protect` | Protect environment from deletion |
| `POST` | `/api/environments/{branch}/unprotect` | Remove protection from environment |

### Services

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/services` | List all managed services |
| `POST` | `/api/services/create` | Create a service (JSON body: `name`, `image`, `port`, `hostname`, `env_vars`) |
| `POST` | `/api/services/{name}/update` | Update (pull latest image & recreate) |
| `POST` | `/api/services/{name}/delete` | Delete a service |
| `GET` | `/api/services/{name}/logs?n=200` | Get service logs |

### Service Presets

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/service-presets` | List saved service presets |
| `POST` | `/api/service-presets/{name}/restore` | Restore a service from a saved preset |
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
| `POST` | `/api/extra-repos/add` | Clone an extra addons repo (JSON body: `name`, `repo_url`) |
| `POST` | `/api/extra-repos/{name}/delete` | Delete a cloned extra addons repository |

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
