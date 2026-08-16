# 0048 — Reusable environment hostname slots

**Status:** Adopted
**Type:** Routing / Capacity
**First introduced:** `litnimax/reusable-hostname-slots` branch (2026-08-16)
**Key code today:** `hostname_registry.py`, `docker_ops/env_ops.py`, `settings.py`, `naming.py`

## Context

[[0004-stable-addressing-port-registry-and-traefik]] originally derived every
Traefik hostname from the environment name. That made URLs descriptive, but
ephemeral branch environments continually introduced new DNS names. With
Traefik's HTTP-01 ACME integration, each new name required a new Let's Encrypt
certificate and sufficiently active teams could reach certificate issuance
rate limits even though they never needed an unbounded set of simultaneous
addresses.

The environment name still matters for source, database, workspace and agent
identity. It does not need to be the public routing identity.

## Decision

Make the public environment hostname an independently persisted, reusable team
resource. A team can configure `environment_slots = N`; Oduflow then numbers
the first label of the team hostname. For `dev.example.com`, the reusable pool
is `dev1.example.com` through `devN.example.com` instead of unbounded
branch-derived names.

`create_environment` also accepts an explicit short `hostname` that replaces
the numbered prefix: `hostname="qa"` under `dev.example.com` produces
`qa.example.com`. Explicit names are useful for controlled integrations but
still consume one of the team's configured concurrent environment slots. The
configured default is 20 slots; a zero slot count preserves the legacy
branch-derived behavior for compatibility.

## How it works (macro)

- A per-team `hostnames.json` registry maps environment identity to its short
  routing hostname. Read-modify-write operations use a thread mutex, process
  `flock`, and atomic replacement, matching the concurrency requirements of the
  persistent port registry.
- Automatic allocation splits the team hostname into a reusable prefix and
  parent domain, then selects the first unused numbered prefix (`dev1`, `dev2`,
  ...). Active legacy environments and in-flight reservations both count toward
  capacity, so two parallel creates cannot receive the same hostname or exceed
  the configured team limit.
- The short hostname is stored on the Odoo container as an `oduflow.hostname`
  label. URL reporting, Traefik rules, browser login handoff and container
  updates read that label instead of recomputing the route from the branch.
- Stops and updates preserve the reservation. Deletion releases it. A failed
  create releases a reservation when no serving container was created.
- Existing unlabeled environments keep their branch-derived hostname until an
  update migrates them into the configured pool, avoiding a surprise URL change
  merely from restarting the Oduflow server.

## Consequences

- Certificate cardinality is bounded by the configured numbered hostname pool
  during normal automatic operation; once the pool has been issued, later
  environments reuse the same Traefik ACME certificates.
- Environment identity and public address are no longer the same concept. All
  routing consumers must use the persisted hostname label, while database,
  workspace and locking code continues to use the environment name.
- A positive slot count is also a hard cap on concurrent development
  environments for the team. Capacity exhaustion is reported before
  provisioning starts.
- Explicit custom hostnames can still create additional certificate names over
  time; that is an intentional operator override rather than the automatic
  lifecycle default.

## History

- `litnimax/reusable-hostname-slots` (2026-08-16) — reusable per-team hostname
  pool, explicit short hostname override, persisted routing identity and
  lifecycle integration.
