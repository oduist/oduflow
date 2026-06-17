# 0004 — Stable environment addressing: persistent port registry + Traefik routing

**Status:** Adopted (still in force)
**Type:** Architecture
**First introduced:** `c60eb5f` "Persient port mapping" (2026-02-07), `d9d877e` "Traefik added" (2026-02-07)
**Key code today:** `port_registry.py` (`ports.json` allocation), `docker_ops/env_ops.py` + `system_ops.py` (Traefik wiring), `naming.py`

## Context

Environments are **ephemeral and frequently recreated** — a container may be
destroyed and rebuilt many times over a branch's life (rebuild, template reload,
config change). Two addressing problems followed from that:

1. **Unstable ports.** If the host port were allocated fresh on every
   `provision`/recreate, the URL an agent or human bookmarked would change out
   from under them, and concurrent environments could race for the same port.
2. **No human-friendly URLs.** Raw `host:port` is awkward to share, doesn't do
   TLS, and doesn't scale to many concurrent environments on one host.

## Decision

Separate **port allocation** (stable, persisted) from **request routing**
(reverse proxy by hostname).

- **Persistent port registry.** A `ports.json` file (`port_registry.py`) records
  the host-port assigned to each environment so the same environment keeps the
  *same* port across recreation, and two environments never collide. Allocation
  is a read-modify-write under a **per-path thread mutex plus an flock** on a
  sidecar file, because the registry is shared state mutated by parallel API
  requests and potentially by more than one Oduflow process on the same data dir.
- **Traefik reverse proxy.** A Traefik container fronts the environments and
  routes by hostname to each container, giving stable, TLS-capable URLs instead
  of bare `host:port`.

## How it works (macro)

- On environment create, the registry returns this environment's stable host
  port (newly allocated, or the previously-recorded one on recreate); the port is
  freed back to the pool on delete.
- Traefik discovers and routes to environments so each gets a predictable URL;
  the same mechanism later serves the Web dashboard's own routes.
- Routing config evolved from a single dynamic file to a **watched directory**
  provider (`--providers.file.directory` + `--providers.file.watch`), so each
  Oduflow instance/team can register its routes independently **without
  restarting Traefik** (`8123667`).

## Consequences

- URLs are **durable**: an agent can recreate an environment and the access URL
  it reported earlier still works — important for an AI workflow that hands a URL
  back to a human or reuses it across steps.
- Concurrency-safe allocation was a prerequisite for safely running many
  environments and bulk operations (e.g. bulk delete) in parallel, reinforcing
  the per-branch concurrency model.
- Centralizing ingress in Traefik created the natural seam for later
  multi-instance / multi-team routing and for hosting the dashboard behind the
  same proxy.

## History

- `c60eb5f` (2026-02-07) — persistent port mapping: `port_registry.py` + `ports.json`,
  ports survive recreation.
- `d9d877e` (2026-02-07) — Traefik added: hostname-based routing to environments.
- `8123667` (2026-02-12) — multi-instance routing via Traefik **file directory**
  provider with watch; each instance writes its own `instance-{id}.yml`, no
  Traefik restart needed.
- `727448b` — match TLS certresolver name to Traefik's ACME provider.
