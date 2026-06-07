# Authentication & Security

## MCP HTTP Auth

When `auth_token` is set for a team in `oduflow.toml`, the MCP endpoint (`/mcp`) requires a Bearer token:

```
Authorization: Bearer <your-token>
```

Each team can have its own auth token:

```toml
[team.1]
auth_token = "secret-token-team-1"

[team.2]
auth_token = "secret-token-team-2"
```

The token is used to both authenticate and identify the team. This is implemented via FastMCP's `StaticTokenVerifier`.

## Self-hosted OAuth (for Claude.ai and other MCP clients)

Oduflow can act as its own OAuth 2.1 Authorization Server, so MCP clients that require an OAuth flow (e.g. Claude.ai Remote MCP, MCP Inspector) can connect without any external identity provider.

When the OAuth flow completes, the issued `access_token` is exactly the team's `auth_token` from `oduflow.toml` — so the same value works as a plain Bearer token for CLI clients.

### Setup

In `oduflow.toml`, set the public URL of this instance and an `auth_token` per team:

```toml
[oauth]
oauth_base_url = "https://your-server.com"

[team.1]
auth_token = "secret-token-team-1"
```

That's it. Oduflow now exposes:

- `GET /.well-known/oauth-authorization-server` — discovery metadata
- `GET /authorize` — authorization endpoint (Authorization Code + PKCE)
- `POST /token` — token endpoint

Dynamic Client Registration (`/register`) is **disabled** — clients must use the preregistered credentials.

### Connecting from Claude.ai

1. Go to Claude.ai Settings → Connectors → Add custom MCP
2. Enter your Oduflow URL: `https://your-server.com/mcp`
3. In the OAuth fields enter the team's `auth_token` as **both** `Client ID` **and** `Client Secret`:

   ```
   Client ID     = secret-token-team-1
   Client Secret = secret-token-team-1
   ```

4. Claude.ai performs the OAuth flow against your Oduflow instance, receives an access token, and connects.

The team is identified by the `auth_token`, so each team's claude.ai connector ends up scoped to its own workspaces, templates, and credentials.

### Bearer-only mode (CLI / automation)

For curl, IDE clients, or anything that doesn't need OAuth, simply send the `auth_token` as a Bearer header:

```
Authorization: Bearer secret-token-team-1
```

This works whether or not `oauth_base_url` is configured.

## Web Dashboard Auth

The web dashboard and REST API use HTTP Basic authentication with a **separate** password:

- **Username**: `admin`
- **Password**: value of `ui_password` from `oduflow.toml`

This is independent from the MCP Bearer token (`auth_token`). Credentials are compared using `hmac.compare_digest` to prevent timing attacks.

## When auth is disabled

MCP auth and Web UI auth are configured independently per team:

- If `auth_token` is empty, the MCP endpoint runs without authentication
- If `ui_password` is empty, the web dashboard runs without authentication

Warnings are logged on startup for each team:

```
INFO  [team.1] http://localhost:8000/ (MCP token OFF, OAuth OFF, UI auth OFF)
```

When OAuth is enabled the status reads `OAuth ON (self-hosted)`.

## Git Credentials

![Credentials Management](img/credentials.png)

Private repository credentials are stored in the git credential store at `{team_data_dir}/.git-credentials` (per-team) via the `setup_repo_auth` tool. The clean URL (without credentials) is always used in Docker labels and logs — credentials are never exposed.

### Managing credentials via MCP

```bash
# Store credentials for a private repository
oduflow call setup_repo_auth https://user:PAT@github.com/owner/private-repo.git
```

The tool parses the URL, stores the credentials, and verifies access by running `git ls-remote`.

### Managing credentials via REST API and Web Dashboard

The Web Dashboard and REST API provide full credential lifecycle management:

| Action | REST API |
|---|---|
| **List** all stored credentials | `GET /api/credentials` |
| **Add** credentials for a repository | `POST /api/credentials/add` (body: `repo_url`) |
| **Delete** a stored credential | `POST /api/credentials/delete` (body: `host`, `username`) |
| **Validate** a credential against the provider | `POST /api/credentials/validate` (body: `host`, `username`) |

Validation checks the credential against the provider's API (GitHub, GitLab, Bitbucket). For other hosts, it reports `"valid"` if the credential exists. Tokens are always masked in API responses (e.g. `ghp_****`).

## iptables rule

On startup, an `iptables ACCEPT` rule is automatically added for the `oduflow-net` Docker bridge interface. This ensures that containers on the shared network can communicate with the host (required for Traefik `host.docker.internal` routing and PostgreSQL access). If `iptables` is not available, the rule is skipped with a warning.

## Odoo security defaults

The bundled `odoo.conf` template includes these security settings:

- `admin_passwd` set to a random value (prevents database manager access)
- `list_db = False` (hides database selector)
- `without_demo = all` (no demo data)
- `max_cron_threads = 0` (disables cron in dev environments)
