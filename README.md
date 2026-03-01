<p align="center">
  <a href="https://github.com/oduist/oduflow/actions/workflows/tests.yml"><img src="https://github.com/oduist/oduflow/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <img src="https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/Docker-Required-2496ED?logo=docker&logoColor=white" alt="Docker">
  <img src="https://img.shields.io/badge/Protocol-MCP-green" alt="MCP">
  <img src="https://img.shields.io/badge/License-Polyform%20NC-yellow" alt="Polyform Noncommercial License">
  <img src="https://img.shields.io/badge/Odoo-15.0--19.0-714B67?logo=odoo&logoColor=white" alt="Odoo">
</p>

# Oduflow

An **AI-first** Odoo development and CI tool, powered by **reusable database templates**. Oduflow provisions isolated, ephemeral Odoo environments on Docker — one per git branch — and exposes them to AI coding agents via [MCP](https://modelcontextprotocol.io/), creating a **closed feedback loop** that enables fully autonomous Odoo development.

### Beyond Vibe Coding: Spec-Driven Development

**Vibe coding** — chatting with an AI and eyeballing the output — was the first wave. It works for prototypes, but breaks down on real ERP systems where a module must install cleanly, pass tests, and work against production data.

**Spec-Driven Development (SDD)** is the next step: you write a precise specification of *what* the module should do, and the AI agent autonomously implements *how* — because it has a **closed feedback loop** with the running system:

```
┌─────────────────────────────────────────────────────┐
│                    AI Agent                          │
│          (Cursor, Cline, Amp, Claude, …)             │
└──────┬──────────────────────────────▲────────────────┘
       │ 1. Read spec                 │ 5. Read errors,
       │ 2. Write code                │    fix code,
       │ 3. Install module via MCP    │    retry
       │ 4. Click-test UI via         │
       │    Playwright MCP            │
┌──────▼──────────────────────────────┴────────────────┐
│               Oduflow (MCP Server)                    │
│  • install_odoo_modules → traceback or success        │
│  • run_odoo_tests → test pass/fail with details       │
│  • get_environment_logs → runtime errors              │
│  • upgrade_odoo_modules → upgrade output              │
├──────────────────────────────────────────────────────┤
│            + Playwright MCP / other tools              │
│  • Navigate Odoo UI, click buttons, fill forms        │
│  • Verify business logic end-to-end                   │
│  • Validate acceptance criteria from the spec         │
└──────────────────────────────────────────────────────┘
```

The agent writes code, installs the module, reads the traceback, fixes the error, retries — and when it installs cleanly, it can open the browser via [Playwright MCP](https://github.com/anthropics/mcp-playwright) to click through the UI, verify business flows, and validate acceptance criteria — **all without human intervention**.

| | Vibe Coding | Spec-Driven Development |
|---|---|---|
| **Input** | Conversational prompts | Formal specification with acceptance criteria |
| **Feedback** | Human eyeballs the code | System returns errors, test results, and UI state automatically |
| **Iteration** | Human copy-pastes errors back | Agent retries autonomously via MCP |
| **Scope** | Single files, prototypes | Full modules against real databases |
| **Verification** | "Looks right" | Module installs, tests pass, UI works on production data |

---

## Quick Start

### 1. Install

```bash
pip install oduflow
```

### 2. Configure

Create `oduflow.toml` (see [Installation](docs/installation.md) for all options):

```toml
[team.1]
hostname = "localhost"
```

### 3. Initialize the system

```bash
oduflow init
```

### 4. Start the MCP server

```bash
oduflow
```

The server starts on `http://0.0.0.0:8000` by default.

### 5. Connect an MCP client

Point your MCP client (Cursor, Cline, Amp, etc.) to `http://<host>:8000/mcp`.

---

## Documentation

For full documentation, visit **[oduflow.dev](https://oduflow.dev)** or see the [`docs/`](docs/) folder:

- [Installation & Configuration](docs/installation.md)
- [Use Cases & Workflows](docs/use-cases.md)
- [Template Management](docs/templates.md)
- [Environment Management](docs/environments.md)
- [Auxiliary Services](docs/services.md)
- [Extra Addons Repositories](docs/extra-addons.md)
- [Web Dashboard & REST API](docs/web-api.md)
- [MCP Tools Reference](docs/mcp-tools.md)
- [CLI Reference](docs/cli.md)
- [Traefik Routing (Auto-HTTPS)](docs/traefik.md)
- [Multi-Instance Support](docs/multi-instance.md)
- [Authentication & Security](docs/security.md)
- [Running in Docker](docs/docker.md)
- [Internals](docs/internals.md)
- [Licensing](docs/licensing.md)

---

## Licensing

Oduflow is source-available under the [Polyform Noncommercial License 1.0.0](LICENSE). Commercial use requires a license — visit [oduflow.dev](https://oduflow.dev).
