# 0013 — Per-environment PostgreSQL credentials + two-tier database sanitization

**Status:** Adopted (still in force)
**Type:** Architecture
**First introduced:** `cd0b9ec` "two-tier database sanitization" (2026-02-24), `e4949b0` "per-environment PostgreSQL credentials" (2026-02-26)
**Key code today:** `env_credentials.py` (generate / persist / load / delete per-env creds), `sanitizer.py` (two-tier script runner), `docker_ops/env_ops.py` (`_create_pg_role`, post-clone ownership fixup), `docker_ops/system_ops.py` (shared DB, role helpers)

## Context

All environments share **one PostgreSQL server** container, with one database per
environment. Two data-safety problems followed from how that started out, and
from where the databases come from.

1. **Shared superuser.** Every environment connected as the same global
   `odoo`/`odoo` user. That account owns *every* environment's database, so a
   single leaked or misused credential — or a sloppy script — reaches across all
   tenants and branches. There was no isolation at the database layer to match
   the container/filestore isolation already in place.
2. **Production-derived data.** Templates ([[0003-database-templates-and-filestore-isolation]])
   are frequently imported from a real production Odoo. A clone of production
   carries **live mail servers, real customer data, scheduled jobs** — exactly
   the things that must *not* fire from a throwaway dev environment. An agent
   spinning up a branch must not be able to email real customers.

A complication sits underneath both: PostgreSQL's `CREATE DATABASE ... TEMPLATE`
copies objects but leaves them **owned by the original (superuser) role**, so a
freshly created per-environment role can't run the DDL Odoo needs and can't even
see some objects in the catalog — which surfaced as confusing "relation already
exists" / "role cannot be dropped" failures.

## Decision

Two related decisions, both about confining blast radius at the database layer.

- **Per-environment PostgreSQL role.** Each environment gets its **own login role
  and password**, generated at create time, persisted to
  `workspace/env_credentials.json`, and used as the **owner** of that
  environment's database. Odoo connects as this role. The global superuser is
  retained only for **administrative** operations (CREATE/DROP DATABASE, template
  loads, GC, orphan cleanup). Old environments without a credentials file fall
  back to the global user, so the change is backward compatible.
- **Two-tier sanitization.** After provisioning (default on, `sanitize=True`),
  Oduflow scrubs the database in two tiers: a **system-wide** baseline owned by
  the team admin, then **per-repo** rules owned by the developer. Both tiers are
  directories of `.sql` and `.py` scripts run in alphabetical order, so the
  baseline is consistent across the fleet while each project can add its own
  rules.

## How it works (macro)

- **Credential lifecycle.** `create_credentials` derives a role name from the
  team + environment (`u_<team>_<env>`, capped at PostgreSQL's 63-char limit) and
  a random password, writes them next to the environment, and `_create_pg_role`
  creates the role as DB owner. Tests, ad-hoc DB queries, the dashboard SQL
  terminal, and the sanitizer all `load_credentials` so they connect as the
  per-env role, not the superuser. On delete/rebuild the role is dropped and the
  file removed; orphan-role detection cleans up stragglers.
- **Post-clone ownership fixup.** When a database is created from a template, the
  fixup transfers ownership of the `public` schema and every table, sequence, and
  view to the new per-env role (DDL requires ownership; `GRANT ALL` only covers
  DML), and drops Odoo's signaling sequences carried over from the template so
  Odoo can recreate them cleanly on first start. This is the load-bearing glue
  that makes a cloned DB usable under a non-superuser owner.
- **Sanitization tiers.** `sanitize_environment` runs scripts from the team's
  `{data_dir}/odoo_sanitize/` first (seeded on `oduflow init` with a bundled
  `01_disable_mail.sql`), then from the repo's `.odoo_sanitize/` folder. `.sql`
  runs against the env DB; `.py` runs *inside* the Odoo container using the
  per-env credentials, so repo rules can use the Odoo ORM. Failures are logged as
  warnings, not fatal — sanitization is best-effort hardening, not a gate.

## Consequences

- A compromised or misbehaving environment is **confined to its own database**:
  its login owns only its own data, so the database layer now matches the
  isolation that containers, ports, and filestores already provide. Tenancy
  isolation ([[0014-team-based-multi-tenancy]]) gains a real DB-level boundary.
- Production-derived templates become **safe to clone** for development: mail
  servers are removed and risky data neutralised before the agent touches the
  environment, so a dev branch cannot email real customers or trigger live jobs.
- **Two tiers** put each rule where it belongs — the operator owns the
  fleet-wide baseline, the developer owns project-specific scrubbing — mirroring
  the same deployment-vs-repo split as the configuration model
  ([[0016-configuration-model]]).
- The non-superuser ownership model is **more correct but more fragile** at the
  clone boundary: it required a string of fixes (ownership reassignment strategy,
  role-membership grants, `--no-owner` template restores) because PostgreSQL's
  template-clone and role-drop semantics interact in non-obvious ways.

## Evolution

The clone-time ownership transfer was reworked several times as edge cases
surfaced:

- `31df320` (2026-02-26) — iterate tables/sequences and `ALTER ... OWNER TO` the
  new role after a template clone, so the env role can see them (fixes spurious
  "relation already exists" on signaling sequences).
- `c6878e4` (2026-02-27) — replace the per-type loops with a single
  `REASSIGN OWNED BY` covering all object types.
- `5c7b737` (2026-02-27) — `REASSIGN OWNED BY` fails on system objects owned by a
  superuser; instead **grant the env role membership** in the superuser role so it
  inherits ownership for DDL. (The current code keeps an explicit
  schema/table/sequence/view `ALTER OWNER` sweep plus a signaling-sequence drop.)

## History

- `cd0b9ec` (2026-02-24) — two-tier sanitization: drop hardcoded built-in queries;
  bundle `01_disable_mail.sql`; create `/etc/oduflow/odoo_sanitize/` on init; run
  system-wide then per-repo `.odoo_sanitize/` scripts; add `sanitize` param to
  `create_environment` (default True).
- `e4949b0` (2026-02-26) — per-environment PostgreSQL credentials: `env_credentials.py`,
  `_create_pg_role`/`_drop_pg_role`, env role as DB owner, global user kept for
  admin ops; tests/queries/sanitizer/SQL terminal use per-env creds; orphan-role
  cleanup; backward-compatible fallback.
- `31df320`, `c6878e4`, `5c7b737` (2026-02-26 → 02-27) — clone-time ownership
  fixups so a non-superuser env role owns the cloned DB (see Evolution).
- `4f729b1` (2026-03-02) — **delete** mail servers (`fetchmail_server`,
  `ir_mail_server`) instead of merely disabling them; disabling still let Odoo
  send in some cases.
