# 0029 — Per-team coding agent: browser console and ACP chat

**Status:** Adopted (MVP), ported from odusphere-cli (its ADRs 0005 + 0008)
**Type:** Architecture — new capability
**First introduced:** this change (2026-07-03), branch `litnimax/port-env-chat-agent`
**Key code today:** `docker/agent/` (Dockerfile + `entrypoint.sh` + `clone-env.sh`, image `oduist/oduflow-coder`); `[agent]` + per-team `agent_*` config in `settings.py`; `agent_config.py` (type validation) + `agent_sessions.py` (durable chat sessions); `_ensure_agent_container` / `_agent_add_env` / `ensure_agent_env_checkout` / `_agent_remove_env` in `docker_ops/env_ops.py`; `ws_agent_console` + `ws_agent_acp` + `api_agent_acp_*` + `api_agent_info` in `web_ui.py`; vendored `templates/static/acp-client.js` + `chat.js` + `marked.min.js`; `#agent-modal` / `#chat-modal` / `#chat-dock` and the card **Agent Chat** button in `templates/dashboard.html`

## Context

The dashboard already streams two `WebSocket ↔ docker exec` consoles per
environment (Odoo shell, psql) rendered with xterm.js. Odusphere-cli built on
that same bridge a full coding-agent surface: a per-installation agent
container (Claude Code + OpenAI Codex in one image) whose interactive CLI opens
in a browser terminal, plus a structured **ACP chat** (Agent Client Protocol,
JSON-RPC over stdio, bridged to the browser over the same WebSocket pattern)
with durable per-environment conversations. This change ports that capability.

The port is not a copy: odusphere is single-tenant (one Sphere, one operator),
while Oduflow is **multi-team with hard tenant isolation**
([[0027-hard-tenant-isolation]]) and has **no Sphere** — only ephemeral
environments. Everything Sphere-specific in the source (the `_sphere` control
checkout bind-mount, upstream-merge conflict resolution, Sphere Chat/CLI
buttons) was deliberately dropped. The agent is also framed differently: it is
a **hosting feature** — a client of a hosted Oduflow grows their Odoo by
chatting with the agent from the browser. A local developer already has the
code and their own agents, so the feature is opt-in per team and hidden for
live-mount environments.

## Decision

**One coding-agent container per team** (`oduflow-{team}-agent`), **opt-in and
off by default** (`agent_enabled` in the `[team.X]` TOML section), joined to
the team's isolated network, with two persistent named volumes — HOME (auth +
sessions) and `/workspace` (one full git checkout per environment at
`/workspace/<slug>`). The dashboard drives it two ways over the existing
WebSocket↔`docker exec` bridge:

- **Agent CLI** (`ws_agent_console`): the agent's own TUI in xterm.js, exec'd
  with a PTY at the environment's checkout.
- **Agent Chat** (`ws_agent_acp`): a TTY-less, line-framed relay to the agent's
  ACP adapter (`claude-code-acp` / `codex-acp`), rendered by a vendored,
  framework-free browser client (`acp-client.js` + `chat.js`). One durable
  session per (environment, agent) is persisted and resumed via ACP
  `session/load`; chats minimize to a dock so several run in parallel.

The agent never touches host files: it edits its own clone → `git push` → drives
the environment through the Oduflow MCP server (`pull_and_apply`, tests). MCP
access is **scoped and per session**: the team `auth_token` never enters the
agent container (a console is a root shell there — anything the container
holds, its user can read). Instead each console/chat exec injects that
environment's per-env token ([[0028-scoped-environment-mcp-access]]) plus its
`/mcp/<env>` URL into its own exec environment; Claude resolves them through
`${VAR}` placeholders in the checkout's `.mcp.json` (which therefore contains
no secret), Codex through per-session `-c mcp_servers.*` CLI overrides. A
leaked session credential grants only the ADR-0028 dev-loop allowlist on the
one environment the session already controls.

**Configuration lives in `oduflow.toml`, not in the dashboard.** Per team:
`agent_enabled` (default **false**), `agent_default` (claude | codex) and the
`[team.X.agent_env]` table (provider credentials + custom vars injected into
the container). The global `[agent]` section holds only deployment-wide bits
(image, optional model overrides). There is no runtime editing and no Agents
tab: config is the source of truth — the container carries a hash of its
injected config as a label and is recreated automatically on the next ensure
(server start, environment create, console open) when the config changed;
disabling the agent removes the container on the next server start. The only
runtime state is `agent_sessions.json` (durable ACP session ids) in
`<team.data_dir>`.

## How it works (macro)

- **Lifecycle:** `init_system` (every server start) ensures one agent
  container per enabled team and removes leftovers for disabled ones;
  `create_environment` execs `clone-env.sh` to add the env's checkout (and its
  project-scoped Claude `.mcp.json`); `delete_environment` removes the checkout
  and forgets its chat sessions; `destroy_system` removes the containers and
  volumes (they would otherwise pin the team networks). Opening a console/chat
  heals a missing checkout on demand from the Odoo container labels.
- **Chat open:** the card's **Agent Chat** button lazy-loads the vendored
  assets, fetches `agent-acp/info` (cwd + stored session id), connects the
  WebSocket, `initialize`s, then `session/load` (resume) or `session/new`
  (persisted back). `session/update` notifications render as markdown,
  collapsible reasoning, tool-call cards and plans; `session/request_permission`
  becomes an inline approve/deny panel.
- **Auth:** the modal and relay ride the dashboard session cookie; the team is
  resolved by the same auth middleware as every other dashboard route, so a
  team can only ever reach its own agent container.

## Consequences

- Two new first-class development surfaces with zero new transport machinery
  and no build step; the dashboard stays a single no-CDN page.
- A console or chat is **arbitrary code execution** in the team's agent
  container — confined to that container, its clones, and its session's
  scoped per-env MCP token; cross-team reach is blocked by per-team
  containers/volumes/networks. The agent cannot create/delete/stop
  environments or touch templates/services/volumes (default-deny allowlist of
  [[0028-scoped-environment-mcp-access]]); those stay operator actions.
- Environments created before per-env tokens existed have no
  `oduflow.mcp_token` label; their consoles warn and the agent works without
  MCP until the environment is updated/recreated. The Codex ACP adapter has no
  config-override channel yet, so Codex *chat* runs without Oduflow MCP for
  now (the Codex CLI console is fully wired) — consistent with the adapter's
  best-effort status below.
- Server-level provider keys (`ANTHROPIC_API_KEY` etc.) are inherited by the
  container **only in single-team deployments**; with several teams each team
  must set its own keys in the dashboard, so an operator credential never
  leaks to tenants.
- **Live-mount environments get no agent UI at all** (the card hides Agent
  Chat / Agent CLI): there is nothing for the containerized agent to clone,
  the host path is deliberately not mounted into it, and the local developer
  uses their own agents anyway.
- **The published image redistributes only Apache-2.0 software** (Codex CLI +
  Codex ACP adapter). Claude Code and its adapter are under Anthropic's
  Commercial Terms, so `entrypoint.sh` npm-installs them at first container
  start onto the persistent HOME volume — downloaded by the end user's
  container directly from npm, never shipped by us.
- Claude is the clean path (`loadSession: true` verified upstream); the Codex
  ACP adapter is best-effort, and the client self-heals a failed resume by
  starting fresh.

## History

- 2026-07-03 — ported from odusphere-cli ADRs 0005 (agent console; including
  its evolution to a single persistent container with HOME/workspace volumes)
  and 0008 (ACP chat UI), reshaped for multi-team Oduflow: per-team container/
  volumes/state, team-network placement, team-token MCP wiring, no Sphere
  surfaces. New image `oduist/oduflow-coder` published by
  `.github/workflows/publish-coder.yml`.
- 2026-07-03 — reframed as an opt-in hosting feature (review feedback): the
  Agents tab and runtime-editable stores were dropped in favour of static
  per-team TOML config (`agent_enabled` default false, `agent_default`,
  `[team.X.agent_env]`) with automatic container recreation on config drift;
  agent UI hidden for live-mount environments; Claude Code moved out of the
  published image to a first-start npm install (licensing).
- 2026-07-03 — the team `auth_token` was removed from the agent container
  entirely (review feedback: a console user could read it). Sessions now carry
  scoped per-environment tokens from [[0028-scoped-environment-mcp-access]] in
  their own exec env; `.mcp.json` holds a `${ODUFLOW_MCP_TOKEN}` placeholder
  instead of a secret, and the global Codex config holds no Oduflow endpoint.
