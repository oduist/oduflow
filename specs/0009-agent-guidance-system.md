# 0009 — Agent guidance system: editable MCP instructions + Odoo version dev guides

**Status:** Adopted (still in force)
**Type:** Architecture
**First introduced:** `d7016d7` "add Agents Guide — editable MCP instructions with UI tab and versioning" (2026-02-13)
**Key code today:** `server.py` (`get_agent_instructions`, `get_odoo_development_guide`, `_MCP_INSTRUCTIONS` bootstrap, `create_environment` hint), `web_ui.py` (`/api/agent-guides`), `templates/agent_guides/` (`agent_instructions.md`, `odoo_<N>_guide.md`)

## Context

Oduflow exposes a large, evolving MCP tool surface
([[0001-mcp-orchestrated-ephemeral-per-branch-environments]]) and a specific,
non-obvious **workflow**: provision per branch, push or live-mount code, call
`pull_and_apply`, read the cached errors, fix, retry. A connecting agent does
not know any of this from its priors. Worse, an agent's "knowledge" of Odoo is a
blend of versions and community folklore — it will happily write Odoo 12-era
patterns into an Odoo 19 module, or guess at the right apply action.

Two forces pushed toward shipping guidance *in-band*, from the server to the
agent, rather than relying on the agent's training:

1. **The workflow is Oduflow-specific and stateful.** The correct sequence,
   the meaning of each tool, and the *active code delivery mode*
   ([[0021-code-delivery-modes]]) are facts the server knows and the agent must
   be told — at connect time, not discovered by trial and error.
2. **Odoo conventions are version-specific.** What counts as correct (manifest
   keys, ORM idioms, view syntax, asset bundles) differs by major version. The
   server knows the target version (from the image/environment); it should hand
   the agent the matching rulebook before any code is written.

A connected agent is also expensive to re-prompt: fetching a long guide on every
call wastes context. And the operator running the deployment must be able to
**tune** what the agent is told without editing Python.

## Decision

Make the server **teach the connecting agent how to work**, through guidance
delivered over MCP and editable at runtime.

- **Server-level instructions.** The MCP server advertises a short
  bootstrap (`instructions=` on the FastMCP app) that tells the agent to fetch
  the full guide first, and exposes `get_agent_instructions` returning the
  Oduflow workflow guide — including a live-computed "Current Code Delivery
  Mode" preface so the agent uses the right (git vs live-mount) loop.
- **Per-version Odoo development guides.** `get_odoo_development_guide(version)`
  returns version-specific Odoo conventions and constraints. The version is
  normalized (`"18"` and `"18.0"` both work), and `create_environment` returns
  an explicit hint to fetch the matching guide *before* writing code.
- **Editable + versioned, surfaced in the dashboard.** Guides are Markdown files
  bundled with the package as defaults, copied per team into the data dir on
  init, and editable through the dashboard. Each guide carries a `Version:`
  marker so edits and re-pulls are traceable.
- **Self-caching instruction.** The agent guide tells the agent to save the
  document locally as a skill/instruction file so it need not re-fetch it over
  MCP on every session — guidance is pulled once, then cached client-side.

## How it works (macro)

- **Bootstrap → fetch → cache.** On connect the agent sees the short
  `_MCP_INSTRUCTIONS`, which directs it to call `get_agent_instructions` (and,
  per target version, `get_odoo_development_guide`). The returned guides instruct
  it to cache them locally, so steady-state operation costs no extra round-trips.
- **Layered resolution.** Each guide tool resolves the team's editable copy under
  the data dir first, then falls back to the bundled default shipped in the
  package — honoring the no-external-assets rule while letting operators override.
- **Bundled defaults, editable copies, versions.** `init` seeds the
  `agent_guides/` folder per team; the dashboard's REST API lists and serves the
  guides for in-place editing, and the `Version:` field increments so a guide's
  evolution is visible.

## Consequences

- The server becomes the **source of truth for how to use itself**: new tools or
  workflow changes are taught to every agent by editing one guide, not by hoping
  each agent's priors are current. This is why the bootstrap instruction is a
  hard convention — fetch guidance first.
- Version-specific guides materially raised code quality: the agent writes to the
  *actual* target Odoo version's conventions instead of an averaged prior, and
  the `create_environment` hint makes the fetch automatic rather than optional.
- Operators can tune agent behavior (house rules, conventions, gotchas) at
  runtime through the dashboard, without a code release — guidance is data, not
  Python.
- A small naming churn was the price of getting the concept right; the resolver
  reads legacy filenames so old data dirs keep working across renames.

## Evolution

The guidance concept is stable; the tool names converged over a few renames as
their role sharpened:

- A single editable "Agents Guide" (`get_agents_guide`) →
- a multi-guide system splitting the workflow guide from per-version Odoo dev
  guides (`get_agent_guide` + `get_odoo_development_guide`) →
- `get_agent_guide` → `get_agent_skill` (framing it as an agent *skill* to cache
  locally) →
- `get_agent_skill` → **`get_agent_instructions`** (the current name).

The on-disk files followed the same path (`agents_guide.md` → `agent_guide.md` →
`agent_skill.md` → `agent_instructions.md`); the resolver still accepts the
legacy names.

## History

- `d7016d7` (2026-02-13) — Agents Guide: bundled default, `get_agents_guide` MCP
  tool, GET/PUT REST endpoints with auto-incrementing `Version:`, dashboard tab.
- `8680a18` (2026-02-15) — refactor into multi-guide system: per-version Odoo dev
  guides + `get_odoo_development_guide(version)`; `create_environment` returns a
  hint to fetch the matching guide; dashboard becomes a guide list + viewer.
- `904f6f4` (2026-02-20) — `--update-guides` to refresh bundled guides; container
  rules added to the guide.
- `8625790` (2026-02-22) — `get_agent_guide` → `get_agent_skill`; add the
  **self-caching instruction** ("save this as a local skill, don't refetch").
- `fbbcf54` (2026-02-24) — `get_agent_skill` → `get_agent_instructions` (part of
  a broader tool-clarity rename); file renamed `agent_skill.md` →
  `agent_instructions.md`.
- `10dda86` (2026-06-15) — server-level MCP bootstrap `instructions` telling the
  agent to fetch the guide and the version dev guide up front.
