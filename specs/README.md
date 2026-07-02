# Decision records

Macro-level Architecture Decision Records (ADRs) for Oduflow — the *why* behind
each architectural pillar and major capability, reconstructed from the project's
history and (going forward) written alongside the change that introduces it. See
the "Record Architectural Decisions" section in `AGENTS.md` for the convention.

Each record is `NNNN-slug.md`, ADR-style: Context · Decision · How it works
(macro) · Consequences · (Evolution) · History. Macro altitude — the reasoning a
future reader needs, not the diff.

**Numbering is chronological** by the date each decision was first made, so the
catalogue reads as the project's evolution timeline. New records take the next
number for *their* place in time; the date column below is the defining-decision
date (a record's own header carries the full commit lineage, including any
precursors).

## Index (chronological)

| # | First decided | Decision |
|---|---|----------|
| [0001](0001-mcp-orchestrated-ephemeral-per-branch-environments.md) | 2026-02-05 | MCP-orchestrated ephemeral per-branch Odoo environments (founding architecture) |
| [0002](0002-remote-multi-user-mcp-access.md) | 2026-02-05 | Remote, multi-user MCP access over streamable HTTP |
| [0003](0003-database-templates-and-filestore-isolation.md) | 2026-02-06 | Database templates + filestore isolation (copy vs fuse-overlayfs) |
| [0004](0004-stable-addressing-port-registry-and-traefik.md) | 2026-02-07 | Stable environment addressing: persistent port registry + Traefik routing |
| [0005](0005-web-dashboard-and-rest-api.md) | 2026-02-07 | Web dashboard + REST API + interactive consoles |
| [0006](0006-git-driven-change-classification.md) | 2026-02-08 | Git-driven change classification → automatic install/upgrade/restart |
| [0007](0007-auxiliary-services-and-volumes.md) | 2026-02-11 | Auxiliary services, presets, and Docker volumes |
| [0008](0008-licensing.md) | 2026-02-12 | Licensing: Polyform Noncommercial + in-app license activation |
| [0009](0009-agent-guidance-system.md) | 2026-02-13 | Agent guidance system: editable MCP instructions + Odoo version dev guides |
| [0010](0010-extra-addons-repositories.md) | 2026-02-13 | Extra addons repositories (bare clones + per-environment worktrees) |
| [0011](0011-per-user-git-credentials.md) | 2026-02-20 | Per-user git credentials and repository authentication |
| [0012](0012-managed-storage-file-access.md) | 2026-02-24 | Agent file access into managed storage (container files + Docker volumes) |
| [0013](0013-per-environment-db-credentials-and-sanitization.md) | 2026-02-24 | Per-environment PostgreSQL credentials + two-tier database sanitization |
| [0014](0014-team-based-multi-tenancy.md) | 2026-03-01 | Team-based multi-tenancy (replacing instance-based isolation) |
| [0015](0015-granular-locking.md) | 2026-03-01 | Granular locking: per-branch / per-team / system locks |
| [0016](0016-configuration-model.md) | 2026-03-01 | Configuration model: `oduflow.toml` + repo-level `.oduflow/` config |
| [0017](0017-mcp-tool-execution-output-cache.md) | 2026-03-02 | MCP tool execution model: server-side output cache + `read_output` |
| [0018](0018-onboarding-stdio-default-auto-init.md) | 2026-03-04 | Onboarding: stdio default transport + auto-init on startup |
| [0019](0019-telemetry.md) | 2026-03-11 | Anonymous usage telemetry |
| [0020](0020-authentication-oauth.md) | 2026-03-13 | Authentication for MCP HTTP: GitHub OAuth → self-hosted OAuth Authorization Server |
| [0021](0021-code-delivery-modes.md) | 2026-06-12 | Code delivery modes: `repo_url` git push vs `local_path` live-mount + `pull_and_apply` guardrail |
| [0022](0022-engineers-console-design-system.md) | 2026-06-12 | The Engineer's Console: dashboard design system + lifecycle automation |
| [0023](0023-startup-data-migrations.md) | 2026-07-02 | Startup data migrations (Odoo-style upgrade steps) |

## Design docs

Forward-looking feature design docs also live here, named by date:

- [`2026-06-08-mkdocs-material-redesign-design.md`](2026-06-08-mkdocs-material-redesign-design.md) — docs-site (MkDocs Material) redesign.
