# Multi-Team Support

Oduflow supports running **multiple isolated teams** within a single server instance. Each team has its own environments, templates, services, credentials, and port registry, while sharing the Docker network and PostgreSQL container.

## Configuration

Define teams in `oduflow.toml` using `[team.*]` sections:

```toml
[team.1]
hostname = "team-a.example.com"
auth_token = "token-team-a"
ui_password = "pass-a"
port_range = [50000, 50050]

[team.2]
hostname = "team-b.example.com"
auth_token = "token-team-b"
ui_password = "pass-b"
port_range = [50050, 50100]
```

Each team gets a dedicated data directory under the base `data_dir`:

```
/srv/oduflow/
├── team_1/
│   ├── workspaces/
│   ├── templates/
│   ├── shared_repos/
│   ├── ports.json
│   ├── .git-credentials
│   └── agent_guides/
├── team_2/
│   ├── workspaces/
│   ├── templates/
│   ├── shared_repos/
│   ├── ports.json
│   ├── .git-credentials
│   └── agent_guides/
```

## Team Resolution

When an MCP tool is called, Oduflow resolves the team using the following priority:

1. **Auth token** — matches the Bearer token against `auth_token` values in team configs
2. **Host header** — matches the HTTP `Host` header against team `hostname` values
3. **Single team** — if only one team is configured, uses it automatically
4. **Default** — falls back to team `"1"`

## Shared vs. Per-Team Resources

| Resource | Scope |
|---|---|
| Docker network (`oduflow-net`) | Shared |
| PostgreSQL container (`oduflow-db`) | Shared |
| Traefik container (`oduflow-traefik`) | Shared |
| Environments (workspaces, containers) | Per-team |
| Templates (DB snapshots, filestores) | Per-team |
| Extra addon repositories | Per-team |
| Auxiliary services | Per-team |
| Port assignments | Per-team |
| Git credentials | Per-team |

## Database Naming

Databases are namespaced by team ID:

- Environment DB: `oduflow_{team_id}_{slugified_branch}` (e.g. `oduflow_1_feature-login`)
- Template DB: `oduflow_template_{team_id}_{template_name}` (e.g. `oduflow_template_1_default`)

Containers are labeled with `oduflow.team={team_id}` for filtering.

## CLI Team Selection

CLI template and service commands accept a `--team` flag:

```bash
oduflow init-template --odoo-image odoo:17.0 --template-name myproject --team 2
oduflow list-templates --team 2
oduflow cleanup --team 2
```

The default is `--team 1`.
