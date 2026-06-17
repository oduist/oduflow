# 0016 — Configuration model: `oduflow.toml` + repo-level `.oduflow/` config

**Status:** Adopted (still in force)
**Type:** Architecture
**First introduced:** `ad3b382` "replace instance-based isolation with team-based multi-tenancy" (2026-03-01)
**Key code today:** `settings.py` (`Settings`/`TeamSettings` dataclasses, `Settings.from_toml`), `docker_ops/env_ops.py` (repo-level `.oduflow/` resolution), `pg_tune.py` (auto-tuned `postgresql.conf`)

## Context

Early Oduflow was configured by a sprawl of `ODUFLOW_*` **environment
variables** (`ODUFLOW_HOME`, `ODUFLOW_OVERLAY_THRESHOLD_MB`,
`ODUFLOW_ETC_DIR`, `ODUFLOW_DEFAULT_TEMPLATE`, …) read ad hoc at module load.
That worked for one local instance but did not express **structure**: there was
no clean way to describe several tenants, each with its own hostname, token,
port range and data dir. The shift to team-based multi-tenancy
([[0002-remote-multi-user-mcp-access]]) needed a config format that could carry
repeated, nested sections — env vars can't.

A second need was **per-repository** configuration. How an environment runs
(Odoo settings, OS/Python dependencies) is a property of the *user's code*, not
of the Oduflow deployment, so it belongs in the repo — but without littering the
repo root with Oduflow-specific files.

Running through both: a project preference for **lean, auto-generated sensible
defaults over many config knobs**. The right number of options is the smallest
one that still works on a stranger's machine.

## Decision

Adopt a **typed `Settings` dataclass loaded from `oduflow.toml`** as the single
configuration source, with **multi-team `[team.*]` sections**; and a
**repo-level `.oduflow/` directory** for per-repository config. Default
aggressively rather than expose knobs.

- **`oduflow.toml` + typed Settings.** A frozen `Settings` dataclass (global
  options) holds a map of frozen `TeamSettings` (per-team: `hostname`,
  `auth_token`, `ui_password`, `port_range_*`, `data_dir`). `Settings.from_toml`
  parses the TOML (via `tomllib`), with every field carrying a default so a
  minimal config just works. The old `.env`/`ODUFLOW_*` mechanism and
  `.env.example` were removed.
- **Bootstrap on first run.** `oduflow init` copies a bundled default
  `oduflow.toml` into place when none exists, so first-run setup needs no manual
  config authoring.
- **Repo-level `.oduflow/`.** Per-repo settings live under `<repo>/.oduflow/`:
  `odoo.conf` (base Odoo config), `apt_packages.txt` (OS packages),
  `requirements.txt` (Python deps). Keeping them in a dedicated directory leaves
  the repo root clean; `requirements.txt` also falls back to the repo root for
  compatibility with conventions used elsewhere (e.g. odoo.sh).
- **Auto-generate, don't ask.** Where a good value can be *derived*, derive it
  instead of adding a setting — e.g. the shared `postgresql.conf` is tuned from
  the host's detected CPU/RAM on first init rather than shipped as a static file
  or exposed as TOML tunables.

## How it works (macro)

- **Load once, pass explicitly.** `from_toml` builds the `Settings` object at
  startup; it is threaded through the orchestration layer rather than re-read
  from the environment, so configuration is a value, not ambient global state.
  Per-request team resolution then selects the right `TeamSettings` (by auth
  token, Host header, or single-team fallback).
- **Defaults everywhere.** Storage threshold, lifecycle reaper hours, image
  tags, Docker resource/label names, port ranges — all have in-code defaults; a
  team section can be as small as a name. This keeps the config a stranger has
  to write minimal.
- **Repo config resolved at env build.** When creating/rebuilding an
  environment, Oduflow looks for `<repo>/.oduflow/odoo.conf` (falling back to the
  team/bundled conf), and installs `.oduflow/apt_packages.txt` and
  `.oduflow/requirements.txt` (latter falling back to repo root) into the
  container. The user controls *how their Odoo runs* from inside their own repo.
- **Tuned PostgreSQL on init.** `pg_tune.detect_resources()` reads CPU/RAM from
  `docker info` (correcting for the Docker Desktop VM, with host-stat and
  conservative fallbacks, never raising), and `generate_postgresql_conf()`
  produces a lean, SSD-oriented config sized for "one host running many
  lightweight single-user Odoo envs". The generated file is written with a
  `# KEEP` marker so [[0006-git-driven-change-classification]]'s upgrade never
  overwrites a hand-edited copy. No new TOML settings were added for it.

## Consequences

- Configuration gained **structure**: multi-team tenancy is expressible as
  repeated `[team.*]` sections, which the env-var model could not represent — this
  is what made team-based multi-tenancy practical.
- A **typed, frozen dataclass** makes settings discoverable, defaultable, and
  safe to pass around, and gives `mypy` something to check, versus stringly-typed
  `os.getenv` calls scattered across modules.
- Splitting **deployment config** (`oduflow.toml`) from **repo config**
  (`.oduflow/`) put each decision where it belongs: the operator owns the fleet,
  the developer owns how their module builds and runs.
- The "derive, don't ask" stance keeps the surface small as the system grows —
  but it puts the burden on the heuristics (e.g. resource detection) to be robust
  and to fail safe to sensible defaults rather than erroring.

## History

- `ad3b382` (2026-03-01) — migrate `.env`/`ODUFLOW_*` → `oduflow.toml`; introduce
  `Settings.from_toml`, `[team.*]` sections, per-team isolation; bootstrap a
  default config on `init`; delete `.env.example` (#5).
- `48eea3e` (2026-04-22) — read repo-level `odoo.conf` from `<repo>/.oduflow/`
  instead of the repo root (#15).
- `7f8d654` (2026-06-04) — resolve `apt_packages.txt` and `requirements.txt`
  under `.oduflow/` (requirements falls back to repo root) (#25).
- `e112ac9` (2026-06-16) — auto-generate a tuned `postgresql.conf` on first init
  via `pg_tune.py` from detected host resources; no new TOML knobs (#67).
