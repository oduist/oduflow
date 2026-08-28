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

Treat environment capacity and public hostname allocation as independent team
choices. `environment_slots = N` is a hard concurrent-environment cap in every
routing mode. Public names remain branch-derived by default. A team explicitly
configures `environment_hostname_mode = "slots"` when it wants Oduflow to
number the first label of its hostname: for `dev.example.com`, the reusable
pool is `dev1.example.com` through `devN.example.com`.

`create_environment` also accepts an explicit short `hostname` that replaces
the numbered prefix: `hostname="qa"` under `dev.example.com` produces
`qa.example.com`. Explicit names are useful for controlled integrations but
still consume one of the team's configured concurrent environment slots. The
configured default is 20 capacity slots with branch-derived hostnames; zero
disables the capacity cap, while numbered hostname mode requires a positive
limit.

## How it works (macro)

- A per-team `hostnames.json` registry reserves environment capacity and, when
  requested, maps environment identity to its short routing hostname.
  Read-modify-write operations use a thread mutex, process `flock`, and atomic
  replacement, matching the concurrency requirements of the persistent port
  registry.
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
- Existing unlabeled environments keep their branch-derived hostname across
  restarts and updates. A persisted explicit hostname always wins. Numbered
  assignments are created only in explicit slot mode.

## Consequences

- Certificate cardinality is bounded by the configured numbered hostname pool
  during normal automatic operation; once the pool has been issued, later
  environments reuse the same Traefik ACME certificates.
- In slot mode or with an explicit override, environment identity and public
  address are separate concepts. Routing consumers use the persisted hostname
  label when present; branch mode continues deriving the route from the
  environment name.
- A positive slot count is also a hard cap on concurrent development
  environments for the team. Capacity exhaustion is reported before
  provisioning starts.
- Explicit custom hostnames can still create additional certificate names over
  time; that is an intentional operator override rather than the automatic
  lifecycle default.

## Evolution

The original v1.69 implementation overloaded `environment_slots`: any positive
limit also enabled numbered hostnames, including for existing configurations
that had never opted into a routing change. Updating an old container then
rewrote `feature.dev.example.com` to `dev1.example.com`, which is outside a
`*.dev.example.com` DNS or wildcard-certificate scope. In August 2026 the
capacity limit was separated from the explicit hostname strategy. Branch mode
became the compatibility-safe default, the cap was extended to port mode, and
legacy automatic assignments became recoverable without disturbing explicit
custom hostnames.

## History

- `litnimax/reusable-hostname-slots` (2026-08-16) — reusable per-team hostname
  pool, explicit short hostname override, persisted routing identity and
  lifecycle integration.
- `litnimax/review-hostname-limits` (2026-08-28) — separate capacity from
  hostname policy and preserve existing wildcard-backed routes by default.
