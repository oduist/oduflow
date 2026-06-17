# 0002 — Remote, multi-user MCP access over streamable HTTP

**Status:** Adopted; tenancy model later superseded (see Evolution)
**Type:** Architecture
**First introduced:** `37ebad3` "Multi user invironment" (2026-02-05), `150b481` "sse -> http" (2026-02-05)
**Key code today:** `server.py` (transport selection, HTTP app), `web_ui.py`, `settings.py`, locking/tenancy modules

## Context

The founding design ([[0001-mcp-orchestrated-ephemeral-per-branch-environments]])
ran the server over **stdio** for a single local agent. Two needs surfaced
immediately:

1. **Remote access** — the server must be reachable by an agent that is not the
   local process ("It must be started so that I can connect remotely also").
2. **Multiple users sharing one server** — several people (each with their own
   agent) should share one Oduflow deployment without seeing or destroying each
   other's environments, and should be able to use the *same branch name*
   without colliding.

A hard constraint was set on the interface: **authentication must not appear as
a tool parameter**. Tools stay clean (`provision_env(branch_name)`), and identity
is carried out-of-band in the transport, not in the agent-visible signature.

## Decision

Serve the MCP server over a **network transport** in addition to stdio, and
**scope every environment to the calling identity** taken from the request
context rather than from a tool argument.

- **Transport:** start with SSE, then move to **streamable HTTP**
  (`150b481`) as the remote transport; stdio remains the default for the
  single-local-agent case. The HTTP app is what makes remote and multi-user use
  possible at all.
- **Identity from headers:** the caller's token is read from HTTP headers
  (`get_http_headers()`), never from tool parameters. It is hashed into a short,
  non-reversible `user_id` so the raw token never lands in Docker labels,
  resource names, or logs.
- **Identity-scoped resources:** the identity is stamped onto every Docker
  resource and host path. List/teardown/test/install operations are filtered to
  the caller's identity, giving each tenant an isolated namespace where branch
  names are unique *per tenant*.

## How it works (macro)

- One server process can run in **stdio mode** (local, implicit single user) or
  **HTTP mode** (remote, many users). The transport is selected at startup.
- In HTTP mode, the per-request identity is resolved from headers and threaded
  through to the orchestration layer, which labels/paths all resources with it.
- Tool signatures never carry auth; the agent calls the same tools regardless of
  who is connected.

## Consequences

- Cemented Oduflow as a potentially **multi-tenant, remotely hosted** service,
  not just a localhost helper — enabling the later Web dashboard, hosted
  deployments, and authentication work.
- The "identity in transport, never in tool args" rule became a durable design
  invariant carried into the OAuth work.
- Per-tenant namespacing made **per-branch concurrency** safe across users and
  fed directly into the granular locking design.

## Evolution

The *transport* decision (HTTP for remote, stdio default) is still in force. The
*tenancy* mechanism changed shape twice:

- `0a8ceab` (2026-02-12) — **multi-instance** isolation: separate Oduflow
  instances, each with its own data dir / routing, instead of hashing a per-user
  token into shared resources.
- `ad3b382` / `14503a0` (2026-03-01) — **team-based multi-tenancy** replaced
  instance-based isolation: tenancy is modelled as `[team.*]` sections in
  `oduflow.toml`, with per-team locks and per-team resource scoping. This is the
  current model and warrants its own decision record.
- `97c3fc8`, `5f32f58` — auth hardened from a raw header token to **GitHub OAuth**
  and then a **self-hosted OAuth Authorization Server** for HTTP transport
  (separate decision record).

## History

- `37ebad3` (2026-02-05) — multi-user: token→`user_id` hash, resources/paths/labels
  scoped per user, scoped `list/teardown/test/provision`, no auth in tool args.
- `150b481` (2026-02-05) — switched remote transport SSE → streamable HTTP.
- `81457b1` / `ed4a265` — `stateless_http=True` and running sync MCP tools in a
  thread pool to unblock concurrent HTTP requests.
