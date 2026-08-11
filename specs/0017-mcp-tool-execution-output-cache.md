# 0017 — MCP tool execution model: server-side output cache + `read_output` for long operations

**Status:** Adopted (still in force)
**Type:** Architecture
**First introduced:** `44810aa` "implement MCP tools refinement spec — output cache, 7 new tools, 3 enhancements" (2026-03-02)
**Key code today:** `output_cache.py` (`OutputCache`, `CachedOutput`), `server.py` (`_make_summary`, `_maybe_cache`, `read_output`, integration in install/upgrade/test/pull_and_apply/run_odoo_command/run_db_query)

## Context

The founding design ([[0001-mcp-orchestrated-ephemeral-per-branch-environments]])
gives the agent a closed feedback loop: install/upgrade a module, run tests, read
the errors, fix, retry. The value of that loop is entirely in the agent *seeing
the output*. But Odoo's install/upgrade/test runs emit tens of thousands of lines
of INFO logs — a typical `upgrade_odoo_modules` is 68K+ characters — and **MCP
responses are bounded**. When a tool result exceeds the client's limit, clients
like Claude Code spill it to a file the agent never reads:

```
upgrade_odoo_modules (...) ⎿ Error: result (68,449 characters) exceeds maximum allowed tokens.
   Output has been saved to .../tool-results/mcp-....txt
```

At that point the agent has **lost the feedback** — it cannot tell whether the
run succeeded or what failed. This was a blocking problem for the core workflow,
not a polish item: the very tools that produce the most important diagnostics
(`install_odoo_modules`, `upgrade_odoo_modules`, `run_odoo_tests`,
`pull_and_apply`, arbitrary `run_odoo_command`, unbounded `run_db_query`) are the
ones most likely to overflow.

A naive "just truncate" loses exactly the error buried in the middle of the log;
a "return everything" overflows. The agent needs a *small* response that always
contains the errors, plus a way to *page into* the full output on demand.

## Decision

Introduce a **server-side output cache** plus a retrieval tool, so any tool with
potentially large output returns a bounded smart summary while the full output
stays addressable on the server. This refinement was specified up front
(`mcp-ref.md`) and then implemented.

- **Cache full output, return a summary.** Outputs above a threshold
  (5K chars) are stored in an in-memory `OutputCache` keyed by a short
  `output_id`. The tool returns a **smart summary** — head + every error/warning
  with context + tail + metadata (total lines/chars, error count, the
  `output_id`) — which fits in one MCP response and always surfaces the failure.
  Smaller outputs pass through unchanged, so the change is transparent and
  backward-compatible.
- **`read_output` for interactive drill-down.** A new tool pages into the cached
  buffer by `output_id`: line ranges, `errors`-only with context, case-insensitive
  `grep`, `tail`, and `info` (metadata). The big log effectively becomes a
  `less`-with-search buffer the agent can explore without re-running anything.
- **Bounded, ephemeral cache.** In-memory only, ~1h TTL, capped entries with
  oldest-out eviction, error lines pre-indexed at store time. Yesterday's upgrade
  log is worthless; the cache exists only to bridge one MCP response limit, not to
  be durable storage.
- **Tighten the surrounding tool surface.** The same refinement renamed
  `exec_in_odoo` → `run_odoo_command` (pairing with the new `run_odoo_shell`),
  added a `max_rows` guard to `run_db_query`, added `grep`/`level` filtering to
  log retrieval, and shipped the other tools the spec called for (file write,
  ORM shell, HTTP-to-Odoo, module listing, in-container search, readiness waits).

## How it works (macro)

- **Transparent caching at the tool boundary.** Tools that can produce large
  output route their result through one helper: under threshold, return as-is;
  over threshold, store in the cache and return `header + smart summary`. The
  agent needs no new parameter — caching is invisible until the output is big.
- **Two tiers of consumption.** A summary-only agent still sees head, the
  extracted errors with context, and tail — enough for the common case. A
  drill-down-capable agent uses `read_output(output_id, mode=...)` to grep, read
  ranges, or fetch errors with more context.
- **Self-describing responses.** Every summary footer states the `output_id` and
  exactly how to call `read_output`, so the retrieval path is discoverable from
  the response itself rather than from out-of-band docs.

## Consequences

- The **core feedback loop survives long operations**: the agent reliably sees
  whether an install/upgrade/test failed and why, even when the raw log is far
  larger than any MCP response could hold — without spilling to an unread file.
- The summary's "head + errors + tail" shape encodes a debugging heuristic into
  the protocol layer: the response is short *and* the signal (errors,
  tracebacks) is never the thing that gets truncated away.
- Keeping the cache in-memory/TTL'd avoided a persistence/cleanup subsystem; the
  cost is that outputs vanish on restart or after an hour, which matches their
  short useful life.
- This refinement also rationalized tool **naming and scope** (notably
  `exec_in_odoo` → `run_odoo_command`), and the threshold was tuned down from
  15K to 5K during the spec phase so the safety net engages well before any
  client limit.

## Evolution

[[0046-durable-nats-operation-queue]] keeps the compact summary and
`read_output` workflow, makes the one-hour lifetime configurable through
`[jobs].retention_seconds`, and adds durable operation-result and full-output
storage so a transport timeout or server restart does not erase the terminal
job result or its complete log.

## History

- `87edd27` (2026-03-02) — author the MCP tools refinement spec (`mcp-ref.md`):
  output cache as the P0 blocking fix, plus new tools and enhancements.
- `417eba7` (2026-03-02) — refine the spec: `exec_in_odoo` → `run_odoo_command`,
  lower the cache threshold 15K → 5K, drop deferred snapshot tools.
- `44810aa` (2026-03-02) — implement it: `output_cache.py`, `_make_summary`,
  `_maybe_cache`, `read_output`; caching wired into 6 existing tools; `max_rows`
  on `run_db_query`; plus the new core tools and log-filter enhancements.
