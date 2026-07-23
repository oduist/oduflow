# Changelog

## Unreleased

### Features

- **Versioned coder runtime contract** — hosted agents now use the immutable `oduist/oduflow-coder:0.2.3` image instead of a rolling `:latest` tag. The publish workflow emits only versioned multi-architecture tags, legacy official `:latest` configuration resolves to the release's pinned image, and Oduflow pulls a changed image before replacing a working container. Container recreation is derived from the actual Docker run specification rather than a manually bumped runtime epoch.
- **Shared immutable extra-addons checkouts** — development environments using the same extra-addons commit now mount one persistent team-level checkout instead of materialising identical per-environment worktrees. Branch updates create a new SHA-keyed checkout and `pull_and_apply` switches only the requested environment, preserving independent database state; production keeps private worktrees for rollback. Existing environments migrate lazily on their next sync, and cached revisions remain until the extra repository is deleted.

### Dashboard

- **Agent Chat conversation history** — each environment and agent now keeps up to 20 recent conversations in MRU order, titled from the first user prompt. The new **History** menu resumes a selected conversation through ACP `session/load`, preserves legacy sessions without a migration, and recovers safely when an adapter cannot load an older session.

### Bug Fixes

- **Agent Chat image uploads keep their filesystem path** — small images still
  reach multimodal agents as inline visual input, but now also retain the
  persisted `/workspace/.oduflow-uploads/...` resource link. Claude and Codex
  can therefore use the exact uploaded file with shell and Odoo tools instead
  of guessing temporary upload locations.
- **Hosted agents use the reachable scoped MCP endpoint** — Agent CLI and Agent Chat now use the team's public HTTPS `/mcp/<environment>` URL in Traefik mode (and an explicit `oauth_base_url` in port mode), matching the dashboard's **MCP Access** dialog. Local port-mode deployments retain the `host.docker.internal` fallback.
- **Claude Agent Chat explains rejected credentials** — provider credentials copied into `[team.*.agent_env]` now have surrounding whitespace removed before they reach the coder container. When Claude ACP returns a recognizable authentication failure, Agent Chat preserves the provider error and adds recovery specific to the active setup-token, API-key, or interactive-login mode. Oduflow still fails closed instead of silently switching accounts or billing methods.
- **claude.ai OAuth connector reaches the authorization endpoint** — a claude.ai custom connector derives its OAuth endpoints path-relative to the MCP URL (requesting `https://<host>/mcp/authorize` and `/mcp/token`) instead of following the root endpoints advertised by discovery. The outer scoped-access shim treated `authorize`/`token` as an environment name and rewrote the request onto the auth-protected `/mcp` endpoint, so the flow failed with `401 invalid_token` before it could start. Oduflow now routes the reserved OAuth/discovery sub-paths requested under `/mcp/` (`/mcp/authorize`, `/mcp/token`, `/mcp/register`, `/mcp/.well-known/*`) to the real root routes. Scoped `/mcp/<env>` connectors remain Bearer-only.

### Security

- **OAuth `client_id` is no longer the secret** — a team's OAuth `client_id` is now the non-secret identifier `team_<id>` (e.g. `team_1`); the team's `auth_token` is the `client_secret` and remains the issued access token. Previously `client_id == client_secret == auth_token`, so the secret appeared in the `/authorize` query string (and thus in server logs, browser history, and the `Referer`). The secret now travels only in the POST `/token` body. **Breaking:** re-enter any existing claude.ai connector as `Client ID = team_<id>`, `Client Secret = auth_token`.
- **OAuth issues independent, expiring, revocable tokens** — completing the previous item, the OAuth Authorization Code / refresh exchange now mints an *independent, opaque* `access_token` (with a rotating refresh token) instead of handing back the team's `auth_token`. A minted access token expires (~1h) and can be revoked via the now-enabled `/revoke` endpoint; using a refresh token rotates the pair so a leaked refresh token is single-use. The OAuth client (claude.ai/IDE) therefore never stores the team's long-lived master secret. Minted tokens are persisted (`oauth_token_store.py`) so live connections survive an Oduflow restart. The `auth_token` still works as a non-expiring direct Bearer credential for curl/CLI, and per-environment tokens remain Bearer-only. (#83)

### Documentation

- **Record the versioned coder-image contract** — specs/0040 documents immutable image publication, server/image version coupling, safe replacement ordering, and removal of the rolling `:latest` channel.
- **Document the short transport flag** — HTTP server startup examples now show `oduflow -t http` / `uvx oduflow -t http` as the short form of `--transport http`.
- **Record the shared extra-addons cache decision** — specs/0039 documents SHA-keyed checkout sharing, isolation from moving branches, persistent cache lifecycle, and why production remains on private worktrees.

## v1.67.0

### Features

- **Restricted HTTP path routing for services** — auxiliary services can now expose a curated set of `routes` alongside (and mutually exclusive with) the existing catch-all `port` model. Restricted routes use segment-safe Traefik rules, work for both bridge and host networking, can optionally strip prefixes, and leave every undeclared path at Traefik's 404 so a service no longer has to publish its whole HTTP surface. The route configuration is preserved through presets, live inspection, updates, MCP, REST, and the dashboard. (#126)

### Bug Fixes

- **Dependency changes are applied on Sync** — a changed `requirements.txt`, `.oduflow/requirements.txt`, or `.oduflow/apt_packages.txt` during `pull_and_apply`/Sync now reinstalls the apt/pip dependencies into the running container and restarts it, instead of being misreported as an XML/JS-only browser refresh. The classifier previously ignored these files, so a dependency-only change silently did nothing. Reinstall runs before any module install/upgrade so new libraries are importable; packages *removed* from the file are not uninstalled until the container is rebuilt via `update_environment`. (#127)
- **Real client IPs behind Traefik** — access logs and the login rate-limiter recorded the Traefik container IP for every request instead of the real client, because uvicorn only trusts `X-Forwarded-For` from `127.0.0.1` by default. Oduflow now passes `proxy_headers=True` with a `forwarded_allow_ips` trust list — the stable Traefik Docker-network CIDRs in `traefik` mode, and nothing in `port` mode so a direct client cannot spoof its IP. The login rate-limiter now throttles per real client. (#123, #124)
- **Bounded MCP HTTP graceful shutdown** — HTTP transport now applies a 10s Uvicorn graceful-shutdown timeout so long-lived MCP StreamableHTTP streams no longer keep server restarts waiting indefinitely. (#122)

### Security

- **Hardened proxy-header trust and credential redaction** — forwarded headers are trusted only from loopback plus the stable Traefik Docker-network CIDRs, failing closed for empty team maps and when Docker-network trust cannot be established; embedded repository credentials are now redacted from non-auth git clone failures; and a staged template filestore replacement that fails to install preserves the previous filestore. (#124)

### Documentation & Testing

- **Restricted service-routing decision record** — specs/0033 documents the restricted HTTP path-routing model for services, its security boundary, and the trade-offs versus catch-all port exposure. (#126)
- **Superseded PR test runs are cancelled** — `tests.yml` now uses a per-ref `concurrency` group with `cancel-in-progress`, so pushing a new commit to an open PR cancels the still-running test workflow for that ref while keeping `main` protected by pre-merge CI. (#128)

## v1.66.0

### Features

- **Traefik certificates available to auxiliary services** — in Traefik TLS mode every created or recreated service receives the exact `oduflow-traefik-acme` volume at `/etc/traefik` read-only, with no per-service TLS flags or wildcard allowance for other system volumes. Existing services gain the mount on their next update.
- **Database-only Odoo template import** — `oduflow import-template` and `import_template_from_odoo` now accept `--without-filestore` / `without_filestore=True`, requesting a PostgreSQL custom-format dump without filestore files and deriving template metadata after restore.
- **Attach separate template filestores** — new `oduflow attach-filestore` CLI and `attach_filestore` MCP tool attach or replace a template filestore after a database-only import. Sources can be local directories, zip/tar archives, `rsync://` URLs, or SSH rsync paths; wrapper directories such as the database name are auto-detected and live overlay env changes are preserved by default.

### Bug Fixes

- **Invalid service volume updates are non-destructive** — `update_service` now resolves the complete candidate volume configuration before stopping the running container, so a missing or reserved volume no longer deletes the service before returning an error.
- **Generated PostgreSQL passwords are safe for the Odoo entrypoint** — per-environment passwords that randomly began with `-` were parsed as another CLI option, leaving `--db_password` without a value and putting the Odoo container into a restart loop. Password generation now retries that rare token shape.
- **Odoo template import now refuses existing template names** — `import-template` fails before DB listing or backup download if the target template directory or template database already exists, avoiding accidental overwrites.
- **Database-only template import can restore newer custom dumps** — when the shared PostgreSQL container's `pg_restore` cannot read a custom dump archive version, Oduflow converts it to plain SQL with a temporary `postgres:17` helper container and restores that SQL through the shared database container.

### Documentation & Testing

- **Implicit service certificate-store decision record** — specs/0032 records the deliberate shared-read, read-only ACME mount and its trust-boundary consequences.

## v1.65.0

### Features

- **Per-team OAuth issuer with host-relative discovery** — in traefik mode, each team's OAuth Authorization Server derives its issuer from the incoming Host, so each team runs OAuth on its own already-certificated hostname with no need for a central `oauth_base_url`. `OduflowOAuthProvider` makes discovery metadata host-relative, `HostRelativeAuthChallenge` rewrites 401 challenge origins, and `[routing].hostname` is now validated per-mode (shared default deleted in traefik). (#112)

### Security

- **Hardened web-UI and MCP surfaces** — fresh installs and upgrades now bootstrap a `ui_password` so the dashboard is never served unauthenticated; the interactive shell, SQL, agent, and service-creation surfaces are fail-closed. Closed two SSRF vectors (`http_request_to_odoo` path rewrite and `setup_repo_auth` loopback/link-local validation), removed the shared-PostgreSQL-superuser fallback so tenants cannot cross databases on legacy envs, added validation guards and escaped output across the surface. (#110)
- **psql WebSocket branch validation** — `ws_sql_terminal` now validates the branch path param before lookup to close an opaque resolution path in scoped-credentials. (#114)

### Bug Fixes

- **Overlay filestore no longer duplicated into each environment** — `_ensure_user_site_packages` was recursively chowning `.local/share`, causing fuse-overlayfs to copy the entire template filestore (~10 GB) into each env's upper layer, defeating overlay space savings. Now chowns only the pip dirs non-recursively, keeping filestore overlay-bound. (#113)
- **Overlay unmount now works on Ubuntu 24.04 with enforced fusermount AppArmor profile** — switched mount cleanup to try clean `umount` first (not mediated by the fusermount3 profile on Ubuntu 24.04+), falling back to `fusermount`/`fusermount3` helpers and lazy `umount -l` only as a last resort. (#113)
- **Multi-team UI password provisioning for newly-added passwordless teams** — `_ensure_web_ui_password` was using `any()` so a multi-team config with one team already set would skip provisioning for others, locking them out. Now uses `all()` to provision every team and enforce that all are set at startup. (#114)
- **Template clone now reassigns objects owned by any non-env role** — when creating an environment from a template imported via `import_template_from_odoo`, objects owned by roles other than the template env's own role were not reassigned, leaving the new env unable to touch them. Now reassigns all non-env-role-owned objects unconditionally. (#111)
- **Native Odoo neutralize skipped for Odoo 15** — the official `odoo:15.0` image does not include the `odoo neutralize` CLI, so Oduflow now skips it for Odoo 15 and tries only for Odoo 16+, keeping custom `.odoo_sanitize` scripts as the baseline. (#109)

### Documentation & Testing

- **Per-team OAuth decision record** — specs/0020 captures the evolution from a static `oauth_base_url` to host-relative issuer discovery. (#112)

## v1.64.0

### Features

- **Odoo.sh addons-path import** — extend push-based import to carry over the addons-path Odoo.sh ran with, supporting Enterprise, Themes, and extra repos. The client detects the live addons-path, classifies each entry, tars private repos and announces reachable extras to the server. Also adds `rename_template` MCP tool, REST endpoint, and dashboard button to rename template directories and their PostgreSQL backing databases. (#108)
- **Chunked Odoo.sh import uploads** — split large SQL dumps and addon tarballs into resumable chunks to pass proxy body-size limits (e.g., Cloudflare's 100 MB cap). Resume state is derived from what is already staged on disk. (#108)
- **Local extra-addons repos** — `extra_addons.create_local_repo` seeds a remote-less bare repo from files with a `.local` marker; fetch short-circuits so worktree creation never attempts a remote pull. (#108)

### Dashboard

- **Import acknowledgement gate** — Enterprise/Themes/Extra addon checkboxes now gated behind a licensing acknowledgment, linking to `oduflow.dev/odoo-sh-import-notes` for documentation. (#108)

### Documentation & Testing

- **Odoo.sh import decision record** — specs/0030 captures the architecture and rationale for the resumable, push-based import model. (#108)

## v1.63.0

### Features

- **Native Odoo database neutralization** — sanitized template-based environments now run Odoo's own `odoo-bin neutralize` inside the serving container after auto-installed modules are present, then continue through the existing custom `.odoo_sanitize` scripts. This gives copied databases Odoo's module-aware baseline protections for outgoing mail, crons, payment providers, connectors and webhooks without adding a new config knob; failures remain warning-only so provisioning is not blocked. (#107)

### Dashboard

- **Unlicensed badge signal** — the unlicensed license badge now plays a brief red edge pulse and jolt once on dashboard load as a registration nudge, while keeping the text as the actual status signal and skipping the animation entirely for reduced-motion users. A follow-up tightened the animation cleanup path so reduced-motion sessions do not retain unused listeners. (#104, #105)

### Documentation

- **Release workflow captured** — a project-scoped `/publish-release` skill now records the Oduflow release flow: analyze commits since the last tag, propose the version/changelog/title for sign-off, bump and push the release commit, publish the GitHub Release that triggers PyPI and Docker, then verify artifacts. (#106)

## v1.62.0

### Features

- **HTTP-only Traefik mode** — a new `[routing].tls` toggle (bool, default `true`). With `mode = "traefik"` and `tls = false`, Traefik listens on plain HTTP `:80` only — no HTTP→HTTPS redirect and no Let's Encrypt/ACME — for running behind an upstream that already terminates TLS (e.g. a Cloudflare tunnel). Environment/service routers bind to the `web` entrypoint without TLS, the `acme_email` requirement is lifted, and `forwardedHeaders.insecure` lets the upstream's `X-Forwarded-Proto: https` through so cookies stay `Secure` and public URLs stay `https://`. `_ensure_traefik` self-corrects on a flipped `tls` setting by recreating the container. (#103)

### Bug Fixes

- **Agent checkout removal guard** — `_agent_remove_env` now refuses when an environment name slugifies to an empty string, which would resolve the checkout dir to `/workspace/` itself and let `rm -rf` wipe every environment's checkout from the shared volume (matching the guard `clone-env.sh` already had on the create side). (#100)
- **Agent session file permissions** — `agent_sessions._save` now creates its temp file `0600` from birth via `os.open` (the mode carries over through `os.replace`), closing the brief default-permissions window that existed with chmod-after-rename. (#100)
- **Surfaced degraded agent modes** — non-fatal `_chat/notice` frames now render as system lines in Agent Chat, making two previously silent degraded modes visible: an environment without a scoped MCP token, and Codex chat's missing Oduflow MCP wiring. (#100)

### Documentation

- **Docs synced with code** — a documentation audit corrected several drifts: the retired "PolyForm Noncommercial 1.0.0" license name → BUSL-1.1 in `llms.txt`/`llms-full.txt`; `auto_delete_hours` default corrected to `0` (opt-in) across pages; the removed `oduflow init` command/flag dropped; `[server].allow_local_path`, `allow_insecure_http` and `[routing].hostname` documented; and the v1.61.0 coding-agent feature fully documented (new `docs/agent.md`, installation/web-api pages, `llms.txt`/`llms-full.txt`). (#101, #102)

## v1.61.0

### Features

- **Per-team coding agent — Agent CLI & Agent Chat** — an opt-in hosting feature that runs one coding-agent container per team (`oduflow-{team}-agent`) on the team's isolated network, with persistent HOME/workspace volumes and one git checkout per environment. The agent drives environments only through the Oduflow MCP server (git push → `pull_and_apply`). Two front-ends ship: **Agent CLI**, the agent's own TUI in xterm.js over a WebSocket ↔ `docker exec` bridge; and **Agent Chat**, a framework-free browser client talking to the agent's ACP adapter, with a durable session per (environment, agent) that resumes via ACP `session/load` and minimizes to a dock so several chats run in parallel. Configured statically in `oduflow.toml` (`agent_enabled`, `agent_default`, `[team.X.agent_env]`); the container carries a config-hash label and is recreated automatically on drift. The agent UI is hidden for live-mount environments. (#99)
- **Scoped per-environment MCP tokens for agents** — the team `auth_token` never enters the agent container; each console/chat session injects the environment's scoped `/mcp/<env>` token (default-deny dev-loop allowlist) into its own `docker exec` environment via a `${ODUFLOW_MCP_TOKEN}` placeholder, so no secret lands on disk and a leaked session credential grants only the one environment it already controls. (#99)
- **Coder image (`oduist/oduflow-coder`)** — a new merge-gated `publish-coder.yml` workflow publishes the agent image, which redistributes only Apache-2.0 software (Codex CLI + codex-acp); Claude Code and its ACP adapter are installed at first container start onto the persistent home volume. (#99)

### Bug Fixes

- **Traefik dynamic config now actually loads** — the per-team hostname→dashboard routing config was written as `.json`, which Traefik's file provider rejects ("unsupported file extension"), so it never loaded. It is now written as `oduflow.yml` (JSON is valid YAML, so no new dependency), placed under `etc_dir` (`/etc/oduflow` when writable, else `~/.oduflow/conf`) with the parent directory created first so a first traefik-mode start no longer crashes on macOS. Migration 0005 removes a still-running Traefik container that mounts the old `.json` path (the ACME volume persists, so certificates survive) and system init recreates it with the corrected path. (#98)

## v1.60.0

### Features

- **Scoped single-environment MCP access** — a new `/mcp/<env>` endpoint exposes only the in-environment dev-loop tools (sync, install/upgrade modules, tests, Odoo shell, SQL, file read/write/search, HTTP request, logs, info, restart) and denies every lifecycle/system tool. Access is isolated by a per-environment token generated at create time and stored in the `oduflow.mcp_token` container label; the Secret Key works as a Bearer token or an OAuth client credential. A dashboard **More → MCP Access** modal surfaces the URL and Secret Key. (#96)

### Dashboard

- **Minimize-to-dock windows** — the Log, Console (Odoo shell) and SQL Console modals gain a minimize button that docks the window as a chip in the system bar. Minimizing only hides the overlay, so the WebSocket, xterm buffer and log state stay alive and restoring resumes the exact session; reopening a window of the same type replaces the docked session without leaking its WebSocket, and focus returns to the launching control across the cycle. (#95)

### Documentation

- **Per-version Odoo development guides (15–19)** — `get_odoo_development_guide` now serves concise, self-contained per-version cheat sheets covering the key conventions and breaking changes for each release, plus a "Migrating a module to this version" block (migration scripts, pre/post split, openupgradelib helpers) for 16–19. Version-boundary facts were verified against `odoo/odoo` 16.0–19.0. (#97)

## v1.59.0

### Features

- **Multi-tenant hosting: hard tenant isolation** — a hosting-grade tenancy pass so environments running arbitrary client code cannot reach across teams. Container names are now team-scoped (`oduflow-{team}-{env}-{type}` and `oduflow-{team}-svc-{name}`); each team gets its own Docker network (only the shared PostgreSQL and Traefik bridge across teams), its own PostgreSQL tablespace (`oduflow_team_{id}`), and default per-container memory/pids limits auto-derived from host size. Odoo-style startup migrations (recorded in `migrations.json`) retrofit existing installs in place — renaming containers, moving tablespaces, and re-homing networks — with no manual steps and a clean resume after a partial run. (#91)
- **Per-team quotas** — `db_quota_gb` (default 50, 0 = off) caps the combined size of a team's PostgreSQL databases, checked before any operation that creates a new database; `disk_quota_gb` (default 0) is enforced on XFS with project quotas, giving a team's files and its PG tablespace one project ID and a single kernel limit. (#91)
- **Per-environment usage stats** — environment cards show DB size / disk usage after the CPU/RAM stats with a per-card refresh control, backed by a cached storage-stats subsystem and a `GET /api/usage` + `POST /api/usage/refresh` REST surface that external billing/quota tooling can consume per team. (#91)
- **Strict HTTP team resolution** — the single-team fallback now applies to stdio only; in HTTP mode an unresolved request is rejected unless `allow_insecure_http` is set, and multi-team HTTP requires an `auth_token` for every team at startup. (#91)

### Bug Fixes

- **Review cleanups** — dropped dead team-guard branches now that `team` is a required argument, extracted the duplicated Traefik-label helper to module level, and `fsync` before the atomic storage-cache rename so a crash mid-write cannot leave an empty file. (#93)

## v1.58.0

### Licensing

- **Relicense to Business Source License 1.1** — replaces PolyForm Noncommercial 1.0.0 with BUSL-1.1 (the canonical MariaDB text). The Additional Use Grant keeps production use free forever for non-commercial purposes (evaluation, education, personal projects, non-profits) and defines three commercial tiers — Individual, Business (internal use), and Integrator (Odoo services to third parties) — matching the existing license-key types. Each release converts to MPL 2.0 four years after publication, per standard BSL mechanics. (#90)

## v1.57.0

### Features

- **Neutral dashboard create buttons** — the `.btn-create` primary buttons move from a solid Console Blue fill to the outline treatment (surface fill, hairline border, ink text) that reveals a blue border, text and 10% tint on hover/focus, matching the Sync/Logs actions; weight 600 and the larger radius keep them distinguishable without a resting fill. The destructive confirm keeps its solid red. (#87)

### CI

- **Automatic Docker image publishing** — a new `docker.yml` workflow builds and pushes `oduist/oduflow:<version>` plus `:latest` (linux/amd64 + arm64, via Buildx/QEMU) to Docker Hub on every published GitHub Release — the same trigger as the PyPI publish. The version comes from the release git tag; `latest` is skipped for pre-releases. (#88)

## v1.56.0

### Features

- **Import templates from Odoo.sh** — Odoo.sh blocks `pg_dump` and `/web/database/manager`, so import is inverted: a push-based, resumable shell client (served at `/import-odoo.sh`, launched from the dashboard "Import from Odoo.sh" button) rides the platform's own daily backup, streaming the SQL dump as-is and the filestore tar'd per hash-directory chunk to short-lived (15 min), token-authed ingest endpoints that stage and restore it through the existing template machinery. Resume state is derived from what is already staged on disk, so a fresh token minted after the previous one expired continues the transfer where it stopped. (#85)

### Security

- **Odoo.sh import hardening** — closed an auth-bypass where the public ingest prefix also exposed sibling routes like template deletion; uploads now land in a staging dir (no fake-resume over an existing template, no writes into a mounted overlay lower layer); chunk extraction is atomic (a truncated upload is never treated as finished on resume); and the client resolves http→https redirects up front, streams with `curl -T` instead of buffering whole payloads in RAM, and fails fast on non-JSON responses instead of crashing with a raw traceback. (#89)

### Dashboard

- **Import from Odoo.sh modal** — widened to 80vw, with the input placeholder updated to `oduist-prod`. (#92, #94)

## v1.55.0

### Security

- **Security & bug audit remediation (17 fixes)** — a broad hardening pass across the tenancy and request surfaces: `template_name` is now validated at both the filesystem and PostgreSQL-identifier sinks (blocking path traversal and SQL injection); env DB roles no longer receive cluster-superuser membership (closing a `SET ROLE` → `COPY … TO PROGRAM` RCE) and the post-clone ownership transfer was made comprehensive; reserved `oduflow-*` volumes can no longer be mounted by a service; SSRF guards were added for `import_from_odoo` and git remote URLs; zip-slip/tar extraction is now containment-checked; `get_client` raises a catchable `FlowError` instead of `SystemExit`; the web create handler no longer releases a lock it does not own; and overlapping team port ranges are rejected in `Settings.validate()`, with constant-time secret compares and atomic credential writes. (#82)

### Bug Fixes

- **Destructive auto-delete off by default** — `auto_delete_hours` now defaults to `0` (opt-in) so a stopped environment is never silently deleted; the non-destructive 48h auto-stop stays on, and enabling auto-delete logs a prominent warning naming the consequence and how to disable it. (#82)
- **`oduflow cleanup --force` actually removes orphans** — `cleanup_orphans` now passes the per-team settings to `_unmount_filestore`, fixing an `AttributeError` that was silently swallowed while the command reported success but removed nothing. (#82)

## v1.54.0

### Bug Fixes

- **Repo `odoo.conf` changes now actually apply** — a changed `.oduflow/odoo.conf` is now reconstructed (merged `addons_path`, stripped `db_*`) and copied into the container before the restart during `pull_and_apply`, in both the restart and upgrade-then-restart paths. Previously (#69) it only triggered a plain restart, which reused the stale `/etc/odoo/odoo.conf` copy and silently ignored the new config; the regeneration only ever ran on a full `update_environment` recreate. (#81)

## v1.53.0

### Features

- **`addons/` sub-directory auto-detected** — when the main repo keeps its modules under a top-level `addons/` directory, the generated `odoo.conf` now points `addons_path` at `/mnt/extra-addons/addons` instead of the repo root. Odoo scans `addons_path` non-recursively, so the root mount alone would miss modules nested under `addons/`; detection is automatic and falls back to `/mnt/extra-addons` otherwise. (#80)
- **Complete bundled config template** — the shipped `oduflow.toml` now includes every option `Settings.from_toml` reads, adding the `[lifecycle]` section (`auto_stop_hours`, `auto_delete_hours`) and a commented `routing.hostname` fallback, so no knob is left undocumented. (#86)

## v1.52.0

### Dashboard

- **`Created` label for services** — service cards now show a Created timestamp. (#79)

### Documentation

- **Release process** — documented the mandatory GitHub Release step (the event that actually triggers the PyPI and Docker publishes) in `AGENTS.md`.

## v1.51.0

### Features

- **Auto-generated DB superuser password** — on a fresh install the bootstrap injects a random `secrets.token_urlsafe(24)` password into the generated `oduflow.toml` `[database]` section, so every install gets a unique PostgreSQL superuser secret. The bundled template no longer ships the hardcoded `password = "odoo"`; an explicit `[database].password` still overrides, and existing configs are never rewritten. (#74)
- **Auto-tuned PostgreSQL config** — first-time system init now generates `postgresql.conf` from the host's detected CPU/RAM (new `pg_tune.py`) instead of copying a static file hardcoded for "2 vCPU / 4 GB / HDD". The tuning is lean and SSD-oriented for a host running many lightweight single-user Odoo environments (e.g. `shared_buffers` ≈ 10% of RAM, floored at 128 MB and capped at 1 GB); the generated file is marked `# KEEP` so `oduflow upgrade` never overwrites it, and falls back to the static bundled config on any failure. The template `db_maxconn` was lowered from 16 to 8. (#67)
- **Restart on repo `odoo.conf` changes** — changes to the repository-level `odoo.conf` now trigger an environment restart during `pull_and_apply`. (#69)
- **Version logging on init** — the Oduflow version is now logged during system initialization. (#70)

### Dashboard

- **Remove templates** — each card in the Templates tab gains a Remove action (with a danger confirm dialog), wired to a new `POST /api/templates/{name}/delete` endpoint. (#76)
- **Wider logs modal + wrap toggle** — the logs modal now fills 80vw instead of being capped at ~900px, and a new "Wrap" toggle switches long log lines (tracebacks, SQL) between no-wrap and soft-wrap to avoid horizontal scrolling. (#75)
- **Calmer template badges** — the Templates tab's readiness badges are toned down (lower-saturation green fill plus a hairline border) and the `Copy`/`Overlay` mode badge is now a neutral gray chip rather than a success-green one, ending the eye-straining "wall of green". (#77)
- **Branch-header commit & PR links** — the Pull Requests and Commits icon-links moved from the variable-length Repo URL line into the card header next to the branch name, giving them a stable, easy-to-find position. (#73)
- **Copyable container names** — running-environment container names are now click-to-copy via the existing clipboard affordance.
- **Stopped status + service env vars** — environments whose containers are all stopped now report a distinct "stopped" status (amber) instead of "partial", and the redundant exited-container row is dropped; service environment variables moved out of the card into an "Info" modal that lists the real (unmasked) `KEY=value` pairs. (#66)

### Bug Fixes

- **`oduflow call` async tools** — the CLI `call` subcommand printed `<coroutine object ...>` (and a "coroutine was never awaited" warning) for tools wrapped by `@handle_errors`; their awaitable results are now run to completion via `asyncio.run()`. Sync tools are unaffected. (#68)

### Documentation

- **Architectural decision records** — the project's macro-level decisions were reconstructed from git history into ADR-style records under `specs/` (0001–0022) with a chronological index, and a "Record Architectural Decisions" rule was added to `AGENTS.md` so future decisions are captured alongside the change that introduces them. (#72)

## v1.50.5

### Features

- **Light theme** — the dashboard and login page gain a light theme alongside the dark "Engineer's Console" field. It follows the OS (`prefers-color-scheme`) by default; a header toggle cycles **System → Light → Dark** and persists the choice to `localStorage`, applied before first paint so there is no flash. It is built by re-pointing the existing `:root` color tokens — signal colors switch to AA-verified darkened variants on the light field, while the embedded terminal stays dark — and the logo is now transparent so it reads cleanly on either field.
- **Larger dashboard type scale** — 68 hardcoded `px` font sizes were replaced with six semantic rem tokens (`--text-2xs`…`--text-xl`), raising every tier ~+2px (body 13→15px, titles 15→17px, header 20→23px) for readability at density. (#63)
- **Note dialog Delete button** — the environment note editor now has an explicit Delete action. (#63)


## v1.50.4

### Features

- **MCP bootstrap instructions** — the MCP initialize response now tells agents to load `get_agent_instructions` first and then call `get_odoo_development_guide(version=...)` before writing Odoo module code.

## v1.50.3

### Features

- **Git-independent live-mount apply flow** — `local_path` environments now use an Oduflow-owned per-environment file snapshot instead of Git status to detect local changes. Git commits are optional in live-mount mode, non-git folders are supported as-is, and already-applied dirty/untracked files are no longer reported repeatedly.
- **Config-gated live-mounts** — added `[server].allow_local_path` (default: `true`) to enable the single-developer live-mount workflow by default while still allowing operators to disable local bind mounts explicitly.
- **Mode-aware agent instructions** — `get_agent_instructions` now starts with a dynamic code-delivery-mode preface. When a live-mounted environment is active, agents see the local workflow first, including the explicit `install`/`upgrade`/`restart` rules that apply to snapshot-based detection.

### Bug Fixes

- **Live-mount templates in Web UI** — environments created from templates that record `local_path` can now be recreated as live-mounts when `allow_local_path` is enabled, instead of always being rejected as HTTP-only.
- **Local `pull_and_apply` repeat detection** — snapshots advance only after successful apply operations, and stay unchanged when strict guardrails block or install/upgrade fails.

### Documentation

- **Agent guide local workflow** — clarified the split between `repo_url` mode (`commit` → `push` → `pull_and_apply`) and `local_path` live-mount mode (edit local files directly; commits optional; pass explicit actions for database-affecting changes).

## v1.50.2

### Bug Fixes

- **License config path** — license activation now stores `license.key` in the resolved Oduflow config directory instead of hard-coding `/etc/oduflow`; when `/etc/oduflow` is not writable it follows the same `~/.oduflow/conf` fallback as the rest of the config. The dashboard license API now uses the running server settings, so the unlicensed badge still appears when no key is installed. ([fe142ec](https://github.com/oduist/oduflow/commit/fe142ec))
- **CLI startup warning** — suppress the noisy Authlib deprecation warning emitted by FastMCP on startup. ([44872e8](https://github.com/oduist/oduflow/commit/44872e8))

### Documentation

- **MCP tool count** — correct the documented MCP tool count from 43 to 54. ([513a53d](https://github.com/oduist/oduflow/commit/513a53d))
- **Release process** — document that version and changelog updates must be committed before creating the release tag. ([9371be5](https://github.com/oduist/oduflow/commit/9371be5))

## v1.50.1 (since v1.20.1)

### Breaking Changes

- **`rebuild_environment` renamed to `update_environment`** — the tool (and its REST route `/api/environments/{branch}/update`) now also accepts `odoo_image` and `env_vars` to switch the image and replace container environment variables. Called with no arguments it behaves exactly like the old `rebuild_environment` (re-create the container, preserving DB and filestore). ([a13c73a](https://github.com/oduist/oduflow/commit/a13c73a))
- **Removed `template-up` / `template-down` CLI commands** ([8342768](https://github.com/oduist/oduflow/commit/8342768))

### Features

- **Stdio live-mount + explicit `pull_and_apply` guardrail** — `create_environment(local_path=...)` bind-mounts the agent's own checkout live into the container (stdio transport only), so edits apply instantly with no git push/pull; `pull_and_apply` is transport-agnostic and takes explicit `install`/`upgrade`/`restart` actions, with a guardrail that cross-checks the requested action against the detected diff and surfaces non-blocking warnings ([aca9445](https://github.com/oduist/oduflow/commit/aca9445))
- **Environment lifecycle automation** — idle environments auto-stop and long-stopped environments auto-delete after configurable hours (protected environments are exempt); container-level tools auto-wake a stopped environment and prepend a note; activity is tracked per team in `activity.json` ([e992971](https://github.com/oduist/oduflow/commit/e992971))
- **Environment variables for environments** — `create_environment` accepts `env_vars` (comma-separated `KEY=VALUE`) injected on top of the database connection variables; `update_environment` can replace them later. Env vars are persisted on the container, reported by `get_environment_info`, and editable from the web dashboard create form ([a13c73a](https://github.com/oduist/oduflow/commit/a13c73a))
- **Self-hosted OAuth Authorization Server for MCP clients** ([5f32f58](https://github.com/oduist/oduflow/commit/5f32f58))
- **GitHub OAuth support for MCP HTTP transport** ([97c3fc8](https://github.com/oduist/oduflow/commit/97c3fc8))
- **`auto_install_modules` parameter** for environments and templates ([e868d90](https://github.com/oduist/oduflow/commit/e868d90))
- **Docker volume management** — manage persistent service data via Docker volumes, mount external Docker volumes to services, and browse/manage files inside volumes ([6b05f8a](https://github.com/oduist/oduflow/commit/6b05f8a), [a396583](https://github.com/oduist/oduflow/commit/a396583), [5cabc2c](https://github.com/oduist/oduflow/commit/5cabc2c))
- **Host network mode for services** ([d4609e6](https://github.com/oduist/oduflow/commit/d4609e6))
- **`privileged` and `NET_ADMIN` capability options for services** ([edad2ef](https://github.com/oduist/oduflow/commit/edad2ef))
- **`restart_service` and `run_service_command` tools** with increased summary limits ([a2fa612](https://github.com/oduist/oduflow/commit/a2fa612))
- **`update_service` can change any setting** — env, image, port, hostname, host_mode, volumes, privileged, and net_admin ([970aba4](https://github.com/oduist/oduflow/commit/970aba4), [5bb3f7e](https://github.com/oduist/oduflow/commit/5bb3f7e))
- **`get_service_info` tool** ([27ec4c9](https://github.com/oduist/oduflow/commit/27ec4c9))
- **Non-destructive template update for fuse-overlayfs environments** ([1edafff](https://github.com/oduist/oduflow/commit/1edafff))
- **`reload-template --source` flag** for S3/local sync ([2e2e0cd](https://github.com/oduist/oduflow/commit/2e2e0cd))
- **`# KEEP` marker** to protect files from being overwritten during upgrade ([a97e13f](https://github.com/oduist/oduflow/commit/a97e13f))
- **Read repo-level `odoo.conf` from `.oduflow/` directory** ([48eea3e](https://github.com/oduist/oduflow/commit/48eea3e))
- **`created_at` timestamp, editable notes, and preset-based service updates** ([b849551](https://github.com/oduist/oduflow/commit/b849551))
- **odoo.conf `db_maxconn=16`** — bumped from 4, which was too small ([90e7e19](https://github.com/oduist/oduflow/commit/90e7e19))

### Dashboard

- **Engineer's Console redesign** — visual system aligned with oduflow.dev (OKLCH tokens, Outfit + Geist Mono, signal palette); all assets vendored (xterm.js and fonts, no CDN, works air-gapped); state-driven Start/Stop toggle; in-system dialogs replace native `confirm()`/`prompt()`; accessibility (focus traps, ARIA roles, keyboard navigation, ≥44px touch targets); responsive down to 390px; single polling interval; real RAM/CPU metrics on macOS ([e992971](https://github.com/oduist/oduflow/commit/e992971))
- **Session-cookie auth with login form and logout** — replaces the HTTP Basic browser dialog; signed `itsdangerous` session cookies (7-day expiry, persistent server-side secret); Basic auth retained for API/CLI clients ([9e24ab2](https://github.com/oduist/oduflow/commit/9e24ab2))
- **Show git branch in env list** when it differs from the environment name ([ae5441d](https://github.com/oduist/oduflow/commit/ae5441d))
- **`host_mode` checkbox** on service creation and restore forms ([73fbee7](https://github.com/oduist/oduflow/commit/73fbee7))
- **PR and commits icon-links** next to the repo URL ([25017cd](https://github.com/oduist/oduflow/commit/25017cd))
- **Copy DB name on click** ([7f8d654](https://github.com/oduist/oduflow/commit/7f8d654))

### Bug Fixes

- **WebSocket terminal auth** — Console/SQL terminals failed because browsers can't send a Basic auth header on a WS handshake; added a signed cookie auth fallback for HTTP and WebSocket scopes ([9e24ab2](https://github.com/oduist/oduflow/commit/9e24ab2))
- **Greenfield DB init race** — for `template_name=none`, initialize the empty DB in an isolated short-lived container before the serving container starts, avoiding a concurrent `orm_signaling_registry` collision that left `base` uninstalled ([aca9445](https://github.com/oduist/oduflow/commit/aca9445))
- **Orphan PG role on template restore** — restore env-derived templates with `--no-owner` so deleting the source environment can drop its per-environment role ([aca9445](https://github.com/oduist/oduflow/commit/aca9445))
- **Test port flag by Odoo version** — pick `--longpolling-port` vs `--gevent-port` based on the environment's Odoo major version ([79b1c35](https://github.com/oduist/oduflow/commit/79b1c35))
- **`run_odoo_tests` port & execution** — pass `--no-http --workers 0` to avoid an 8069 conflict; override test ports and use `-u` so tests actually run ([6311dc4](https://github.com/oduist/oduflow/commit/6311dc4), [738263e](https://github.com/oduist/oduflow/commit/738263e))
- **Post-clone DB fixup** — transfer object ownership and drop signaling sequences ([205e79f](https://github.com/oduist/oduflow/commit/205e79f))
- **Filter containers by prefix** to isolate test/production environments ([4a50e1c](https://github.com/oduist/oduflow/commit/4a50e1c))
- **Skip submodule recursion on git fetch** to tolerate inaccessible submodules ([5a1a476](https://github.com/oduist/oduflow/commit/5a1a476))
- **Raise `NotFoundError` when deleting a non-existent environment** ([0a1d875](https://github.com/oduist/oduflow/commit/0a1d875))
- **`update_service` URL KeyError; fresh pull on create** ([27ec4c9](https://github.com/oduist/oduflow/commit/27ec4c9))
- **Propagate `cap_add`/`privileged` in `restore_service`** and surface them in `list_service_presets` ([6bca16c](https://github.com/oduist/oduflow/commit/6bca16c))
- **Strip docker volume name prefix** in `resolve_volume_binds` ([2573b17](https://github.com/oduist/oduflow/commit/2573b17))
- **Exclude checkboxes from full-width input styling** in forms ([7f685c9](https://github.com/oduist/oduflow/commit/7f685c9))
- **Pass missing `team` arg** to `_get_used_ports` in `template_up` ([fbfc85e](https://github.com/oduist/oduflow/commit/fbfc85e))
- **Recreate live-mount environments as live-mounts** — record `local_path` in template metadata and restore it on create ([9e24ab2](https://github.com/oduist/oduflow/commit/9e24ab2))

### Performance

- **`reload-template`** — pipe `gunzip` into `psql` to avoid a temp file ([0bd0838](https://github.com/oduist/oduflow/commit/0bd0838))

### Documentation

- **Use Odoo 19.0 across examples** and bump default image fallbacks to `odoo:19.0` ([a1fba72](https://github.com/oduist/oduflow/commit/a1fba72))
- **Docs Material redesign** ([7980fea](https://github.com/oduist/oduflow/commit/7980fea))
- **Document self-hosted OAuth** in the config reference, quick start, and llms docs ([73aa9b9](https://github.com/oduist/oduflow/commit/73aa9b9))
- **Consolidate agent guidance into `AGENTS.md`**, making `CLAUDE.md` a thin include ([7b60d8c](https://github.com/oduist/oduflow/commit/7b60d8c))
- **Document `refresh_template` MCP tool** in mcp-tools, llms, llms-full ([73a4768](https://github.com/oduist/oduflow/commit/73a4768))
- **Rework transport mode documentation** (stdio/http), add `uvx` examples ([f2c8eef](https://github.com/oduist/oduflow/commit/f2c8eef))
- **Fix `create_environment` CLI examples** — wrong positional arg order ([67dae80](https://github.com/oduist/oduflow/commit/67dae80))
- **Docs auto-deploy and `.oduflow/` dependency files** ([7f8d654](https://github.com/oduist/oduflow/commit/7f8d654))

## v1.20.1 (since v1.15.1)

### Breaking Changes

- **Stdio transport is now the default** — `oduflow` starts in stdio mode by default (previously HTTP). Use `oduflow --transport http` for HTTP mode. ([34e42fa](https://github.com/oduist/oduflow/commit/34e42fa), [f33dfe6](https://github.com/oduist/oduflow/commit/f33dfe6))

### Features

- **Move repo-level `odoo.conf` to `.oduflow/odoo.conf`** — per-repo Odoo config is now read from `<repo>/.oduflow/odoo.conf` instead of the repo root
- **Auto-initialize on startup** — `oduflow` automatically runs initialization (system setup, Docker check) on first start, removing the need for a separate `oduflow init` step ([34e42fa](https://github.com/oduist/oduflow/commit/34e42fa), [f33dfe6](https://github.com/oduist/oduflow/commit/f33dfe6))
- **MCP tools refinement** — output cache for long-running tool results, 7 new MCP tools, 3 enhanced tools; renamed `exec_in_odoo` to `run_odoo_command` ([44810aa](https://github.com/oduist/oduflow/commit/44810aa))
- **Include odoo.conf in upgrade** — module upgrades now apply odoo.conf changes, skipping files that haven't changed ([f69bc2f](https://github.com/oduist/oduflow/commit/f69bc2f))
- **Show template database name** in `list-templates` output ([c816229](https://github.com/oduist/oduflow/commit/c816229))
- **Default odoo.conf values** — added `workers=0` and `db_maxconn=4` to the odoo.conf template for single-process development ([f16ab37](https://github.com/oduist/oduflow/commit/f16ab37))

### Dashboard

- **Rebuild button** — added rebuild button to web UI; pull Docker image before create/rebuild operations ([0004ac7](https://github.com/oduist/oduflow/commit/0004ac7))

### Bug Fixes

- **Sanitization: delete mail servers** — delete mail servers entirely instead of just disabling them during database sanitization ([4f729b1](https://github.com/oduist/oduflow/commit/4f729b1))
- **`search_in_odoo` filenames and limit** — always show filenames in search results and fix `max_results` limit not being applied correctly ([fc3c53c](https://github.com/oduist/oduflow/commit/fc3c53c))
- **Missing team parameter in `pull_environment`** — pass team parameter when running module operations during pull ([dc43104](https://github.com/oduist/oduflow/commit/dc43104))
- **`delete_service_preset` lock** — fix locking for delete_service_preset operation ([18b18b1](https://github.com/oduist/oduflow/commit/18b18b1))

### Documentation

- Add MCP tools refinement spec (`mcp-ref.md`) ([87edd27](https://github.com/oduist/oduflow/commit/87edd27), [417eba7](https://github.com/oduist/oduflow/commit/417eba7))
- Rename Mutex → Lock in documentation, add glightbox for image zoom ([18b18b1](https://github.com/oduist/oduflow/commit/18b18b1))
- Rename `exec_in_odoo` → `run_odoo_command`, add 7 missing tools to docs ([518e2c8](https://github.com/oduist/oduflow/commit/518e2c8))
- Advise agents to prefer local search over container search for Odoo sources ([961b9dc](https://github.com/oduist/oduflow/commit/961b9dc))

---

## v1.15.1 (since v1.10.1)

### Breaking Changes

- **Team-based multi-tenancy replaces instance-based isolation** — configuration migrated from `.env` / `ODUFLOW_INSTANCE_ID` to TOML-based `oduflow.toml` with per-team settings (workspaces, templates, credentials, port ranges, hostnames). The `.env.example` file has been removed. ([ad3b382](https://github.com/oduist/oduflow/commit/ad3b382), [14503a0](https://github.com/oduist/oduflow/commit/14503a0))

### Features

- **Team-based multi-tenancy** — per-team isolation with dedicated hostnames, git credentials, port ranges, and MCP token auth; auto-generated Traefik dynamic config for per-team routing; team resolution from Host header or bearer token ([ad3b382](https://github.com/oduist/oduflow/commit/ad3b382), [14503a0](https://github.com/oduist/oduflow/commit/14503a0))
- **CLI: `run-instance` command, `--version` and `--instance` flags** — run a named instance directly from CLI with version info support ([0094369](https://github.com/oduist/oduflow/commit/0094369))
- **CLI: `systemd-install` / `systemd-uninstall` commands** — install/remove Oduflow as a systemd service ([d266ca9](https://github.com/oduist/oduflow/commit/d266ca9))
- **Per-environment PostgreSQL credentials** — each environment gets its own isolated database role and password ([e4949b0](https://github.com/oduist/oduflow/commit/e4949b0))
- **Two-tier database sanitization** — system-wide sanitization scripts plus per-repo `.odoo_sanitize/` folder support ([cd0b9ec](https://github.com/oduist/oduflow/commit/cd0b9ec))
- **MCP tool: `read_file_in_odoo`** — read files and list directories inside Odoo containers without shell commands ([abcc525](https://github.com/oduist/oduflow/commit/abcc525))
- **MCP tool: `reset_admin_password`** — reset the admin user password in any environment's database ([608b476](https://github.com/oduist/oduflow/commit/608b476))
- **Extra repos: fetch summary and propagation** — `update_extra_repo` returns a summary of fetched branches and propagates updates to running environments ([a80ba53](https://github.com/oduist/oduflow/commit/a80ba53))
- **Per-branch locking module** — new `LockManager` replaces the single global mutex with per-branch and per-team locks ([ad3b382](https://github.com/oduist/oduflow/commit/ad3b382))
- **Docker publishing support** — added `.dockerignore` and Docker publishing instructions ([24f653a](https://github.com/oduist/oduflow/commit/24f653a))

### Dashboard

- **Interactive SQL console (psql)** — run SQL queries directly from the web dashboard ([15d0e59](https://github.com/oduist/oduflow/commit/15d0e59))
- **Interactive Odoo shell console** — access Odoo shell via the web dashboard ([894f1ea](https://github.com/oduist/oduflow/commit/894f1ea))
- **Colored logs rendering** — dashboard shows ANSI-colored logs; MCP tools receive clean stripped output ([2e3989b](https://github.com/oduist/oduflow/commit/2e3989b))
- **Detailed sync results popup** — sync operations show detailed results in a popup instead of a plain toast ([ade3ee6](https://github.com/oduist/oduflow/commit/ade3ee6))
- **Editable restore service dialog** — service restore dialog shows editable preset values before confirming ([4eac120](https://github.com/oduist/oduflow/commit/4eac120))
- **Wider logs modal** — logs modal expanded to 80vw with horizontal scroll ([e9d0273](https://github.com/oduist/oduflow/commit/e9d0273))

### Bug Fixes

- **MCP concurrency** — pass `stateless_http=True` to unblock concurrent MCP requests; run sync MCP tools in thread pool to prevent event loop blocking during long operations ([81457b1](https://github.com/oduist/oduflow/commit/81457b1), [ed4a265](https://github.com/oduist/oduflow/commit/ed4a265))
- **TLS certresolver name** — match Docker label to Traefik's ACME provider name ("letsencrypt" not "le") ([727448b](https://github.com/oduist/oduflow/commit/727448b))
- **HOME fallback for systemd** — add `HOME=/root` to `GIT_ENV` so git operations work under systemd ([a982b9d](https://github.com/oduist/oduflow/commit/a982b9d))
- **Reject SSH repo URLs** — SSH URLs caused hangs due to interactive host-key prompts; now rejected early with a clear error ([ad3b382](https://github.com/oduist/oduflow/commit/ad3b382))
- **Database ownership on template clone** — fixed ownership reassignment for tables, sequences, views, and materialized views; switched to `GRANT role` approach ([31df320](https://github.com/oduist/oduflow/commit/31df320), [afb66d0](https://github.com/oduist/oduflow/commit/afb66d0), [5c7b737](https://github.com/oduist/oduflow/commit/5c7b737))
- **Environment deletion** — drop database before role to avoid dependency errors ([4b28e9e](https://github.com/oduist/oduflow/commit/4b28e9e))
- **Strip `db_password` from odoo.conf** — ensures the Docker entrypoint uses environment variables instead ([f9f89d5](https://github.com/oduist/oduflow/commit/f9f89d5))
- **Template publish** — prevent self-copy in `reload_template`, scope publish to environments matching the template ([b92631a](https://github.com/oduist/oduflow/commit/b92631a))
- **Git sync** — replace `git pull --rebase` with `fetch + reset --hard` for reliable sync; use explicit refspec in `pull_repo` ([d57ee7e](https://github.com/oduist/oduflow/commit/d57ee7e), [74ef53d](https://github.com/oduist/oduflow/commit/74ef53d))
- **Load extra_addons from template metadata** in `create_environment` ([3d629e6](https://github.com/oduist/oduflow/commit/3d629e6))
- **Dump restoration** — handle gzip files and verify table count ([7ef7a02](https://github.com/oduist/oduflow/commit/7ef7a02))
- **Fallback data dir** — fallback `ODUFLOW_HOME` to `~/oduflow_data_{id}` when `/srv` is read-only ([d45307c](https://github.com/oduist/oduflow/commit/d45307c))

### Refactoring

- **TOML-based configuration** — replace `.env` with `oduflow.toml`; `oduflow init` auto-bootstraps default config ([ad3b382](https://github.com/oduist/oduflow/commit/ad3b382))
- **Merge `external_host` and `base_domain` into per-team `hostname`** ([ad3b382](https://github.com/oduist/oduflow/commit/ad3b382))
- **Rename `ODUFLOW_HOME` → `ODUFLOW_DATA_DIR`** with instance subdirectories ([52224cb](https://github.com/oduist/oduflow/commit/52224cb))
- **Move odoo.conf and odoo_sanitize** from `etc/` to instance data directory ([dbfc969](https://github.com/oduist/oduflow/commit/dbfc969))
- **Require explicit branch for extra_addons** — format is now `name:branch` ([9254169](https://github.com/oduist/oduflow/commit/9254169))
- **`chown_recursive()` helper** — replaces manual `os.chown` loops with macOS fallback support ([872059d](https://github.com/oduist/oduflow/commit/872059d))
- **Rename `get_environment_status` → `get_environment_info`** with comprehensive details ([20e4abf](https://github.com/oduist/oduflow/commit/20e4abf))
- **Rename `get_agent_guide` → `get_agent_skill`** ([8625790](https://github.com/oduist/oduflow/commit/8625790))
- **Rename MCP tools** for clarity ([fbbcf54](https://github.com/oduist/oduflow/commit/fbbcf54))
- **Async MCP tool wrapper** — `handle_errors` decorator converted to async, offloading all tools to thread pool ([ed4a265](https://github.com/oduist/oduflow/commit/ed4a265))

### Documentation

- Comprehensive docs rewrite for TOML-based multi-team architecture ([ad3b382](https://github.com/oduist/oduflow/commit/ad3b382))
- Add MCP tools refinement spec (`mcp-ref.md`) ([87edd27](https://github.com/oduist/oduflow/commit/87edd27))
- Add missing features to documentation, fix inaccuracies ([e0c3bc3](https://github.com/oduist/oduflow/commit/e0c3bc3), [1aaa6a5](https://github.com/oduist/oduflow/commit/1aaa6a5))
- Add `llms-full.txt` for complete LLM-consumable documentation ([e330ad4](https://github.com/oduist/oduflow/commit/e330ad4))
- Add screenshots to documentation pages ([8726d01](https://github.com/oduist/oduflow/commit/8726d01))
- Add macOS vs Linux file ownership section ([ac4a7a8](https://github.com/oduist/oduflow/commit/ac4a7a8))
- Add `CLAUDE.md` with project architecture and dev commands ([008d43f](https://github.com/oduist/oduflow/commit/008d43f))

---

## v1.10.1 and earlier

## Features

- Add `reset_admin_password` MCP tool to reset the admin password in Odoo environments ([608b476](https://github.com/oduist/oduflow/commit/608b476))
- Refactor Agent Guides into multi-guide system with Odoo version-specific development guides ([8680a18](https://github.com/oduist/oduflow/commit/8680a18b284ba1a7e0e2de9a2d0c5b9af489af69))
- Add `import_template_from_odoo` — import templates from running Odoo instances ([a6d53fc](https://github.com/oduist/oduflow/commit/a6d53fc988e5c387c2d49d98436a530fee3bab19))
- Store `use_overlay` flag in template metadata instead of computing filestore size on every env creation ([e393c44](https://github.com/oduist/oduflow/commit/e393c445fbc040ea451c6876aa82a35b1d5ff392))
- Show `use_overlay` mode in CLI `list-templates` and dashboard UI ([cf85489](https://github.com/oduist/oduflow/commit/cf854894eeb8c33e13b05599652a7e6894c8b4f0))
- Show template name in `get_environment_info` and dashboard UI ([e79a6ae](https://github.com/oduist/oduflow/commit/e79a6ae80917d9d64e5b22d5ca0c61e78ed0019e))
- Add Sync button to environment UI ([66ceed8](https://github.com/oduist/oduflow/commit/66ceed8ef8747b3f679ad1ed7ed62aeab8e779b6))
- Add `ODUFLOW_TRACE=1` trace logging for sync/classify pipeline ([af7b9bc](https://github.com/oduist/oduflow/commit/af7b9bcb55ba43cb4daaaa884cfc9edcfd59a9cb))
- Docker deploy added ([e934bea](https://github.com/oduist/oduflow/commit/e934bea4e9e72e5259c94b6bd54c558e4a0e4daf))
- Append `/web?debug=1` to environment URLs in dashboard ([ff913ad](https://github.com/oduist/oduflow/commit/ff913ada0ff1406b505c5fa103e85977b57d0428))
- Add logo to Web UI header ([6fd52ce](https://github.com/oduist/oduflow/commit/6fd52ce17369f30d889aa09fb847bfb136dfeae9))
- Add `run_db_query` MCP tool for executing SQL queries against environment databases ([d7b1b44](https://github.com/oduist/oduflow/commit/d7b1b44d3a378d1357125526860b6a459fc8bbf1))
- Credentials management UI + git auth improvements ([259164e](https://github.com/oduist/oduflow/commit/259164ebcbd74a1216e5585e2d93c1526a6d69ae))
- Per-user git credentials for repo operations ([5c28100](https://github.com/oduist/oduflow/commit/5c28100df0b9072c0b1da85a230ba06e6fd45bf8))
- Add `oduflow cleanup` CLI command to remove orphaned resources ([d1fa09c](https://github.com/oduist/oduflow/commit/d1fa09cf346726892653f724f0ce4a47de19a8b5))
- Add protect/unprotect for extra repos ([1245d6c](https://github.com/oduist/oduflow/commit/1245d6c5f953ac876c52d113ba5b43d82c94a0af))
- Add `update_extra_repo` MCP tool; remove one-off migration SQL ([3c678cc](https://github.com/oduist/oduflow/commit/3c678ccffe26060043a3deb28095ed66150c1108))
- Report elapsed time in `create_environment` response ([90ca9e1](https://github.com/oduist/oduflow/commit/90ca9e19ea1beb6957c30066cbec1a4f78f14927))
- Show filestore and dump sizes in template listing ([a1403a7](https://github.com/oduist/oduflow/commit/a1403a7737d071b42f3052214ae3eef7684c4f65))
- Add Recreate button for environments in web UI ([31afb9e](https://github.com/oduist/oduflow/commit/31afb9e7a28d7e785d05849783db36927f851137))

## Fixes

- Show friendly error when Docker is not available ([5df6df7](https://github.com/oduist/oduflow/commit/5df6df71f6f4886c815f59b1f7469884beb30045))
- Service logs filtering and state isolation from env logs ([9926c2a](https://github.com/oduist/oduflow/commit/9926c2aa6946f80c7e0ef6ac0c51cddaac7a9612))
- Update `test_settings` to match current Settings API ([b92fe80](https://github.com/oduist/oduflow/commit/b92fe800e75c466384cb3f8290543c3b56183f3c))
- Restart container after successful module installation ([b819c65](https://github.com/oduist/oduflow/commit/b819c65c4eb526ba4a51f97682c239b377bc1319))
- Fix tests: expect `ToolError` instead of `ValueError` ([dcf7f23](https://github.com/oduist/oduflow/commit/dcf7f23c7d608603e2fdd589a0471ea594026432))
- License load fix ([8e66e8f](https://github.com/oduist/oduflow/commit/8e66e8f005ac4aa1f8fdd64a50bd84e639459319))
- Stream dump file to container to avoid OOM; save dump to workspace on `reload-template --dump-path` ([8615fe2](https://github.com/oduist/oduflow/commit/8615fe29354943110b3c75def142ad2a9ab3d292))
- Eliminate OOM in all container file extraction (`init_template`, `template_down`, `publish_env_as_template`) ([b40395b](https://github.com/oduist/oduflow/commit/b40395b769480e09cc340c3c68f8dd31217f1cab))
- Surface database drop failures as warnings in `delete_environment` ([02a628b](https://github.com/oduist/oduflow/commit/02a628b4ebc59e142791f8d88539d0a4a6d865c7))
- Use `odoo_image` version as fallback branch for extra addons worktree ([ea297fd](https://github.com/oduist/oduflow/commit/ea297fdf4d81bc06a7bd0c22f89cb1b8427582c0))
- Configure fetch refspec for bare extra repos so `git fetch` updates branches ([54bc937](https://github.com/oduist/oduflow/commit/54bc9373e55569f85db0fe2737691d1a9cb10be3))

## Documentation

- Rework README — add missing config vars, licensing, template metadata, CLI flags ([0b8de7e](https://github.com/oduist/oduflow/commit/0b8de7eea6cb92cffc7678fc9e7c6101f7463df4))
- Restructure README — reorder sections, remove odoo.sh comparison, merge internals ([70af4df](https://github.com/oduist/oduflow/commit/70af4df31e3295db864a034d6afbc43e21218ad2))
- Update README.md ([fe6674f](https://github.com/oduist/oduflow/commit/fe6674ff41ccdcf0a0e4e2f9cab0400f287b2af6))
- Add MkDocs with Material theme, build site to docs/ for GitHub Pages ([bbcf1e9](https://github.com/oduist/oduflow/commit/bbcf1e90691ad96368fd7b6c9270870f64a39311))
- Switch to GitBook theme, use gh-pages branch for deployment ([3cfbbb2](https://github.com/oduist/oduflow/commit/3cfbbb261fabba064e0877430ae5dceeac91d88b))
- Add `requirements-docs.txt` for mkdocs setup ([b620ff5](https://github.com/oduist/oduflow/commit/b620ff57153623f5b47cfcec89b261389ea81936))
- Split README into structured mkdocs documentation ([c52084f](https://github.com/oduist/oduflow/commit/c52084f0772fc0abe1564a384e0341b0907d35ae))
- Add database migrations workflow guide for agents ([63629fd](https://github.com/oduist/oduflow/commit/63629fd267170e1af7a3e5d1d162653da61f99bc))
- Document extra addons pinning behavior in environments ([651f877](https://github.com/oduist/oduflow/commit/651f8778266fd350399bd1605c32ad4ea96e9e77))

## Refactor

- Rename CLI command `promote` to `template-from-env` ([ecaaf84](https://github.com/oduist/oduflow/commit/ecaaf84c1a8bf205ed6b3451ec0d58f0842dce58))
- Rename template DB prefix to `oduflow_template_{id}_{name}`, make `template_name` required ([c3bee0f](https://github.com/oduist/oduflow/commit/c3bee0fbeece89253860389ad144461d4d291861))

## Chore

- Make dev guide hint more assertive in `create_environment` output ([b9933ce](https://github.com/oduist/oduflow/commit/b9933cec0b72aa4832c711a9fbea8ffae9d10b7e))
- Clean up startup logs: use websockets-sansio, suppress docker noise ([a486d87](https://github.com/oduist/oduflow/commit/a486d87da6c7fdf94ae4711d13fe2b12553ad225))
- Add `site/` to `.gitignore`, remove tracked build output ([b36e832](https://github.com/oduist/oduflow/commit/b36e8327eacc900a44f0ea65a0e585b9005a3dc6))
- Add `logo.png` to tracked files for package distribution ([935f5f7](https://github.com/oduist/oduflow/commit/935f5f764d0544d32627c48e046596c0fa441fe8))
