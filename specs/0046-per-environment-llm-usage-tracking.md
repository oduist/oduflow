# 0046 — Per-environment LLM usage tracking (hook-fed, capability-token ingest)

**Status:** Adopted (still in force)
**Type:** Architecture
**First introduced:** branch `litnimax/env-token-cost-stats` (2026-07-18)
**Key code today:** `usage.py` (persistence, aggregation, capability tokens), `web_ui.py` (`POST /api/llm-usage`), `server.py` (`get_claude_hooks`, usage in `get_environment_info`), `docker_ops/env_ops.py` (token mint on create, archive + revoke on delete, usage in `list_environments`), `templates/hooks/oduflow_usage_hook.py` (the Claude Code hook), `templates/dashboard.html` (card line + Usage modal)

**Note:** the ingest endpoint is `POST /api/llm-usage` (not `/api/usage`, which
[[0035-production-hosting]] already uses for team storage usage + quotas).

## Context

An agent does most of its work *against a specific environment* — Oduflow already
attributes one environment to one git branch
([[0001-mcp-orchestrated-ephemeral-per-branch-environments]]). A natural question
follows: how much LLM work did each environment cost — tokens, time, which models?

The blocking constraint is **where the truth lives**. An LLM cannot measure its
own token consumption mid-conversation; only the harness (for Claude Code: the
session transcript, `/cost`) holds the real numbers. So a "self-report" MCP tool
would collect hallucinated figures. The accurate source is *outside* the model,
in the coding client.

A second force is **identity and trust**. The reporter is an external process on
the developer's machine with no dashboard login, and one repository checkout moves
between many branches/environments over its life. Whatever ingests usage must
authenticate that process and bind each report to the right environment without
shipping a shared password or assuming a single environment per checkout.

We also deliberately scope *out* money: no pricing tables, no cost in dollars —
only tokens (input/output/cache), wall time, and model names. Pricing churns and
varies per contract; storing raw usage keeps the data durable and the system lean
([[0016-configuration-model]] philosophy: avoid config knobs that go stale).

## Decision

Record usage **per environment**, fed by a **Claude Code Stop hook** (not the
agent), ingested over a dedicated REST endpoint authenticated by an **opaque
per-environment capability token (UID)**.

- **No self-report tool.** The model never sends its own counts. A bundled hook
  reads the session transcript, sums tokens per model and wall-clock time, and
  POSTs them.
- **Capability-token auth, not dashboard login.** `create_environment` mints a
  random `usage_uid` and registers `uid → (team, env)` in a server-level index.
  The hook sends it in `X-Oduflow-Env-Uid`; the UID both *authenticates* the call
  and *identifies* the environment+team. `POST /api/llm-usage` needs no UI session
  (it is a public path that enforces the token itself). This reuses the
  "secret-as-capability" pattern of per-environment DB credentials
  ([[0013-per-environment-db-credentials-and-sanitization]]) and fits multi-tenancy
  ([[0014-team-based-multi-tenancy]]) without hostname/cookie plumbing.
- **One hook per repo, branch-routed.** The hook maps the *current git branch* to
  a UID via a local, git-ignored `.oduflow/usage-tokens.json`. Switching branches
  routes usage to the matching environment; a single installed hook serves all of
  a repo's environments.
- **Idempotent per session, accumulating across sessions.** Reports are keyed by
  `session_id`: re-firing a Stop hook *overwrites* that session (no double count),
  while distinct sessions sum. Per-session data keeps a per-model breakdown, since
  a model can change mid-session.
- **Live now, archived on delete.** While an environment lives, usage sits in
  `.usage.json` in its workspace (alongside `.note`). On delete — manual or by the
  reaper — totals fold into a per-team `usage.json` archive so accounting survives
  the ephemeral environment, and the token is revoked.
- **Delivery is self-service via MCP.** A `get_claude_hooks` tool (in the spirit of
  [[0009-agent-guidance-system]]) returns the hook script, the token-map entry
  (UID pre-filled), and the `.claude/settings.json` snippet, so the agent installs
  the hook itself.

## How it works (macro)

- **Three stores, one discipline.** Per-env live file (`{workspace}/.usage.json`),
  per-team archive (`{data_dir}/usage.json`), and a server-level token index
  (`{base_data_dir}/usage_tokens.json`) all use the same flock + atomic
  temp-rename writes as the port registry and activity tracker
  ([[0004-stable-addressing-port-registry-and-traefik]], [[0015-granular-locking]]).
  Writes are best-effort and never break the operation they ride on.
- **Aggregation is read-time.** Storage keeps raw per-session, per-model counters;
  totals and per-model rollups are computed on read, so the same data backs the
  dashboard card line, the Usage modal, and `get_environment_info`.
- **The dashboard reads, the hook writes.** `list_environments` embeds a `usage`
  summary; the dashboard shows a compact card line and a per-model breakdown modal
  ([[0005-web-dashboard-and-rest-api]], [[0022-engineers-console-design-system]]).
  The only write path is the hook → `POST /api/llm-usage`.

## Consequences

- Usage figures are **real**, because they come from the transcript, not the
  model's guess — at the cost of requiring the dashboard (HTTP mode) reachable
  and a one-time hook install per repo.
- The capability token makes the ingest endpoint **safe to expose without a login**
  and keeps the hook trivial (one URL + one header), while scoping each token to a
  single environment limits blast radius if one leaks.
- Tracking **survives environment churn** via the per-team archive, making the data
  useful for longer-term accounting rather than vanishing with each ephemeral env.
- Choosing tokens-only (no money) keeps the system durable and lean; cost can be
  derived downstream from raw usage if ever needed, without Oduflow owning a
  pricing table.
- The hook is delivery-client-specific (Claude Code transcript + Stop event).
  Other clients would need their own reporter, but the ingest contract
  (`POST /api/llm-usage` with `X-Oduflow-Env-Uid`) is generic.

## History

- branch `litnimax/env-token-cost-stats` (2026-06-17) — `usage.py` storage +
  capability-token index; `POST /api/llm-usage`; `get_claude_hooks` MCP tool and
  bundled Stop hook; token mint on create, archive + revoke on delete; usage in
  `list_environments`/`get_environment_info`; dashboard card line + Usage modal;
  `docs/usage-tracking.md`.
