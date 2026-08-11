# 0044 — Unified host resource planning for PostgreSQL and production Odoo

**Status:** Adopted (v1)
**Type:** Architecture — runtime resource model
**First introduced:** this change (2026-08-02), branch `litnimax/postgresql-config-upgrades`
**Key code today:** `resource_plan.py`; `pg_tune.py`; `prod_tune.py`; `_run_retune_postgres` in `server.py`; production config generation in `docker_ops/system_ops.py` and `docker_ops/production_ops.py`

## Context

The dev PostgreSQL profile, production PostgreSQL profile, and production Odoo
worker profile were introduced at different times. Each detected the complete
host CPU/RAM and sized itself independently. The profiles were informally
co-designed — most importantly, dev `shared_buffers` was capped at 1 GB — but
there was no executable host-wide budget and no way to verify their combined
assumptions. On small hosts, enabling [[0035-production-hosting]] could make
multiple services behave as though each owned the server.

The generated files were intentionally protected by `# KEEP`. That prevented
package upgrades from destroying operator changes, but also meant adding RAM,
changing CPU allocation, or enabling production silently left the old tuning
in place. Automatically rewriting live PostgreSQL configs is unsafe: some
settings require a restart, a file may contain intentional edits, and a
package upgrade must not introduce an unplanned database restart.

## Decision

**All Oduflow resource-tuned services consume one deterministic host resource
plan.** The planner takes detected CPU/RAM plus the declarative
`[production].enabled` flag. Production capacity is reserved when the feature
is enabled, even before the first production is created; creating a production
must not unexpectedly resize the already-running dev tier.

The plan is advisory rather than a Docker reservation. In production mode it
targets 20% of RAM for the OS and other services, 45% for production Odoo
worker sizing, and 25% for PostgreSQL shared buffers (5% dev + 20% production),
subject to conservative floors and caps. PostgreSQL CPU concurrency is split
between the two clusters; production Odoo worker sizing uses 75% of host CPUs
so database work can burst. Dev-only installations retain the original lean
10% shared-buffer target.

Profile renderers remain separate because dev and production have different
WAL, connection, logging, and autovacuum requirements. They receive values
from the same plan instead of calculating independent shares.

## How it works (macro)

`resource_plan.py` is the single source of tuning budgets. A stable planner
version and the relevant plan fields produce an `ODUFLOW-TUNE` fingerprint in
each generated config. Startup recomputes the expected fingerprint and warns
when a managed config reflects different resources, a different production
mode, or the legacy pre-planner format.

Retuning is an explicit operator boundary. `oduflow retune-postgres` prints the
plan and unified diffs without writing. `--apply` backs up and writes managed
PostgreSQL configs, regenerates and stages the derived `odoo.conf` in existing
production containers, then lists the PostgreSQL and Odoo containers that need
restarting; it does not restart them. A custom PostgreSQL config is refused
unless the operator also passes `--force`. Matching fingerprints preserve
subsequent operator edits and avoid header-only churn on ordinary package
upgrades.

## Consequences

- Dev PostgreSQL, production PostgreSQL, and production Odoo sizing now share
  testable combined assumptions instead of relying on comments and caps.
- `production.enabled` becomes part of resource intent; toggling it can make a
  managed dev config stale and prompts an explicit retune.
- Resource or package changes never silently rewrite a config or restart a
  database. This preserves availability and the existing `# KEEP` contract at
  the cost of an operator step before new capacity is used.
- The plan cannot bound an arbitrary number of ephemeral dev Odoo containers
  and is not a hard admission-control system. Docker limits/quotas remain the
  enforcement layer; the planner coordinates generated defaults.

## History

- 2026-08-02 — v1 unified the tuning budgets, added fingerprints and introduced
  the preview/apply `retune-postgres` workflow.
