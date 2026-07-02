# 0025 — Hard tenant isolation: per-team networks, resource limits, disk quotas

**Status:** Adopted
**Type:** Architecture
**First introduced:** this change (2026-07-02), branch `litnimax/multi-tenant-hosting-design`
**Key code today:** `system_ops.py` (`ensure_team_network`), `naming.py` (`get_team_network_name`), `stats.py` (`default_env_limits`), `quotas.py`, `migrations.py` (`0003-per-team-networks`, `0004-env-resource-limits`)

## Context

Team-based multi-tenancy ([[0014-team-based-multi-tenancy]]) isolated teams'
*data* — directories, databases, ports, credentials — but all containers still
shared one Docker network and ran without resource limits. For teams inside
one organization that was acceptable; for **mutually-untrusting clients** it
is not, because environment containers execute arbitrary client code (their
addons): client A's module could reach client B's Odoo over the shared
network (XML-RPC, default admin passwords), hit B's Redis/Meilisearch
(typically unauthenticated), or simply OOM the whole machine with one heavy
test run. Disk quotas (`disk_quota_gb`, reserved since the quota work) had no
enforcement.

## Decision

Make the container, network, and filesystem layers enforce the team boundary:

- **Per-team networks.** Each team gets `oduflow-{team}-net`; environment and
  service containers join *only* their team's network. The shared PostgreSQL
  container and Traefik are attached to every team network — they are the
  only cross-team surface, and both are credential/route protected. Traefik
  backend selection moves from a global `--providers.docker.network` to the
  per-container `traefik.docker.network` label.
- **Default resource limits** on environment containers, auto-derived from
  host size (no config knobs): memory = host RAM / 4 clamped to [2 GB, 8 GB],
  plus a pids ceiling (fork bombs). CPU is deliberately uncapped — it is
  compressible and the kernel scheduler already arbitrates contention.
- **Disk quota enforcement** for `disk_quota_gb` via XFS project quotas
  (`quotas.py`): the team dir and its PG tablespace ([[0024-per-team-pg-tablespaces]])
  share one project ID, so a single kernel-enforced `bhard` limit covers the
  client's files and databases. On filesystems without project-quota support
  enforcement is off with one startup warning — usage stays visible via the
  dashboard and `/api/usage`.

## How it works (macro)

- `ensure_team_network` is idempotent and called from system init and every
  environment/service creation; it creates the network and attaches infra.
- Startup migrations ([[0023-startup-data-migrations]]) convert existing
  installs live: `0003-per-team-networks` connects each managed container to
  its team network and disconnects it from the shared one (no restarts;
  established DB connections re-establish over the team network), removing a
  Traefik container that still pins the old backend network so init recreates
  it. `0004-env-resource-limits` retrofits limits onto running environment
  containers via `docker update`, best-effort.
- `quotas.apply_all` runs on every server start after init: detects
  Linux + xfsprogs + XFS with `prjquota`, allocates stable numeric project
  IDs per team (`quota_projects.json`), stamps both directory trees, and sets
  the hard limit.

## Consequences

- Lateral movement between clients is gone at the network layer; a client's
  addon code can reach only its own containers plus the password-protected
  shared PG and Traefik.
- One client can no longer take the machine down by memory exhaustion or a
  fork bomb, and — on a properly provisioned host (XFS + `prjquota`) — cannot
  exceed its disk allotment at all.
- The recommended hosting layout gains one requirement: put the data dir on
  an XFS filesystem mounted with `prjquota` for quota enforcement.
- Residual trust surface: the shared PostgreSQL and Traefik containers, and
  the oduflow process itself (Docker socket) — application-level bugs remain
  the tenant boundary there, which is why the strict HTTP team resolution and
  input-validation audits stay load-bearing.

## History

- Completes the multi-tenant hosting pass started with DB quotas / strict
  HTTP resolution, naming v2 (`0001`, `0002` migrations), and
  [[0024-per-team-pg-tablespaces]].
