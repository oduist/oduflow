# 0007 — Auxiliary services, presets, and Docker volumes

**Status:** Adopted (still in force)
**Type:** Architecture
**First introduced:** `cbab215` "add service container management" (2026-02-11)
**Key code today:** `docker_ops/service_ops.py` (service CRUD), `docker_ops/service_presets.py` (save/restore named configs), `docker_ops/volume_ops.py` (named Docker volumes), plus MCP tools in `server.py`, CLI, and the dashboard Services/Volumes tabs

## Context

A per-branch Odoo environment ([[0001-mcp-orchestrated-ephemeral-per-branch-environments]])
is rarely the whole picture. Real Odoo modules talk to **companion services** —
Redis for caching/queues, Meilisearch for search, and assorted sidecars — and a
developer needs those running next to the environment to exercise the code. Up
to this point Oduflow orchestrated only the Odoo + PostgreSQL pair; everything
else had to be stood up by hand outside the tool, which broke the "the agent
spins up what it needs" promise and left those containers untracked.

Three follow-on needs surfaced:
- Service configs are **reused** across environments and survive container
  recreation, so re-typing image/port/env every time is wasteful and error-prone.
- Some services hold **persistent data** (a search index, a cache snapshot) that
  must outlive a container rebuild.
- Some sidecars (VPN/WireGuard, tun/iptables tooling) need **elevated Docker
  capabilities or host networking** that an ordinary bridge-attached container
  cannot get.

## Decision

Make **auxiliary service containers a first-class, managed entity** with full
CRUD across MCP, CLI, and the dashboard — and give them two supporting
abstractions: **presets** (reusable named configs) and **named Docker volumes**
(persistent storage), each likewise managed through all three surfaces.

- **Services.** `create/list/get/update/restart/delete` companion containers,
  attached to the shared network and routable through Traefik
  ([[0004-stable-addressing-port-registry-and-traefik]]) by hostname. Containers
  are tracked purely by **Docker labels** (`oduflow.managed`, `oduflow.team`,
  `oduflow.service`) scoped per team ([[0014-team-based-multi-tenancy]]) — no
  separate registry file — so listing/teardown finds exactly the right set.
- **Presets.** A service config can be **saved as a named preset** and
  **restored** later. Presets are a single per-team JSON file
  (`service_presets.json` in the team data dir) holding image, port, env, and
  the capability options below.
- **Volumes.** Named Docker volumes with their own CRUD, tracked by labels (no
  registry file), mountable into services as `volume_name:/path[:ro|rw]`; a
  volume in use by a service is protected from deletion.
- **Capability options.** Services can opt into `host_mode` (host networking),
  `privileged`, and `NET_ADMIN`, plus mounting **external (non-managed)
  volumes** — all carried in presets and re-applied on `update`/recreate so a
  pull or rebuild preserves them.

## How it works (macro)

- **Label-tracked, registry-free.** Both services and volumes are discovered by
  querying Docker for the team's `oduflow.*` labels rather than maintaining a
  parallel state file. The only persisted file is the presets JSON, because a
  preset is config that has *no* live container to read it back from.
- **Recreation preserves intent.** `update_service` recreates the container but
  reads its full config (env, volumes, capabilities, host mode) forward, so the
  service comes back the same after an image pull or a settings change. Presets
  encode the same fields so a restore reproduces a service from scratch.
- **Traefik routing in both network modes.** Bridge-attached services route
  normally; a `host_mode` service still gets a Traefik router whose backend
  points at `host.docker.internal:{port}`, so the addressing model holds either
  way.
- **External volumes by name fallback.** Volume resolution first looks for the
  managed name (`oduflow-vol-{team}-{name}`) and falls back to the raw name, so a
  service can mount a non-managed volume (e.g. Traefik's ACME store) when needed.
- **Teardown safety.** `destroy_system` refuses while active service containers
  exist, and an in-use volume cannot be deleted — manual cleanup stays explicit.

## Consequences

- Environments gained **companions**: an agent (or human) can stand up the Redis
  or Meilisearch a module depends on through the same MCP/CLI/UI surface as the
  environment itself, keeping the whole setup inside Oduflow's tracking.
- **Presets** turned repeated, fiddly service setups into a one-call restore and,
  crucially, made service config durable across the frequent container
  recreations the system performs.
- **Volumes** let stateful sidecars keep their data across rebuilds without
  leaving Oduflow's label-based bookkeeping.
- The **capability/host-network options** widened the range of sidecars Oduflow
  can host (VPN, network tooling) at the cost of granting elevated Docker
  privileges — a deliberate, opt-in trade-off persisted in presets so it is
  visible and reproducible rather than a one-off manual `docker run`.
- Services, presets, and volumes each became a tab in the
  [[0005-web-dashboard-and-rest-api]] dashboard, reinforcing the
  visualization-first split: agents wire services up, humans watch and intervene.

## History

- `cbab215` (2026-02-11) — service container management: `service_ops` CRUD, MCP
  tools, `oduflow list-services` CLI, dashboard Services tab, REST routes;
  `destroy_system` blocks on active services.
- `c83d749` (2026-02-12) — service presets (save/restore/list/delete);
  `ce33ee2` (2026-02-12) — presets support wired into the dashboard UI.
- `d4609e6` / `73fbee7` (2026-03-14) — `host_mode` (host networking) for
  services, across tool / REST / presets / restore, with the Traefik
  `host.docker.internal` backend.
- `6b05f8a` (2026-03-19, `#11`) — Docker volume management (label-tracked CRUD,
  `volumes` param on services, in-use delete protection, Volumes tab).
- `a396583` (2026-03-27) — allow mounting external (non-managed) volumes via a
  name fallback in volume resolution.
- `edad2ef` (2026-05-21, `#17`) — `privileged` and `NET_ADMIN` capability
  options, persisted in presets and re-applied on recreate.
