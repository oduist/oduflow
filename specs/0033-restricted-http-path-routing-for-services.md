# 0033 — Restricted HTTP path routing for auxiliary services

**Status:** Adopted (still in force)
**Type:** Architecture / Service exposure
**First introduced:** `litnimax/service-route-architecture` branch (2026-07-14)
**Key code today:** `docker_ops/service_ops.py` (validation and Traefik labels), service presets, MCP/REST/dashboard service surfaces

## Context

Auxiliary services originally exposed one HTTP port through a hostname-only
Traefik router. That is adequate for Redis-like single-port services, but a
service can host several HTTP interfaces on different ports. FreeSWITCH is the
concrete case: its XML-RPC interface lives at `/RPC2` on port 8080, while other
HTTP modules may use different paths and ports.

A hostname-only router also forwards scanning and unrelated paths to the
backend. Operators wanted the public surface to be an allowlist: only declared
URL prefixes should reach the service, and all other paths should stop at
Traefik. The capability must work for ordinary bridge services as well as
`host_mode` services without opening arbitrary proxying across team networks.

## Decision

Let a managed service choose exactly one HTTP exposure model in Traefik mode:
the existing single catch-all `port`, or a non-empty list of restricted
`routes`. Each route contains a path prefix, a port on the same service, and an
optional `strip_prefix` flag.

- Prefixes match on path-segment boundaries (`/api` and `/api/...`, never
  `/apix`). No hostname-only fallback router is generated when routes exist.
- A bridge service is reached on its container IP and declared port. A
  host-network service is reached through `host.docker.internal` and the
  declared port.
- Route targets cannot name another container, hostname, scheme, or URL. This
  is service exposure, not a general reverse proxy or SSRF facility.
- URL paths apply only to HTTP(S)/WebSocket traffic. Raw TCP/UDP services need a
  future, separate entrypoint and exposure model.

## How it works (macro)

Each route becomes an explicit Traefik HTTP router and load-balancer service on
the managed container. The router combines the service hostname with a
segment-safe path rule and points to the route-specific backend port. Optional
StripPrefix middleware adapts root-mounted applications and preserves the
original prefix in `X-Forwarded-Prefix`.

The canonical route list is kept in both the service preset and an Oduflow
Docker label. Presets make restore/recreate durable; the label lets live
inspection recover configuration when no preset exists. Updates replace the
whole route list, matching the existing replacement semantics for env vars and
volumes. Clearing routes requires a replacement catch-all port in the same
update, so a service never enters an ambiguous exposure state.

## Consequences

- Multi-port HTTP services keep one hostname while exposing only their intended
  path surface; unknown paths receive Traefik's 404 without touching backend
  logs or handlers.
- The same public model works across Docker networking modes; only backend
  address resolution differs.
- Existing services and presets need no data migration: absence of `routes`
  retains the original single-port behavior.
- This does not firewall a host-network process that binds directly to a public
  host interface. Such a process must bind loopback or be protected by the host
  firewall independently of Traefik routing.

## History

- `litnimax/service-route-architecture` (2026-07-14) — structured service
  routes across core orchestration, presets, MCP, REST, dashboard and docs.
