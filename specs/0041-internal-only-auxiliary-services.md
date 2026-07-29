# 0041 — Internal-only auxiliary services

**Status:** Adopted (still in force)
**Type:** Architecture / Service exposure
**First introduced:** 2026-07-29
**Key code today:** `docker_ops/service_ops.py` (`_validate_service_exposure`, `create_service`, `update_service`), service presets, MCP/REST/dashboard service surfaces

## Context

Every managed auxiliary service was required to be publicly reachable. Outside
Traefik that meant a `port` published on the host; in Traefik mode it meant a
hostname-only router or, since [[0033-restricted-http-path-routing-for-services]],
a restricted list of path routes. There was no way to say "this container exists
for Odoo, and for nobody else."

That is the wrong default for a growing class of sidecars. A message broker
(NATS), a cache, an embedding service, or an internal API is consumed over the
team's Docker network and gains nothing from a public address — it only gains an
attack surface. Operators worked around the requirement by declaring a dummy
HTTP route that pointed nowhere useful. The workaround was worse than it looked:
the router was real, the hostname was real and resolvable, and internet scanners
found and probed it continuously. The exposure was obfuscated, not removed.

The requirement itself was also a category error. Publication and reachability
had been conflated: a Docker network resolves containers by name and carries
traffic on whatever port the process inside is listening on, entirely
independently of `EXPOSE`, published host ports, and reverse-proxy routers.
Oduflow was demanding a public exposure model as the price of admission to a
private network.

## Decision

Add `internal_only` as a third, mutually exclusive exposure mode alongside
catch-all `port` and restricted `routes`. It defaults to `false`, so every
existing service, preset, and API call keeps its current meaning.

An internal-only service gets no public hostname, no Traefik router, service or
middleware labels, and no Docker host port binding. It joins the team network
under its ordinary container name and is reached exactly as any sibling service
is: `oduflow-{team_id}-svc-{name}:{whatever port the image listens on}`.

Two further decisions give the mode teeth:

- **The container states its own refusal.** It carries `traefik.enable=false` —
  the only `traefik.*` label it ever has. Oduflow's Traefik already runs with
  `exposedByDefault=false`, so a label-less container gets no router anyway; but
  that invariant lives in a *different* container, is not re-applied to a
  Traefik that predates it, and does not hold for an operator-supplied proxy.
  Relying on it alone would make a service's isolation a property of something
  else's configuration. Both defences are kept, and both are tested.
- **Ambiguity is refused, not resolved by guessing.** The mode is recorded in
  the preset *and* on the container. `update_service` reads the preset, but
  preset writes are best-effort, so the two can drift. When they disagree and no
  explicit `internal_only` is supplied, the call fails with a conflict error.
  Either guess would silently change public exposure — re-publishing a withdrawn
  service, or withdrawing a working one — and silent exposure changes are the
  precise failure this whole feature exists to prevent. An explicit override
  names the intended state and forces a recreate, so the container actually
  reaches it rather than merely being reported as such.

Two deliberate non-additions:

- **No `internal_port` argument.** The listening port is a property of the image
  and its configuration, not of Oduflow's orchestration. Recording it would be
  documentation masquerading as configuration — a second source of truth that
  can drift from the process actually listening, while changing nothing about
  what works.
- **No new escape hatch for ambiguity.** `internal_only=true` combined with
  `port`, `routes`, `hostname`, or `host_mode` is a validation error, not a
  silently ignored argument. `host_mode` in particular is excluded on its
  merits: a host-network container is not resolvable by container name *and*
  binds the host's interfaces, which inverts both halves of what this mode
  promises.

## How it works (macro)

Exposure validation is a single function shared by create, update, preset
restore, and the REST and MCP paths, so the mode cannot be enforced in one
entrypoint and quietly bypassed in another. The internal-only branch computes no
hostname, emits no Traefik labels, and passes no published-port configuration;
everything else about the container — network, name, volumes, capabilities,
restart policy — is unchanged.

The mode is recorded in an Oduflow Docker label and in the service preset,
matching how host mode and routes are already persisted: the preset makes
restore durable, the label lets live inspection recover the configuration when
no preset exists. Both are written only when true, so old presets and old
containers read back as published services and no startup migration is needed.

`update_service` treats the flag as a tri-state override. Switching to
internal-only drops the previous public exposure outright and recreates the
container, which is what makes the old router labels and port bindings actually
disappear rather than linger in Docker. Switching back requires a new `port` or
`routes` in the same call, so a service never rests in an ambiguous state.
Introspection reports the mode explicitly, so an internal-only service is
distinguishable from a published one that is merely misconfigured.

## Consequences

- Sidecars that were never meant to be public can stop being public, without a
  dummy route standing in for the absence of one.
- The public surface of a team shrinks to the services that actually serve
  users; scanner traffic no longer reaches internal brokers at all, because
  there is no router to reach them through.
- Inter-container connectivity is unchanged and needs no new configuration:
  container name plus the image's own port, as before.
- No data migration. Absence of the flag means `false` in both presets and
  container labels — which is also why a legacy pair (preset without the field,
  container without the label) reads as agreement rather than as a conflict.
- `update_service` gained a failure mode it did not have before: a service whose
  preset and container disagree about the mode cannot be updated without saying
  which is right. That is deliberate — the alternative is a silent exposure
  change — but it means an unattended update can now stop and ask.
- The mode is chosen at create/restore time. Switching an existing service runs
  through the MCP, CLI or REST surface; the dashboard does not offer the
  transition. Adding it is a UI question, not a gap in the contract.
- This does not isolate a service *within* the team network — every container on
  `oduflow-{team_id}-net` can still reach it. Internal-only is about removing
  public exposure, not about intra-team segmentation, which remains the tenant
  boundary described in [[0027-hard-tenant-isolation]].
- The implicit Traefik ACME mount ([[0032-implicit-traefik-acme-mount-for-services]])
  still applies to internal-only services, so they can read the shared
  certificate store they have no use for. Narrowing that perimeter was
  considered and deliberately left out of this change.

## History

- 2026-07-29 — internal-only exposure mode across core orchestration, presets,
  MCP, REST, dashboard, tests and docs; `traefik.enable=false` on the container
  and a conflict error on preset/label disagreement, both covered by unit tests
  and by a live-Docker check against a Traefik running `exposedByDefault=true`.
