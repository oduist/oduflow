# 0034 — External Traefik routes and drop-in dynamic config

**Status:** Adopted (still in force)
**Type:** Architecture / Routing
**First introduced:** `litnimax/traefik-yml-overwrite` branch (2026-07-16)
**Key code today:** `settings.py` (`ExtraRoute`, `[route.*]` parsing/validation), `docker_ops/system_ops.py` (`_write_traefik_dynamic_config`, `_ensure_traefik`), `migrations.py` (`0006-traefik-dynamic-directory`)

## Context

In Traefik mode Oduflow generates a dynamic-config file that routes each team's
hostname to the Oduflow server, and Traefik obtains a Let's Encrypt certificate
per environment subdomain. Operators asked to also point an arbitrary domain at
a service **outside** Oduflow's control — another Docker container, a process on
the host, or a remote machine.

The generated file was mounted as a single read-only file and fully overwritten
on every server start, so hand-editing it never survived a restart. There were
two gaps: no supported way to declare "this host → that upstream", and no way to
add custom Traefik configuration (middleware, header rewrites, TLS options) that
Oduflow would not clobber.

## Decision

Offer two layered mechanisms, sharing the same Traefik instance and TLS/ACME
setup that already serves teams:

- **Declarative routes** — a `[route.<name>]` TOML section with `host` and an
  upstream `url`. Oduflow turns each into a Traefik router + load-balancer
  service, reusing the team routers' entrypoint/TLS logic (Let's Encrypt on
  `websecure` when `tls = true`, plain `web` otherwise). This covers the common
  hostname→URL case with automatic certificates and zero Traefik knowledge.
- **Drop-in dynamic files** — the dynamic config is mounted as a *directory*
  Traefik watches, not a single file. Oduflow writes only `oduflow.yml`;
  operator-authored `*.yml` files in the same directory are loaded by Traefik
  and never touched by Oduflow, giving unbounded expressiveness for cases the
  declarative form can't model.

A loopback upstream (`127.0.0.1` / `localhost`) is rewritten to
`host.docker.internal` so a declared route to a service on the host is reachable
from inside the Traefik container. Declarative routes are traefik-mode only,
must use an `http(s)` upstream, and must not collide with another route or a
team hostname.

## How it works (macro)

`Settings` parses `[route.*]` into a tuple of `ExtraRoute(name, host, url)` and
validates it alongside team settings. `_write_traefik_dynamic_config` emits the
team routers plus one router/service per extra route into `oduflow.yml`.

`_ensure_traefik` bind-mounts `<config-dir>/traefik-dynamic/` to
`/etc/traefik/dynamic` and switches the file provider from
`--providers.file.filename` to `--providers.file.directory` (still watched).
Because `_ensure_traefik` never rewrites an existing container's args, migration
`0006-traefik-dynamic-directory` removes a Traefik container still using the
single-file provider so init recreates it in directory mode; the ACME volume
persists, so certificates survive.

## Consequences

- Domains for non-Oduflow services can be terminated by the same Traefik, with
  Let's Encrypt handled automatically for the declarative form.
- Operators get an escape hatch (drop-in files) for arbitrary Traefik dynamic
  config that upgrades will not overwrite — the earlier "your edits get wiped"
  footgun is gone for anything placed outside `oduflow.yml`.
- The declarative form is intentionally limited to hostname→URL; richer routing
  is expected to use drop-in files rather than growing more TOML knobs.
- This is a reverse-proxy passthrough: Oduflow does not health-check, sanitize,
  or firewall the upstream. Operators own the target's exposure and DNS.

## History

- `litnimax/traefik-yml-overwrite` (2026-07-16) — `[route.*]` declarative routes,
  directory-watched dynamic config with operator drop-in files, and migration
  `0006` to recreate Traefik in directory mode.
