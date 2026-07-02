# 0024 — Per-team PostgreSQL tablespaces

**Status:** Adopted
**Type:** Architecture
**First introduced:** this change (2026-07-02), branch `litnimax/multi-tenant-hosting-design`
**Key code today:** `docker_ops/system_ops.py` (`_ensure_pg_container`, `ensure_team_tablespace`), `naming.py` (`get_tablespace_name`), `migrations.py` (`0002-team-pg-tablespaces`), `CREATE DATABASE ... TABLESPACE` call sites in `env_ops.py` / `system_ops.py`

## Context

Hosting mutually-untrusting clients on one machine ([[0014-team-based-multi-tenancy]]
hardened for that case) needs a per-client disk quota that is *complete*. A
filesystem project quota on `team_{id}/` covers workspaces, filestores, and
template dumps — but not PostgreSQL: all databases lived in one `PGDATA`
inside the shared PG container's volume, invisible to any per-team quota, and
"how much disk does this client use" required adding up numbers from two
different worlds.

Mounting the whole data dir into the PG container to co-locate database files
with team files was rejected outright: PostgreSQL has no business seeing
other teams' workspaces or credential files.

## Decision

Give every team its **own PostgreSQL tablespace**, with its files under a
**dedicated** `base_data_dir/pg_tablespaces/team_{id}/` directory:

- The PG container mounts **only** `pg_tablespaces/` (as `/tablespaces`) —
  one mount, created once. Adding a team is `mkdir` + `CREATE TABLESPACE`
  inside the existing mount; the container is never recreated again.
- Every `CREATE DATABASE` (environments, templates) goes through
  `ensure_team_tablespace()` (idempotent, one catalog query) and carries
  `TABLESPACE "oduflow_team_{id}"`.
- For quotas, hosting setups assign `team_{id}/` and
  `pg_tablespaces/team_{id}/` the **same XFS project ID** — project quotas
  attach to the ID, not to a single subtree — so one `disk_quota_gb` covers
  the client's files *and* databases.
- WAL stays in the shared `PGDATA`, deliberately: a team hitting its quota
  gets aborted transactions (ENOSPC on data files is safe), never a
  server-wide PANIC — the noisy neighbor stays isolated.

## How it works (macro)

- `_ensure_pg_container` (extracted from the two duplicated PG-bootstrap
  blocks) creates the PG container with the `/tablespaces` mount.
- `ensure_team_tablespace` creates the host dir, fixes ownership to the
  `postgres` OS user inside the container, and issues `CREATE TABLESPACE`.
- Startup migration `0002-team-pg-tablespaces` ([[0023-startup-data-migrations]])
  converts existing installs: recreates the PG container once if the mount is
  missing (data volume persists; seconds of downtime at startup), then
  `ALTER DATABASE ... SET TABLESPACE` per team database — blocking reconnects
  via `ALLOW_CONNECTIONS false` + `pg_terminate_backend` so the move cannot
  race Odoo's reconnect loop. Move time is proportional to database size;
  already-moved databases are skipped, so a partial run resumes cleanly.

## Consequences

- A client's disk consumption is one number and one enforcement point; the
  external billing/usage API (`/api/usage`) and `du`/`xfs_quota` now agree on
  what "the client's disk" means.
- Databases become visible per team on the host filesystem — useful for
  ops even without quotas.
- Operators using `pg_basebackup`-style physical backups must be aware
  tablespaces exist; the built-in template flow (`pg_dump`) is unaffected.
- One-time upgrade cost on existing installs: a PG container recreate plus a
  physical copy of every database at first startup.

## History

- Follows the naming-v2 hardening (`0001-team-scoped-container-names`) in the
  multi-tenant hosting pass; see [[0014-team-based-multi-tenancy]] and
  [[0023-startup-data-migrations]].
