# Quick Start

[TOC]

## 1. Start the MCP server

```bash
oduflow
```

That's it. On first launch, Oduflow automatically:

- Creates a default `oduflow.toml` config (in `/etc/oduflow/` or `~/.oduflow/conf/`)
- Initializes shared infrastructure (Docker network, PostgreSQL, team directories)

By default, the server starts in **stdio** mode (for local MCP clients). For remote/multi-user deployments:

```bash
oduflow --transport http
```

The HTTP server starts on `http://0.0.0.0:8000` by default (configurable via `[server]` section in `oduflow.toml`).

To set up a template database, use `oduflow init-template` (see [Template Management](templates.md)).

## 2. Connect an MCP client

**stdio (local)** — add to your MCP client config (e.g. `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "oduflow": {
      "command": "oduflow"
    }
  }
}
```

**HTTP (remote)** — point your MCP client (Cursor, Cline, Amp, etc.) to `http://<host>:8000/mcp`.

## 3. (Optional) Customize configuration

Edit `oduflow.toml` to change settings:

```toml
[team.1]
hostname = "localhost"
```

See [Installation — Configuration Reference](installation.md#configuration-reference) for all options.
