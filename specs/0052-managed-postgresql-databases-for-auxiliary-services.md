# 0052 — Managed PostgreSQL databases for auxiliary services

**Status:** Adopted
**Type:** Architecture
**First introduced:** 2026-08-29
**Key code today:** `docker_ops/service_database_ops.py`, `service_database_credentials.py`, `stack_models.py`, `stack_ops.py`, MCP tools in `server.py`, REST/dashboard controls in `web_ui.py` and `templates/dashboard.html`

## Context

Auxiliary services ([[0007-auxiliary-services-and-volumes]]) can persist files
in Docker volumes, but many sidecars need relational storage: workers, webhook
receivers, MCP services, and integration gateways commonly expect PostgreSQL.
Running an unmanaged PostgreSQL container per service duplicates infrastructure,
bypasses team disk quotas, and leaves credentials, backups, and lifecycle outside
Oduflow. Reusing an Odoo environment database is worse: it couples unrelated
schemas and grants the sidecar access to Odoo data.

The existing development PostgreSQL cluster already reaches every isolated team
network ([[0027-hard-tenant-isolation]]), gives each team a quota-backed
tablespace ([[0026-per-team-pg-tablespaces]]), and supports scoped roles
([[0013-per-environment-db-credentials-and-sanitization]]). It is the natural
storage plane, provided sidecar databases occupy a separate namespace and have
an independent lifecycle.

## Decision

Make a PostgreSQL database for an auxiliary service a first-class, persistent,
team-scoped resource.

- Every logical database gets a distinct physical database and a dedicated
  login role that is an owner but never a PostgreSQL administrator.
- The resource lives in the team's existing tablespace and counts toward the
  same database and filesystem quotas as environment/template databases.
- Database lifecycle is independent of container lifecycle. Updating or deleting
  a service never drops its data; deletion is a separate, explicit destructive
  operation that terminates connections and removes both database and role.
- Generated credentials are stored atomically in owner-only per-resource files.
  Database listings and logs never include passwords; create, explicit
  credential reveal, and rotation are the only database secret-bearing surfaces.
- Declarative Stacks ([[0046-declarative-oduflow-stacks]]) may declare databases
  and resolve selected connection fields into auxiliary-service environment
  variables at apply time. Manifests and Stack state retain references, not
  generated secrets.

The initial scope is the shared **development** PostgreSQL cluster and
bridge-mode team services. Production-cluster placement is deferred until its
snapshot/restore and per-database recovery semantics are designed. Host-network
services are also excluded because the shared cluster deliberately publishes no
host port.

## How it works (macro)

Physical names use a reserved `oduflow_service_<team>_` namespace; roles use
`svc_<team>_`. Team ids are unvalidated configuration keys, so the readable
rendering is used only when it uniquely determines the team/database pair;
otherwise — an ambiguous team id, or PostgreSQL's 63-byte identifier limit — a
digest of the exact pair is appended. Quota accounting matches service
databases by exact name rather than by prefix, because a prefix would also
capture the environment databases of a team literally named `service_<x>`.
Logical names are strict lowercase path-safe resource names.

A credential record under `team.data_dir/service_databases/` carries the
mapping and secret; PostgreSQL remains authoritative for live size, connection,
database, and role state. Missing catalog objects are reported as drift rather
than silently recreated or deleted.

Create performs quota admission, ensures the team network/tablespace, creates a
non-superuser role and owner database, restricts public database grants, then
atomically publishes credentials. Failures compensate database and role creation.
Password rotation similarly restores the old PostgreSQL password if credential
persistence fails. Resource-scoped `svc-db:<team>:<name>` locks
([[0050-granular-resource-locks]]) serialize mutations without blocking unrelated
environments or services; because that key is per database name, quota
admission and creation additionally run under a team-scoped registry mutex, so
concurrent creates cannot jointly overshoot the quota.

Stacks create databases before Odoo and auxiliary containers. A `database` plus
`databaseField` value source resolves `url`, `host`, `port`, `database`,
`username`, or `password` only after the database exists. Docker necessarily
receives resolved environment values, preserving its existing host-admin trust
boundary, but Stack manifests, hashes, plans, and state contain only references.

## Consequences

- Sidecars gain relational persistence without another database server per
  container, and data survives routine service recreation.
- Team network, role, tablespace, and quota boundaries remain aligned with the
  rest of Oduflow's multi-tenant model.
- Database deletion is intentionally not coupled to service or Stack pruning;
  removing YAML or a container cannot silently destroy relational data.
- A database owner can manage its own schema but cannot create roles/databases,
  replicate, or become the shared superuser. As with scoped Odoo roles, cluster
  administration stays inside Oduflow.
- The shared dev cluster is not a production backup promise. Workloads needing
  WAL-G/PITR must wait for an explicit production-sidecar storage design.

## History

- 2026-08-29 — decision introduced with managed database CRUD, dashboard/REST/
  MCP surfaces, quota integration, and declarative Stack value sources.
