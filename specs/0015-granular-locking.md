# 0015 — Granular locking: per-branch / per-team / system locks

**Status:** Adopted (still in force)
**Type:** Architecture
**First introduced:** global mutex `3a26c70` "Load filestore" (2026-02-06); replaced by the granular `LockManager` in `ad3b382` "team-based multi-tenancy" (2026-03-01)
**Key code today:** `locking.py` (`LockManager`, `BusyError`), `server.py` (`with_env_lock` / `with_team_lock` decorators, `acquire_system`)

## Context

Oduflow mutates heavy, stateful resources — Docker containers, PostgreSQL
databases, filestores, a shared port registry. Two concurrent operations that
touch the *same* environment (e.g. an install and a rebuild on the same branch)
can corrupt each other, so mutating tools must be serialized.

The first cut took the blunt approach: a **single process-wide mutex**
(`_busy = threading.Lock()`, applied via a `with_mutex` decorator) guarded
*every* mutating tool. If any operation was running, a second tool call failed
fast with `BusyError` rather than queuing or racing.

That was correct but far too coarse. The whole point of the founding design
([[0001-mcp-orchestrated-ephemeral-per-branch-environments]]) is that the branch
name is the unit of isolation and work on different branches is independent — and
once the server became multi-user and then multi-team
([[0002-remote-multi-user-mcp-access]]), a global lock meant **one tenant's slow
operation blocked everyone else's**. Installing modules on branch A should never
make `create_environment` on branch B (let alone another team's branch) bounce
with "busy". A global mutex threw away exactly the parallelism the architecture
was built to exploit.

## Decision

Replace the single global mutex with a **`LockManager` that locks at the finest
safe granularity**, choosing the lock scope to match what an operation actually
touches:

- **Per-environment (per-branch) lock** — for operations scoped to one
  environment (install/upgrade/test/restart/exec/rebuild/delete). Two operations
  on the *same* branch serialize; operations on *different* branches run fully in
  parallel.
- **Per-team lock** — for operations that touch team-wide shared state rather
  than a single environment (e.g. credentials, extra-addon repos, template and
  service-preset management). Teams never block each other.
- **System lock** — a single lock for truly global operations (init / destroy of
  the shared infrastructure) that must exclude everything else.

Contention is still surfaced to the agent as a fast-failing **`BusyError`**
(non-blocking `acquire`): the caller is told another operation on that specific
resource is in progress and to retry, instead of the request hanging.

## How it works (macro)

- `LockManager` holds lazily-created `threading.Lock` objects keyed by
  environment name and by team id, plus one system lock; a small map-guard lock
  protects the dictionaries themselves.
- Tools opt into a scope by decorator in `server.py`: `with_env_lock` derives the
  branch/env name from the call and takes that environment's lock;
  `with_team_lock` resolves the caller's team and takes that team's lock;
  global lifecycle paths take the system lock directly.
- Acquisition is **non-blocking** — a busy lock raises `BusyError` immediately,
  which `@handle_errors` turns into a clean MCP `ToolError`. The agent retries
  rather than blocking a worker thread.
- Because sync tools already run in a thread pool for HTTP concurrency
  (see [[0002-remote-multi-user-mcp-access]]), independent locks genuinely run in
  parallel across threads.

## Consequences

- Parallel, conflict-free work across branches and across teams became the
  default, not the exception — the locking granularity finally matches the
  branch-as-key and team-as-tenant models.
- A slow or stuck operation only blocks its own environment (or its own team),
  containing the blast radius of contention to the smallest meaningful unit.
- Each new mutating tool must pick the *right* scope; mis-scoping (env lock where
  a team-shared resource is touched, or vice versa) reintroduces races or
  over-serialization. The `BusyError` retry contract keeps the failure mode
  uniform and agent-friendly regardless of scope.
- Locks are in-process only: they serialize within one Oduflow process, while
  cross-process safety on shared files (e.g. the port registry) is handled
  separately with an flock (see [[0004-stable-addressing-port-registry-and-traefik]]).

## Evolution

- **2026-08-17 — the team lock stops being the catch-all**
  ([[0049-granular-resource-locks]]). The three scopes proved right; the *choice*
  of scope did not. Per-team had become the default for anything that was not one
  environment — services, volumes, extra repos, credentials, presets, backup
  pruning, template listing — which, through the team↔environment mutual
  exclusion, let a long `create_environment` reject unrelated service and volume
  work. Those operations moved to narrow resource keys on the same generic keyed
  lock, the team lock was reserved for template mutations (the one operation that
  really does touch other environments), and the system lock — dead since this
  record was written — was revived for cluster PITR, mutually exclusive with the
  whole production keyspace.

## History

- `3a26c70` (2026-02-06) — global mutex introduced: `_busy = threading.Lock()` +
  `with_mutex` decorator guarding all mutating tools, failing fast with
  `BusyError`.
- `ad3b382` (2026-03-01) — granular `LockManager` added (`locking.py`) as part of
  the team-tenancy refactor; the global `with_mutex` is replaced by per-branch
  (`with_env_lock`) and per-team (`with_team_lock`) decorators plus a system lock.
- `18b18b1` (2026-03-02) — documentation and tool tables renamed "Mutex" →
  "Lock" / "Locking" to reflect the per-branch/per-team model; fixed a missing
  lock indicator on `delete_service_preset`.
