# 0023 — Startup data migrations (Odoo-style upgrade steps)

**Status:** Adopted
**Type:** Architecture
**First introduced:** this change (2026-07-02), branch `litnimax/multi-tenant-hosting-design`
**Key code today:** `migrations.py` (`Migration`, `MIGRATIONS`, `run_pending`), `server.py` (hook in the server-start path, before `_ensure_initialized`)

## Context

Oduflow manages long-lived state outside its own process: Docker containers,
networks and volumes, per-team data directories, `ports.json`, Traefik dynamic
config. Until now every release had to be shape-compatible with whatever a
previous version left on the machine — there was no mechanism to change that
shape. The multi-tenant hardening work is the first to need one: moving to
team-scoped container names (`oduflow-{team}-{env}-{type}`) requires renaming
the containers of every existing environment, or they become visible but
inoperable "zombies" (listing is label-based, operations are name-based).

Ad-hoc "detect and fix at startup" code scattered per feature would run on
every start forever and accumulate. The prior art we already live with daily
is Odoo's own migration model: versioned one-shot scripts that run exactly
once, when an existing install upgrades past them — and never on a fresh
install.

## Decision

Add a **startup migrations subsystem** modeled on Odoo's:

- The code ships an ordered, **append-only registry** of one-shot steps
  (`MIGRATIONS` in `migrations.py`), each with a stable sequence id
  (`"0001-team-scoped-container-names"`) and an `apply(settings)` function.
- The data dir records applied ids in `base_data_dir/migrations.json`. On
  server start, before shared-infrastructure init, Oduflow diffs registry vs
  state and applies only the missing steps, oldest first.
- A **fresh install is stamped** as fully applied without running anything —
  current code lays down current-shape data, so historical steps have nothing
  to act on (exactly Odoo's fresh-install behavior). Freshness = no state file
  and no `team_*` directories yet.
- State is persisted **after each successful step** (atomic replace, under an
  flock), so a crash resumes at the failed step. A failing step aborts startup
  with a `PrerequisiteNotMetError` naming the migration — a half-migrated
  fleet must be noticed by the operator, not served.

## How it works (macro)

- `run_pending(settings)` is called once in the server-start path
  (`args.command is None`), *before* `_ensure_initialized`, so each step sees
  the data dir and Docker resources exactly as the previous version left them
  (and before init creates fresh per-team directories, which would defeat
  fresh-install detection).
- Steps run with full `Settings` and may touch anything Oduflow owns: rename
  containers, rewrite registries, regenerate Traefik files. They must be
  idempotent where possible, since a crashed step re-runs on the next start.
- CLI subcommands do not trigger migrations; only serving does. A machine that
  only ever runs one-off CLI commands keeps its state until the server next
  starts.

## Consequences

- Releases can now change the on-disk/Docker shape of existing deployments —
  the prerequisite for team-scoped resource naming, per-team networks, and
  the rest of the multi-tenant hardening plan.
- Upgrade cost at start is one JSON read once migrated; the registry is
  append-only, so entries are never re-run, reordered, or renamed.
- The first entry is `0001-team-scoped-container-names`: renaming every
  managed container to the team-scoped scheme, which fixes the cross-team
  branch-name collision the label-based listing masked.

## History

- Introduced together with the multi-tenant hosting design discussion
  ([[0014-team-based-multi-tenancy]] is the tenancy model this hardens).
