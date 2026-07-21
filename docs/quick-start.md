# Quick Start

## Install

The fastest way — run directly without installing (requires [uv](https://docs.astral.sh/uv/)):

```bash
uvx oduflow
```

Or install permanently:

```bash
uv tool install oduflow
```

On first launch, Oduflow automatically:

- Creates a default `oduflow.toml` config (in `/etc/oduflow/` or `~/.oduflow/conf/`)
- Initializes shared infrastructure (Docker network, PostgreSQL, team directories)

## Single-user mode (stdio)

Stdio is the default transport — Oduflow communicates with the MCP client over stdin/stdout. The client starts and manages the Oduflow process directly. No network port is needed.

```bash
# These are all equivalent:
uvx oduflow
oduflow
oduflow --transport stdio
```

Add to your MCP client config (Claude Desktop, Windsurf, etc.):

```json
{
  "mcpServers": {
    "oduflow": {
      "command": "uvx",
      "args": ["oduflow"]
    }
  }
}
```

If Oduflow is installed globally (`uv tool install oduflow`), you can use the shorter form:

```json
{
  "mcpServers": {
    "oduflow": {
      "command": "oduflow"
    }
  }
}
```

## Server mode (HTTP)

HTTP transport starts a persistent server with Streamable HTTP, a Web Dashboard, and a REST API. Suitable for remote and multi-user deployments.

```bash
# Start the HTTP server:
uvx oduflow --transport http
uvx oduflow -t http
# or, if installed:
oduflow --transport http
oduflow -t http
```

The server starts on `http://0.0.0.0:8000` by default (configurable via `[server]` section in `oduflow.toml`). The MCP endpoint is at `/mcp`.

### Authentication

In HTTP mode it is recommended to set a Bearer token for MCP and a password for the Web Dashboard. Add to your `oduflow.toml`:

```toml
[team.1]
hostname = "localhost"
auth_token = "my-secret-mcp-token"      # Bearer token for MCP clients
ui_password = "my-dashboard-password"    # Basic auth for Web Dashboard (user: admin)
```

MCP auth and Web Dashboard auth are independent — they use different tokens and different mechanisms (Bearer vs Basic).

### Self-hosted OAuth (Claude.ai)

Some MCP clients (e.g. Claude.ai Remote MCP) require an OAuth flow instead of a static Bearer token. Oduflow can act as its own OAuth 2.1 Authorization Server — no external identity provider needed. In [traefik mode](traefik.md) it's enabled automatically and runs on each team's own hostname, so no extra config is required. In port mode, set the public URL of this instance in `oduflow.toml`:

```toml
[oauth]
oauth_base_url = "https://oduflow.example.com"
```

The OAuth `client_id` is the non-secret `team_<id>` (e.g. `team_1`); each team's `auth_token` is the `client_secret` and the issued access token. See [Authentication & Security](security.md#self-hosted-oauth-for-claudeai-and-other-mcp-clients) for the full setup and how to connect from Claude.ai.

### MCP client configuration

Point your MCP client (Cursor, Cline, Amp, etc.) to the server with the Authorization header:

```json
{
  "mcpServers": {
    "oduflow": {
      "type": "http",
      "url": "http://your-server:8000/mcp",
      "headers": {
        "Authorization": "Bearer my-secret-mcp-token"
      }
    }
  }
}
```

If the server is behind a reverse proxy with HTTPS (see [Traefik Routing](traefik.md)):

```json
{
  "mcpServers": {
    "oduflow": {
      "type": "http",
      "url": "https://oduflow.example.com/mcp",
      "headers": {
        "Authorization": "Bearer my-secret-mcp-token"
      }
    }
  }
}
```

### Web Dashboard

When running in HTTP mode, a web dashboard is available at the root URL (`http://your-server:8000/`). It provides environment management, service controls, a WebSocket terminal, and more. See [Web Dashboard & REST API](web-api.md) for details.

## Next steps

- **Set up a template** — `oduflow init-template` (see [Template Management](templates.md))
- **Customize configuration** — edit `oduflow.toml` (see [Configuration Reference](installation.md#configuration-reference))
- **Auto-start on boot** — `oduflow systemd-install` (see [systemd setup](installation.md#auto-start-with-systemd))
- **Multi-team isolation** — add multiple `[team.*]` sections (see [Multi-Team Support](multi-instance.md))
