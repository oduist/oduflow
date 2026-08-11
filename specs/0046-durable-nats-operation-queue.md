# 0046 — Durable NATS operation queue for mutating work

**Status:** Adopted
**Type:** Architecture
**First introduced:** this change (2026-07-25)
**Key code today:** `operations.py`, `nats_runtime.py`, `server.py`

## Context

MCP transports and clients impose response timeouts, while Odoo installs,
upgrades, tests, template work, backups, and production deploys can legitimately
run for minutes. A timed-out request did not stop its synchronous worker thread,
so the process-local lock remained held and every later operation on that
resource reported busy even though the caller no longer had a result. REST,
webhooks, schedulers, and MCP also used different execution/locking paths.

## Decision

Run mutating work as durable operations through a local, Oduflow-managed
NATS/JetStream container and one embedded worker coordinator. NATS is
bootstrap infrastructure for one Oduflow server, not a distributed Oduflow
deployment contract. Use the official `nats-py` client directly.

Each operation receives a server-generated UUID. Callers may request
`wait=false` and receive the ticket immediately, or use `wait=true`: the
original result is returned when it finishes within the configured safe wait,
otherwise the same ticket is returned while work continues. Status, repeatable
wait, output, list, and cancellation are separate APIs.

Replace environment/team/system mutex categories with named resource keys.
Operations acquire their complete, sorted resource set before execution, so
conflicting work queues while unrelated environments, services, volumes,
templates, extra repositories, productions, and backup resources remain
parallel.

## How it works

- A JetStream work-queue stream stores commands; a KV bucket stores current
  operation state; Object Store holds large terminal results and complete tool
  output behind compact summaries. Arguments and secrets are encrypted at rest
  by the managed single-node JetStream store.
- States are `submitting`, `queued`, `running`, `cancel_requested`, and the
  terminal `succeeded`, `failed`, `cancelled`, `interrupted`. Arbitrary
  multi-step mutations are never automatically rerun after a crash. A tracked
  Docker exec can be reattached by exec id; otherwise a previously running job
  becomes `interrupted`.
- Queue scheduling is FIFO per resource, with bounded parallel workers for
  independent resource sets. Webhook deploys and scheduled/lifecycle work use
  coalescing keys and re-check destructive eligibility at execution time.
- Cancellation removes queued work immediately. Running tracked Docker execs
  receive TERM and then KILL for their process group; other multi-step work
  observes cancellation at safe checkpoints. Tracked exec output is written to
  a per-operation container file, making live reads and post-restart recovery
  possible even though Docker cannot reattach a second stdout stream.
- Completed metadata and output expire together after
  `[jobs].retention_seconds` (one hour by default). Queued/running work never
  expires. No ETA statistics or longer idempotency-history tombstones are kept.
- The first shutdown signal drains: new mutations are rejected, queued commands
  remain in JetStream, and only running jobs are awaited. A repeated signal or
  `oduflow drain --force` exits immediately.

## Consequences

Transport timeouts no longer own operation lifetime or resource locks. Every
entrypoint observes the same conflict model and operation state, and agents do
not generate UUIDs. The cost is a managed NATS container/volume and a
heterogeneous return type for mutating MCP tools: a completed result or an
operation ticket. Operators must treat NATS as critical local infrastructure;
health checks fail when it is unavailable.

This evolves [[0015-granular-locking]] from process-local fail-fast locks into
durable named-resource scheduling, and extends
[[0017-mcp-tool-execution-output-cache]] with restart-safe operation results.

## History

- Unreleased — introduced the managed JetStream runtime, durable operation
  APIs, named-resource scheduling, cancellation, drain, and dashboard operation
  view.
