# Quick Start

[TOC]

## 1. Configure

Create `oduflow.toml` (searched in `/etc/oduflow/` then `~/.oduflow/`, or set `ODUFLOW_TOML`):

```toml
[team.1]
hostname = "localhost"
```

## 2. Initialize the system

Create the shared Docker network, PostgreSQL container, and all team directories:

```bash
oduflow init
```

To set up a template database, use `oduflow init-template` (see [Template Management](templates.md)).

## 3. Start the MCP server

```bash
oduflow
```

The server starts on `http://0.0.0.0:8000` by default (configurable via `[server]` section in `oduflow.toml`).

## 4. Connect an MCP client

Point your MCP client (Cursor, Cline, Amp, etc.) to `http://<host>:8000/mcp`.
