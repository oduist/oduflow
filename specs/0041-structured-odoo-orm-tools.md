# 0041 — Structured Odoo ORM tools over the web JSON-RPC endpoint

**Status:** Adopted
**Type:** MCP capability
**First introduced:** this change (2026-08-02)
**Key code:** `docker_ops/odoo_rpc.py`; `server.py` (the six `odoo_*` tools);
`docker_ops/odoo_ops.py` (`connect_as_user`); `scoped_access.py`

## Context

Until now an agent had exactly one way to touch Odoo business data:
`run_odoo_shell`, which pipes arbitrary Python into `odoo shell`. As an escape
hatch it is excellent. As the *primary* data interface it is poor. The agent
must author code for every read, invent its own output format, and then parse a
stream in which its `print()` output is interleaved with the Odoo banner and log
lines. Domain nesting and field names are guessed, and guesses fail quietly —
an empty result set looks the same as a wrong domain. Every call also boots a
fresh Odoo registry, so an ordinary explore-then-change loop pays that cost
several times over.

What was missing is the interface every Odoo integrator already knows: the
`execute_kw` surface — `search_read`, `create`, `write`, `unlink`, an arbitrary
method call, and schema introspection.

A second gap mattered just as much. Answering "can this user see or change this
record?" is a routine development question, and neither existing path answers it
well: `run_odoo_shell` runs as superuser unless the agent hand-writes
`env(user=…)`, and real XML-RPC needs the target user's password, which Oduflow
environments deliberately do not have.

## Decision

Oduflow exposes six tools — `odoo_search_read`, `odoo_create`, `odoo_write`,
`odoo_unlink`, `odoo_call`, `odoo_schema` — with `execute_kw` semantics, and
every one of them runs **as a named Odoo user**.

They are implemented over Odoo's authenticated web JSON-RPC endpoint
`/web/dataset/call_kw`, requested over loopback from *inside* the environment's
Odoo container. They are deliberately **not** implemented over `/xmlrpc/2` or
`/jsonrpc`: those endpoints are deprecated in Odoo 19 and scheduled for removal
in 20, and each call logs a deprecation warning into the very environment log an
agent reads while debugging. `/web/dataset/call_kw`, the JSON-RPC envelope and
the error payload are unchanged across the versions Oduflow supports.

Authentication reuses the passwordless session minting already adopted for
"Connect as user" ([[0031-connect-as-user-impersonation]]). No password is
created, transmitted or required.

Mutations keep their own tool names rather than folding into the generic call.
`odoo_call` rejects `create`, `write` and `unlink`, so an operator or MCP client
can deny those named tools without the same mutation being buried in its
`method` string.

## How it works

- A one-shot Python helper is delivered into the container and executed there.
  Requesting `127.0.0.1:8069` makes the call independent of the routing mode,
  published ports, DNS and TLS, and identical on Linux and macOS. The same
  in-container HTTP pattern already backs the production health probe.
- The helper carries a live session id, so it is written root-owned and
  unreadable by the `odoo` user, deletes itself before issuing the request, and
  never places the id in process arguments or the exec environment — the rule
  the database password already follows.
- The response is framed by byte length rather than sentinels: ORM data can
  contain any string, including a sentinel.
- A session is minted once per (team, environment, user) and cached in the
  server process, well below Odoo's own session lifetime. A session rejected by
  Odoo — expired, rotated, or invalidated by a password change — is re-minted
  once and the call retried; a second rejection is an error that points at
  `run_odoo_shell`. Resetting the admin password drops the cache explicitly,
  because Odoo derives the session token from the password hash.
- Arguments arrive as JSON strings and are also accepted as Python literals,
  parsed without evaluation. A bare domain leaf is wrapped, so the most common
  nesting mistake stops being an error.
- Model discovery is paged with `limit` and `offset`; a full page explicitly
  warns that more models may exist rather than presenting a silent partial list.
- Odoo-side failures are **results, not exceptions**: the tool returns the
  exception name, message and server traceback. An `AccessError` for a portal
  user is the answer to the question that was asked, and the traceback is the
  most useful artefact these tools can hand an agent.

## Consequences

- Reading and changing Odoo data no longer requires writing Python, and the
  answer comes back as JSON rather than as a log stream to be scraped.
- Access rights and record rules become directly testable: run the same call as
  admin and as a portal user and compare.
- These tools talk to the **running** server, so edited Python code is invisible
  to them until the environment restarts, while `run_odoo_shell` sees it
  immediately. That difference is real and is documented in the tool docstrings
  and the agent guide; it is also the honest semantics of an RPC client.
- Each call is its own committed transaction. There is no dry run and no
  atomicity across calls; both remain reasons to use `run_odoo_shell`.
- No new privilege is granted. Anyone who can call these can already call
  `run_odoo_shell`, so they join the scoped-environment allowlist
  ([[0028-scoped-environment-mcp-access]]) on the same reasoning used for
  "Connect as user".
- Oduflow now depends on one more Odoo internal — the `call_kw` route contract —
  in addition to the session store it already depended on. Both are verified
  across the supported versions, and both degrade to an actionable error that
  names `run_odoo_shell` as the fallback.

## History

- 2026-08-02 — introduce the six `odoo_*` tools over in-container
  `/web/dataset/call_kw` with cached passwordless sessions; fix the environment
  base URL used by `http_request_to_odoo` and the readiness probe, which had been
  composing paths onto a URL that already ended in `/web?debug=1`.
