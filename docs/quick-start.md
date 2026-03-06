# Quick Start

[TOC]

## 1. Configure

Create `oduflow.toml` (searched in `/etc/oduflow/` then `~/.oduflow/`, or set `ODUFLOW_TOML`):

```toml
[team.1]
hostname = "localhost"
```

## 2. Start the MCP server

```bash
oduflow
```

Oduflow automatically initializes shared infrastructure (Docker network, PostgreSQL, team directories) on first launch — no separate init step needed.

By default, the server starts in **stdio** mode (for local MCP clients). For remote/multi-user deployments:

```bash
oduflow --transport http
```

The HTTP server starts on `http://0.0.0.0:8000` by default (configurable via `[server]` section in `oduflow.toml`).

To set up a template database, use `oduflow init-template` (see [Template Management](templates.md)).

## 3. Connect an MCP client

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
