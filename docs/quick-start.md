# Quick Start

[TOC]

## 1. Configure

```bash
cp .env.example .env
# Edit .env — at minimum set paths and optionally ODUFLOW_AUTH_TOKEN
```

## 2. Initialize the system

Create the shared Docker network, PostgreSQL container, and Traefik reverse proxy:

```bash
oduflow init
```

To set up a template database, use `oduflow init-template` (see [Template Management](templates.md)).

## 3. Start the MCP server

```bash
oduflow
```

The server starts on `http://0.0.0.0:8000` by default (configurable via `ODUFLOW_HOST` / `ODUFLOW_PORT`).

## 4. Connect an MCP client

Point your MCP client (Cursor, Cline, Amp, etc.) to `http://<host>:8000/mcp`.

For stdio transport, set `ODUFLOW_TRANSPORT=stdio` and run `oduflow` as a subprocess.
