# 0032 — Auxiliary services receive the Traefik ACME store read-only

**Status:** Adopted (still in force)
**Type:** Architecture / Security boundary
**First introduced:** `litnimax/whitelist-traefik-acme-volume` branch (2026-07-13)
**Key code today:** `docker_ops/service_ops.py` (implicit mount and update preflight), service guidance in `templates/agent_guides/agent_instructions.md`

## Context

Some auxiliary services terminate their own TLS rather than leaving termination
to Traefik. FreeSWITCH is the concrete case: its Verto/WSS startup needs the
certificate material that Traefik has already obtained from Let's Encrypt and
stored in `acme.json`.

Oduflow's hard tenant-isolation pass deliberately rejected every raw
`oduflow-*` volume supplied through the public service `volumes` argument. That
closed access to the shared database, other teams' managed volumes, and system
volumes, but also made Traefik's ACME store unavailable. Attempting to add it to
an existing service exposed a second problem: `update_service` validated the
volume only while recreating, after the old container had already been removed.

Per-service certificate projection, new TLS flags, and a certificate-delivery
API were considered. They add lifecycle and renewal machinery that is not
needed for the operating model: services are trusted containers, and whether a
service consumes the mounted store is the service's concern.

## Decision

In Traefik TLS mode, mount the exact deployment ACME volume into **every**
auxiliary service at `/etc/traefik`, always read-only. The mount is platform
configuration: callers neither request nor override it, and it is not persisted
in service presets. There is no prefix or wildcard allowance for raw system
volumes; all user-supplied `oduflow-*` mounts remain forbidden.

Also preflight the complete candidate volume configuration in `update_service`
before stopping or removing the current container. This addresses configuration
errors such as missing or reserved volumes; it is not a general transactional
rollback for arbitrary Docker failures after recreation begins.

## How it works (macro)

- `create_service` resolves caller-supplied volumes normally, verifies the exact
  configured Traefik ACME volume exists, then adds
  `volume:/etc/traefik:ro` to the Docker run configuration.
- `/etc/traefik` is reserved while the implicit mount is active, so a caller
  cannot obscure it with another volume. Port mode and Traefik installations
  where Oduflow does not terminate TLS do not receive the mount.
- `update_service` resolves the resulting mounts before pulling or deleting.
  A legacy service whose live container lacks the implicit mount is treated as
  configuration drift and recreated on an otherwise ordinary update.
- Presets continue to describe only user-controlled service configuration.
  Live inspection reports the implicit mount from Docker's actual mount list.

## Consequences

- FreeSWITCH and similar trusted services can consume `acme.json` without a
  special public API, custom TLS parameters, or manual raw system-volume access.
- Read-only protects certificate-store integrity and Traefik availability, but
  not confidentiality. Every service can read and exfiltrate all certificate
  and private-key material in the shared store; service images and operators
  are therefore inside the deployment trust boundary.
- This intentionally weakens the cross-service and cross-team confidentiality
  guarantee established by [[0027-hard-tenant-isolation]], while preserving the
  prohibition on database volumes, other teams' managed volumes, and arbitrary
  system-volume name patterns.
- Invalid volume overrides no longer reproduce the observed delete-then-fail
  path. Failures after destructive recreation begins remain outside this narrow
  guardrail.

## History

- `litnimax/whitelist-traefik-acme-volume` (2026-07-13) — implicit exact
  read-only ACME mount for all Traefik TLS auxiliary services, legacy drift
  detection, and pre-removal volume validation.
