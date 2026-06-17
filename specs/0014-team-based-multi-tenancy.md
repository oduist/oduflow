# 0014 — Team-based multi-tenancy (replacing instance-based isolation)

**Status:** Adopted (current tenancy model). Supersedes the tenancy mechanism in [[0002-remote-multi-user-mcp-access]]
**Type:** Architecture
**First introduced:** `ad3b382` "replace instance-based isolation with team-based multi-tenancy" (2026-03-01), finished in `14503a0` (2026-03-01)
**Key code today:** `settings.py` (`Settings.from_toml`, `[team.*]` → `TeamSettings`, per-team `data_dir` / ports / credentials), `locking.py` (per-team locks), `server.py` (`_resolve_team`)

## Context

How Oduflow isolates one user's work from another's went through three shapes.
This record captures *why* it landed on **teams**.

1. **Per-user token hashing** (the original [[0002-remote-multi-user-mcp-access]]
   model). Identity came from the HTTP header token, hashed into a short
   `user_id` that was stamped onto every Docker resource and host path. One
   process, many users, all sharing the same data dir and database, separated
   only by a label/prefix. This was lightweight but fragile: isolation depended
   entirely on getting the per-user filtering right everywhere, and everyone
   shared one blast radius.

2. **Separate instances** (`0a8ceab`, 2026-02-12). To get real operational
   separation, Oduflow gained an `ODUFLOW_INSTANCE_ID`: databases were prefixed
   (`oduflow_{instance}_{branch}`), containers carried an `oduflow.instance`
   label, and each instance got its own data directory, while still sharing the
   Docker network, PostgreSQL and Traefik. `52224cb` (2026-02-24) reworked the
   data layout to `ODUFLOW_DATA_DIR` with auto-created `instance_{ID}`
   subdirectories so a single volume could hold all instances. This isolated
   instances cleanly — but at the cost of **env-var-driven configuration** and,
   in practice, running/initializing each instance as its own thing
   (`init-instance`, `run-instance`).

3. **Teams** (`ad3b382` / `14503a0`, 2026-03-01). The instance model was the
   right *isolation* but the wrong *ergonomics*: a hosted deployment serving
   several teams shouldn't require N processes or a spray of env vars. The
   decision was to make tenancy a **first-class, declarative concept** in one
   config file, served by one process.

## Decision

Model tenancy as **teams declared as `[team.*]` sections in `oduflow.toml`**, and
replace both per-user hashing and per-instance env vars with this single
TOML-driven model.

- Each `[team.N]` carries its own `auth_token`, `ui_password`, `hostname`, and
  `port_range`, and gets its own per-team data directory (`team_{id}`) holding
  workspaces, templates, credentials, and a private `ports.json`.
- One Oduflow process loads all teams from config and **resolves the caller's
  team per request** (auth token → Host header → single-team fallback), then
  scopes every operation to that team's directories, ports, and database
  namespace.
- Tenancy plugs into the granular lock model
  ([[0015-granular-locking]]): each team has its own **per-team lock**, so
  team-wide operations never block another team.

This supersedes the token-hash tenancy described in
[[0002-remote-multi-user-mcp-access]] (whose *transport* decision — HTTP for
remote, stdio default — still stands) and retires the instance model entirely.

## How it works (macro)

- `Settings.from_toml` requires at least one `[team.*]` section and builds a
  `TeamSettings` per team, deriving `team_{id}` data paths under the shared
  `data_dir` and a per-team port range / registry.
- At request time the server resolves which team a call belongs to and passes
  that `TeamSettings` to the orchestration layer, which reads/writes only that
  team's workspaces, templates, credentials, and ports.
- Routing reuses the Traefik seam from
  [[0004-stable-addressing-port-registry-and-traefik]]: each team has its own
  `hostname`, and per-team routers are generated so requests land on the right
  team without restarting the proxy.
- Per-team auth tokens and UI passwords are validated for uniqueness, so a token
  unambiguously identifies one team.

## Consequences

- A single hosted Oduflow can serve multiple teams with **real isolation**
  (separate data dirs, port ranges, credentials, DB namespaces) but **without**
  running multiple processes — simpler to deploy and operate than the instance
  model it replaced.
- Configuration consolidated from scattered env vars / `.env` into one
  declarative `oduflow.toml`, matching the project's lean, config-first
  direction; `init-instance` / `run-instance` and the instance env vars were
  removed.
- Tenancy, locking, and routing now share one notion of "team," keeping
  per-tenant concurrency and addressing consistent.
- The removed `MULTI_INSTANCE.md` (deleted in `0aafefb`) documents the
  superseded instance model for historical reference only.

## History

- `0a8ceab` (2026-02-12) — multi-instance support: `ODUFLOW_INSTANCE_ID`,
  DB-name and container-label prefixing, per-instance data dir; shared
  network/PostgreSQL/Traefik. (`MULTI_INSTANCE.md` added.)
- `52224cb` (2026-02-24) — `ODUFLOW_HOME` → `ODUFLOW_DATA_DIR` with auto-created
  `instance_{ID}` subdirectories for a single-volume layout.
- `0aafefb` (2026-02-24) — `MULTI_INSTANCE.md` removed.
- `ad3b382` (2026-03-01) — team-based multi-tenancy (#5): TOML `[team.*]`
  settings with per-team workspaces / templates / credentials / port ranges /
  hostnames; adds `locking.py` (per-team locks) and per-team team resolution;
  retires instance-based isolation and env-var config.
- `14503a0` (2026-03-01) — follow-up (#6): cleanup (TRACE flag, `stateless_http`,
  removed `.env.example`) completing the migration.
