# 0050 — Resource-scoped locks: the team lock stops being the catch-all

**Status:** Adopted
**Type:** Architecture
**First introduced:** 2026-08-17
**Key code today:** `locking.py` (resource key builders, `keyed_mutex`, `acquire_system`), `server.py` (`with_key_lock`), `web_ui.py`, `docker_ops/service_ops.py`, `docker_ops/volume_ops.py`

## Context

[[0015-granular-locking]] replaced a global mutex with per-branch, per-team and
system locks — and per-team became the default for everything that was not
obviously one environment: services, volumes, extra-addon repos, credentials,
service presets, backup pruning, even *listing* templates.

That scope is far wider than what those operations touch. Because the lock
manager also enforces team↔environment mutual exclusion (a team-wide operation
must not run while any of the team's environments is busy, and vice versa), a
ten-minute `create_environment` made `delete_service`, `create_volume`,
`add_extra_repo` and `list_templates` all fail with `BusyError` — and a service
tweak could reject an environment build. Agents read those rejections as stuck
state and reach for restarts. Meanwhile the coarse lock was not even buying
correctness where it mattered: `restart_service` and `run_service_command` took
no lock at all, and backup pruning could run in the middle of a snapshot,
because productions lock in their own `prod:` keyspace that the team lock never
touched. The one genuine team-wide invariant — publishing a template remounts
other environments' overlay filestores — was hidden among a dozen operations
that had nothing team-wide about them.

## Decision

**Lock the resource, not the tenant.** Every operation takes the narrowest key
that names what it actually mutates, using the lock manager's existing generic
keyed lock:

- `svc:{team}:{name}`, `vol:{team}:{name}`, `preset:{team}`, `creds:{team}`,
  `prod-backups:{team}` — acquired *without* a team id, so they sit outside the
  team↔environment mutex entirely.
- The **team lock is reserved for the one real team×environments invariant**:
  template mutations that remount live environments' filestores.
- Operations that another mechanism already serialises take **no** lock:
  extra-addon repos (per-repo RLock plus a container-based dependency guard in
  `extra_addons.py`), pure reads, and the `odoo_*` XML-RPC tools — PostgreSQL,
  not Oduflow, arbitrates concurrent ORM calls, exactly as it already did for
  the neighbouring lock-free `http_request_to_odoo`.
- The **system lock**, dead since [[0015-granular-locking]], is revived for
  `restore_cluster_pitr` and made mutually exclusive with the whole `prod:`
  keyspace in both directions — the honest expression of "this rewrites the
  cluster every team's productions live in".

## How it works (macro)

- One module builds every key string, because the MCP tools and the REST
  dashboard must lock the same resource under the same key or they stop seeing
  each other. A `with_key_lock(key_builder)` decorator applies it to tools; the
  dashboard calls the same builders.
- Below the tool layer, two invariants span calls the tool layer cannot see: the
  service-slot count, and "no service mounts this volume". These use a short,
  blocking, re-entrant `keyed_mutex` inside `docker_ops` rather than a
  user-visible lock — it must be held across `update_service`'s remove-and-
  recreate window, and a caller has nothing useful to do but wait for
  milliseconds. Ordering is fixed (tool key first, then the mutex; nothing under
  the mutex takes a tool key), so the two schemes cannot deadlock.
- Lock-free tools that wake a stopped environment made an existing
  check-then-start race reachable; the wake is now atomic per environment
  through the same keyed mutex.
- Contention still surfaces as `BusyError` naming the holding operation and its
  age — the diagnostic contract agents depend on is unchanged, only its blast
  radius shrank.

## Consequences

- Work on unrelated resources genuinely runs in parallel: a long environment
  build no longer bounces service, volume, credential or repo operations, and
  vice versa. Locks now fail only when two callers really do want the same
  thing.
- Races that the coarse lock never actually covered are closed: service restart
  and exec now exclude delete/update, prune excludes snapshot and restore, and
  cluster PITR excludes every production operation including the blocking
  webhook-deploy path.
- **Accepted window:** `create_environment` materialises shared extra-addon
  checkouts *before* its container exists, so the delete guard cannot see it. A
  `delete_extra_repo` landing exactly there makes the in-flight create fail with
  a clear error instead of every repo operation queueing behind a team lock for
  minutes. Documented at the guard.
- The team lock now means something specific, so the "which scope?" question for
  a new tool has a sharper answer — but the answer is no longer "team by
  default", and mis-scoping is still the standing risk of granular locking.
- Locks remain in-process; cross-process safety on shared files stays with the
  registries' own flocks.

## History

- `3a26c70` (2026-02-06) — global mutex.
- `ad3b382` (2026-03-01) — `LockManager` with per-branch/per-team/system locks
  ([[0015-granular-locking]]).
- 2026-08-17 — resource-scoped keys, template-only team lock, lock-free `odoo_*`
  and extra-repo tools, revived system lock for `restore_cluster_pitr`.
