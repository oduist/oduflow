# 0001 — MCP-orchestrated ephemeral per-branch Odoo environments

**Status:** Adopted (foundational, still in force)
**Type:** Architecture
**First introduced:** `7d9dc70` "Initial concept" (2026-02-05)
**Key code today:** `server.py` (FastMCP entry point + tools), `docker_ops/` (Docker SDK orchestration), `naming.py` (branch → resource names)

## Context

The developer works in a **branch-per-task** workflow and wants an AI coding
agent to be able to spin up a *fully isolated* Odoo instance for any branch,
exercise the code (install/upgrade modules, run tests, read logs), and tear it
down — without a human clicking through a UI. The agent needs a programmatic,
typed interface it can call directly from its tool-use loop.

Forces at play:
- Odoo environments are heavy (Odoo + PostgreSQL + filestore) and stateful.
- An agent must not corrupt one branch's state by acting on another.
- The interface must be machine-first: callable tools, structured results, no
  hidden global "current environment".

## Decision

Build a single **MCP server on the FastMCP framework** that exposes Odoo
environment lifecycle as a set of `@mcp.tool()` functions, and orchestrates
**Docker** containers directly via the Docker SDK. The **git branch name is the
primary key** for an environment: it derives the container/network names, the
database name, and the host paths. One branch ⇒ one isolated, disposable
environment.

The seed toolset was `provision_env`, `teardown_env`, `execute_test`,
`list_envs`; this grew into today's create/delete/install/upgrade/test/logs/exec
surface, but the shape — *one tool call per lifecycle action, scoped by branch* —
is unchanged.

## How it works (macro)

- **FastMCP entry point.** The server registers tools and is launched as one
  process. The agent connects over MCP and calls tools by name.
- **Branch-keyed isolation.** Every Docker resource is named and labelled from a
  slugified branch name (`naming.py`), so listing/teardown can reliably find
  exactly the resources belonging to one branch and nothing else.
- **Docker as the runtime.** Official Odoo + PostgreSQL images are composed per
  environment; the server talks to the Docker daemon rather than to a hosted
  Odoo control plane. Provisioning returns a URL the agent (or human) can open.
- **Ephemeral by design.** Environments are cheap to recreate and meant to be
  torn down at end of task; persistence lives in templates and the per-branch DB,
  not in long-lived snowflake servers.

## Consequences

- Established the project's identity: an **AI-first, MCP-native** Odoo dev/CI
  tool, not a web app with an API bolted on. Every later capability is added as
  another MCP tool plus its orchestration logic.
- Branch-as-key made parallel, conflict-free work across branches natural — this
  later motivated **granular per-branch locking** rather than a global mutex.
- Direct Docker orchestration (vs. docker-compose/k8s) kept the dependency
  surface small and the control flow explicit, at the cost of reimplementing
  some compose-like wiring (networks, port mapping, volumes) in `docker_ops/`.
- The "one process exposing tools" model set up later decisions about
  **transport** (stdio vs HTTP) and **tenancy** (see [[0002-remote-multi-user-mcp-access]]).

## History

- `7d9dc70` (2026-02-05) — initial concept: FastMCP server + `odoo_manager.py`,
  branch-scoped provision/teardown/test/list, Docker labels for tracking.
- `d6918c6` (2026-02-10) — project renamed `flow` → `oduflow`; package and
  resource prefixes updated, architecture unchanged.
- `9a4428c`, later `docker_ops/` split — `odoo_manager.py` decomposed into
  `client/system_ops/env_ops/odoo_ops/...` as the surface grew, preserving the
  same per-branch orchestration model.
