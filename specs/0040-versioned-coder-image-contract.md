# 0040 — Versioned coder image contract

**Status:** Adopted
**Type:** Architecture — runtime delivery and compatibility
**First introduced:** this change (2026-07-22)
**Key code:** `docker/agent/Dockerfile`; `.github/workflows/publish-coder.yml`; `settings.py`; `docker_ops/env_ops.py`

## Context

The hosted agent server and its coder image form one runtime: Python launches
specific CLI/ACP binaries while the image supplies the entrypoint, operating
system packages, user, and persistent-volume layout. The original deployment
published and configured a rolling `:latest` image. Because that tag did not
change when its contents changed, Oduflow also carried a manually incremented
runtime epoch solely to invalidate the container configuration hash.

That split version signal was fragile. A server could start invoking a new
binary while an existing container still ran the previous entrypoint, and a
failed pull could delete the working container before Docker discovered that
the replacement was unavailable. The rolling tag also made rollback and the
server/image compatibility pair implicit.

## Decision

Coder images are published only under immutable version tags. Each Oduflow
release pins one exact `oduist/oduflow-coder:<version>` default; no `:latest`
tag is published. A change to `docker/agent/**` must bump `CODER_VERSION` and
the server's pinned default in the same change.

The exact image tag is part of a declarative Docker run configuration. That
same configuration is both hashed into the container label and passed to
Docker, removing the separate runtime epoch. Before replacing a mismatched
container, Oduflow must successfully pull the desired image. Pull failure
leaves the working container and its old hash intact so the next startup can
retry.

The former official `oduist/oduflow-coder:latest` configuration value resolves
to the current pinned default with a warning. Other configured image names
remain explicit operator overrides, but they must be available from a registry;
the runtime no longer falls back to an unpullable local development tag.

## How it works

- CI reads `CODER_VERSION` from the Dockerfile and publishes one amd64/arm64
  manifest under that version only. Publication fails if the tag already
  exists, so a forgotten version bump cannot replace an immutable artifact.
- `DEFAULT_AGENT_IMAGE` carries the compatible image tag for the Python server;
  a unit test keeps it equal to the Dockerfile version.
- The container hash covers the image, environment, volumes, user, network,
  host mapping, shared-memory size, and restart policy. Changing any of those
  fields recreates the container without a manual compatibility counter.
- Persistent HOME and workspace volumes remain outside the container lifecycle,
  so image upgrades retain authentication, transcripts, browser state, and
  checkouts.

## Consequences

- Server/image compatibility and rollback are explicit and reproducible.
- Publishing an image and selecting it in Oduflow are one coordinated change;
  forgetting either version bump fails the unit suite or leaves the old default.
- A registry outage delays an upgrade instead of destroying the working agent.
- Operators who intentionally override `[agent].image` own the availability and
  compatibility of that immutable tag.
- Local-only coder tags are no longer a supported runtime path.

## History

- 2026-07-22 — replace rolling `:latest` publication and the manual agent
  runtime epoch with immutable, release-coupled tags and pull-before-replace.
