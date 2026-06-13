# Changelog

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
