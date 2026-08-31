# 0053 — Explicit start commands for auxiliary services

**Status:** Adopted
**Type:** Architecture / Service lifecycle
**First introduced:** 2026-08-31
**Key code today:** `docker_ops/service_ops.py`, `docker_ops/service_presets.py`, `stack_models.py`, `stack_ops.py`, MCP tools in `server.py`, REST/dashboard controls in `web_ui.py` and `templates/dashboard.html`

## Context

Auxiliary services ([[0007-auxiliary-services-and-volumes]]) originally always
started with the Docker image's default `CMD`. That works for self-starting
images such as Redis, but many official images intentionally provide only an
`ENTRYPOINT` and expect deployment-specific arguments. MinIO, for example,
needs a storage path and may need a separate console address. Rebuilding such
images merely to supply arguments creates unnecessary image variants and moves
ordinary runtime configuration outside Oduflow.

The command must survive service update and restore, remain inspectable, and
participate in declarative Stack convergence ([[0046-declarative-oduflow-stacks]]).
At the same time, accepting a shell program would add expansion and injection
semantics that Docker does not require and would make a displayed command
ambiguous to reproduce.

## Decision

Make an explicit argv-style Docker `CMD` override part of the managed auxiliary
service configuration while always preserving the image `ENTRYPOINT`.

- Operators may enter a shell-quoted string for convenience or provide an argv
  array directly. Oduflow parses the string once and passes the resulting array
  to Docker without invoking a shell.
- The override is available consistently through MCP, REST, the dashboard,
  saved presets, and declarative Stacks.
- Updates use tri-state semantics: omission preserves the current command, a
  non-empty value replaces it, and an explicit empty value removes the override
  so the image `CMD` becomes effective again.
- Presets store argv rather than the original spelling. Live inspection reports
  the configured override and the image default separately; Stack planning
  compares effective argv so an explicit command equal to the image default is
  already converged.

## How it works (macro)

All human-oriented string inputs pass through one POSIX shell-word parser, but
the parsed result is data, not a shell script. The service runtime gives Docker
the array as its `command` option and persists the same array in the automatic
preset. Recreate and image-update paths read the preset as their authoritative
configuration; legacy services without a preset recover an override by
comparing the container command with the image default.

REST accepts only a string or an array of strings and rejects other JSON shapes.
The Stack model accepts those same two input forms, normalizes both to argv, and
publishes both forms in the generated JSON Schema. Command state participates
in resource hashing, plan drift, and apply alongside image, environment,
volumes, capabilities, and routes.

## Consequences

- Official images whose entrypoint expects arguments can run directly without
  wrapper images or manual container management.
- Commands round-trip without losing argument boundaries, including arguments
  containing spaces; shell operators and variable expansion are deliberately
  unavailable unless the configured command or image entrypoint explicitly
  invokes a shell.
- Clearing an override is explicit and distinguishable from leaving it
  unchanged, at the cost of command update semantics differing from older
  string options where an empty value means “keep”.
- Docker does not record whether a container command identical to the image
  default was supplied explicitly. Runtime convergence therefore uses effective
  argv, while presets preserve the operator's intent for later image updates.

## History

- 2026-08-31 — decision introduced with command support across service
  orchestration, presets, MCP, REST, dashboard, declarative Stacks, and docs.
