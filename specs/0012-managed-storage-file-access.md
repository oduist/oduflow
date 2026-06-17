# 0012 — Agent file access into managed storage (container files + Docker volumes)

**Status:** Adopted (still in force)
**Type:** Capability
**First introduced:** `abcc525` "add read_file_in_odoo MCP tool" (2026-02-24); extended to volumes by `5cabc2c` "Manage files in volumes" (`8dbdc17`, 2026-03-20, `#12`)
**Key code today:** `docker_ops/odoo_ops.py` (`*_in_odoo` via container exec), `docker_ops/volume_file_ops.py` (`*_in_volume` via helper container), `server.py` (the file tools)

## Context

An AI agent debugging an environment constantly needs to *look at and touch
files that live inside Oduflow-managed storage* — a generated config inside the
running Odoo container, an attachment under the filestore, a seed file dropped
into a service's Docker volume. Two of those stores are awkward to reach:

- **Inside the running container**, files exist but the agent has no shell — its
  only channels are MCP tool calls. Asking it to script `docker exec` itself
  defeats the point of a managed, tool-mediated interface.
- **Inside a Docker volume**, there may be *no running container at all* (a
  volume can be attached to a stopped service, or to none). The data is opaque:
  not on the host filesystem in any stable place, not reachable by exec.

The existing surfaces don't cover this. Code delivery
([[0021-code-delivery-modes]]) moves a repo *into* an environment but says
nothing about reading arbitrary files back out; volume management
([[0007-auxiliary-services-and-volumes]]) is lifecycle of the volume *object*
(create/list/inspect/delete/mount), not its *contents*. And exposing raw shell
access would blow a hole in the tenant isolation the rest of the system works to
preserve.

## Decision

Give agents a **first-class, tool-mediated filesystem reach into managed
storage**, as a small family of MCP tools with consistent semantics and built-in
safety rails — covering both stores that lack a convenient host path:

- **Container files (`*_in_odoo`)** — `read_file_in_odoo`, `write_file_in_odoo`,
  `search_in_odoo` operate on the **running Odoo container** via `exec` (`cat` /
  `sed` ranges / `stat` / `file --mime` / writes), so the agent inspects and
  edits what Odoo actually sees, without a shell.
- **Volume files (`*_in_volume`)** — `read_file_in_volume`,
  `write_file_in_volume`, `search_in_volume`, `delete_file_in_volume` reach into
  an **opaque Docker volume by mounting it into a short-lived helper container**
  (`alpine:latest`) at a fixed mount point, running the operation, and discarding
  the helper. This makes a volume's contents inspectable/editable even when
  nothing else has it mounted.
- **Safety is part of the contract, not the caller's job.** Paths are normalised
  and confined to the mount point (**path-traversal rejected**), reads do
  **binary detection** and **size/line-range bounds** (the same bounded-output
  discipline as [[0017-mcp-tool-execution-output-cache]]), and writes stream in
  via a tar archive rather than shelling out interpolated content.

## How it works (macro)

- **Pick the channel by where the data lives.** If the bytes are inside a live
  container, exec is the cheapest path and shows exactly the container's view. If
  they're inside a volume, a disposable helper container is the *only* reliable
  way to reach them, so the tools spin one up, bind the volume, act, and tear it
  down — the volume is the durable thing, the helper is throwaway.
- **One mental model for the agent.** Read / write / search behave the same
  whether the target is `_in_odoo` or `_in_volume`; the agent thinks "file in
  managed storage", not "exec vs. mount plumbing".
- **Confined by construction.** Every path resolves under a single mount root and
  anything climbing out (`..`) is refused, so a file tool can only touch the
  storage it was scoped to — preserving the per-tenant boundary the orchestration
  layer enforces elsewhere.

## Consequences

- Closes the agent's biggest blind spot: it can now *see* generated configs,
  filestore contents, and volume seed data directly through the tool surface,
  instead of guessing or driving a raw shell — squarely serving the AI-first goal
  of [[0001-mcp-orchestrated-ephemeral-per-branch-environments]].
- The helper-container trick turns **opaque volumes into an inspectable
  filesystem** without requiring a service to be running, which is what made the
  volume side worth a distinct capability rather than a footnote on volume
  management.
- The safety rails (traversal confinement, binary/size bounds, tar writes) keep a
  broad new power from becoming an isolation or output-blowup hazard, consistent
  with the rest of the system's guardrail-first posture.

## History

- `abcc525` (2026-02-24) — `read_file_in_odoo`: first direct file reach into a
  running container.
- `44810aa` (2026-03-02) — the MCP tools refinement
  ([[0017-mcp-tool-execution-output-cache]]) adds `write_file_in_odoo` and
  `search_in_odoo`, rounding out container-file access.
- `5cabc2c` / `8dbdc17` (2026-03-20, `#12`) — `read_/write_/search_/delete_file_in_volume`:
  file access *inside* Docker volumes via a mounted `alpine` helper container,
  with path-traversal protection, binary detection, and tar-stream writes.
