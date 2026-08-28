# 0051 — Built-in remote CLI over the live FastMCP tool surface

**Status:** Adopted
**Type:** Architecture — delivery mode / significant CLI capability
**First introduced:** this change (2026-08-29)
**Key code today:** `client.py` (remote schema-driven FastMCP client), `server.py` (`oduflow client` dispatch), `scoped_access.py` (server-authoritative single-environment policy)

## Context

Oduflow had two ways to execute a registered tool. Native MCP clients connected
to the HTTP or stdio transport, while `oduflow call` invoked the tool function
locally in the server package. The local command is useful on the Docker host,
but it loads local configuration and cannot operate a remote Oduflow instance.
Shell scripts, CI jobs, operators, and coding-agent skills therefore had to
configure a full MCP integration or use the dashboard's partial REST API.

The REST API is intentionally shaped around dashboard workflows and does not
mirror the complete development tool surface. Reimplementing every MCP tool as
a second REST contract would duplicate argument schemas, error semantics,
scoped authorization, and future tool additions. It would also encourage agents
to receive the dashboard `ui_password` instead of the narrower per-environment
credential introduced by [[0028-scoped-environment-mcp-access]].

## Decision

Add `oduflow client <tool>` as an official remote CLI. It connects through
FastMCP to the exact endpoint supplied in `ODUFLOW_MCP_URL`, authenticates with
the Bearer credential in `ODUFLOW_MCP_TOKEN`, discovers the endpoint's live
`tools/list` schemas, and calls the selected tool. `oduflow call <tool>` remains
the separate local in-process path.

The remote CLI does not carry a hard-coded copy of the tool catalogue. It uses
the server schema for visibility, argument names, basic types, required fields,
and help output. Consequently a full `/mcp` endpoint exposes team-wide tools,
while `/mcp/<env>` exposes only the default-deny scoped allowlist and omits
`env_name`; the client does not duplicate or weaken that policy.

## How it works

The client accepts either kebab-case named flags or one JSON argument object.
It converts scalar and JSON values according to the advertised schema and sends
the resulting dictionary through `FastMCP.Client.call_tool`. Required
`env_name` defaults to an explicit client override, `ODUFLOW_ENV_NAME`, or the
current Git branch. `create_environment` additionally defaults its required
`branch` to the current Git branch; tools that deploy long-lived targets, such
as `create_production`, always require the branch explicitly. On a
scoped endpoint the server removes `env_name` from the schema and injects the
URL-bound environment after authorization, so the client performs no target
selection.

Connection options are parsed before the tool name; everything after the tool
name belongs to its live schema. The command is dispatched before Oduflow loads
local Settings, so a client installation needs neither the remote server's TOML
nor Docker access. Human output preserves returned text and cached-output hints;
`--json` emits the complete MCP result for automation. Authentication,
transport, schema, and tool failures produce non-zero process exits.

## Consequences

- Operators and CI can drive any current Oduflow tool remotely with the same
  package and semantics as native MCP clients, without a parallel REST API.
- Coding-agent skills can use one deterministic CLI and avoid loading the full
  Oduflow tool catalogue into the model context; they can refresh workflow
  guidance through `oduflow client get_agent_instructions`.
- Scoped URLs and per-environment tokens remain the preferred agent credential:
  tool visibility and target injection are enforced by the server exactly as
  described in [[0028-scoped-environment-mcp-access]].
- Every CLI invocation performs MCP initialization and live tool discovery.
  This adds a small round trip but prevents stale client schemas and makes new
  tools available without a client release.
- The Oduflow Python package remains larger than a minimal standalone client,
  but keeping client and server on the same FastMCP dependency avoids another
  package, release process, and compatibility matrix.

## Evolution

Schema caching or generated shell completion may be added later if repeated
discovery becomes measurable. They must remain advisory: the authenticated
server response is always the source of truth for available tools and scope.

## History

- 2026-08-29 — built-in schema-driven FastMCP client and `oduflow client`
  command introduced.
