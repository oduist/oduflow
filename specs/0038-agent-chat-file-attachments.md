# 0038 — Agent Chat file attachments through persistent workspace resources

**Status:** Adopted
**Type:** Architecture — new Agent Chat capability
**First introduced:** this change (2026-07-22), branch `litnimax/collapse-agent-details`
**Key code today:** `agent_uploads.py`; Agent Chat attachment routes in `web_ui.py`; `get_agent_upload_dir` and `_agent_remove_env`; `templates/static/acp-client.js` + `chat.js`; Agent Chat composer styles in `templates/dashboard.html`

## Context

The browser Agent Chat could send only text even though users routinely need an
agent to inspect screenshots, exported data, PDFs and other files that are not
part of the environment's git checkout. Sending every file as base64 over the
ACP WebSocket would increase prompt and session-history size, duplicate large
payloads on every replay, and depend on optional multimodal support in each ACP
adapter. Saving uploads inside the checkout would instead pollute `git status`
and risk committing user attachments into the repository.

The existing [[0029-agent-console-and-chat]] architecture already provides a
stronger boundary: each team has a persistent `/workspace` volume shared with
its unprivileged coding-agent container, and every ACP session runs with the
environment checkout as its working directory.

## Decision

Agent Chat uses an **upload-then-reference** model. The authenticated dashboard
uploads each file over a dedicated HTTP endpoint, Oduflow copies it into the
team's persistent agent workspace, and the subsequent ACP prompt refers to the
stored file with a standard `resource_link` content block. The user's textual
instruction is always required; a file is context for an explicit request, not
a prompt by itself.

Attachments live outside every git checkout at
`/workspace/.oduflow-uploads/<environment>/<random-id>/<safe-name>`. They remain
available to resumed conversations and agent tools until the environment is
deleted. Environment deletion removes both its checkout and attachment tree.

Small images take a capability-aware fast path: when the ACP agent advertises
`promptCapabilities.image`, the prompt carries an ACP `image` block with the
stored file URI and inline image data so the model receives visual context
directly. Other files, large images, and agents without that capability use the
baseline `resource_link` representation.

## How it works

- The browser uploads raw file bytes separately from JSON-RPC. This keeps the
  ACP WebSocket line-framed and avoids adding multipart parsing dependencies.
- The server authenticates the team and environment, normalizes the filename,
  streams into a temporary file, and copies a private (`0600`) tar member into
  the team's running agent container. Paths and upload ids are server-derived.
- Limits are enforced at both boundaries: five files per prompt, 25 MiB per
  file, and 100 MiB of attachment storage per environment. The server is
  authoritative for byte and quota limits.
- The composer exposes upload progress, failure and removal as text states.
  `Send` remains disabled until the instruction is non-empty and every selected
  attachment is ready. Removing an unsent completed upload deletes its stored
  directory.
- Live messages and `session/load` replay render attachment content blocks as
  compact filename/type/size rows. Payloads are never rendered as active HTML.

## Consequences

- Agents can inspect attachments with their native multimodal input or ordinary
  filesystem tools, while follow-up turns keep access to the same path.
- The per-team container and volume remain the tenancy boundary; no host path is
  mounted into an agent and no attachment URL crosses teams.
- Storage is intentionally tied to environment lifetime rather than browser or
  conversation lifetime. The fixed environment quota bounds persistent growth
  while preserving history semantics.
- Oduflow does not extract archives or promise built-in parsing for every
  binary format. Predictable PDF/Office extraction can evolve independently by
  adding readers to the coder image; the attachment transport stays unchanged.
- Content inside an attachment remains untrusted input to a full-access coding
  agent. The feature does not weaken the Docker/team boundary established by
  [[0029-agent-console-and-chat]], but it cannot eliminate prompt injection in
  user-supplied documents.

## History

- 2026-07-22 — introduced authenticated uploads, persistent workspace storage,
  ACP resource/image blocks, composer attachment states and environment-scoped
  cleanup.
