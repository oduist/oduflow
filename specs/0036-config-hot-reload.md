# 0036 — Config hot-reload: `oduflow.toml` without a restart

**Status:** Adopted
**Type:** Architecture — runtime/orchestration model change
**First introduced:** this change (2026-07-04), branch `litnimax/odoo-prod-hosting`
**Key code today:** `_do_reload` / `_reconcile` / `_install_reload_handler` / `_run_config_reload` and the `_reaper_thread` / `_auth_provider` runtime handles in `server.py`; `classify_settings_change` + `ReloadDelta` in `settings.py`; dynamic-settings getters in `OduflowTokenVerifier` (`scoped_access.py`) and `OduflowOAuthProvider` (`oauth_provider.py`, `refresh_clients`); `ExecReload` in `systemd.py`

## Context

Oduflow's configuration model ([[0016-configuration-model]]) reads `oduflow.toml`
**once** at startup into a frozen `Settings` singleton (`_get_settings()`), with no
file-watcher or reload path. Changing anything — most importantly **provisioning a
new team/client** — meant editing the file and **restarting the whole service**.

That is acceptable for a single-user dev tool but not for the hosting direction: on
a multi-team host, restarting to add one customer disrupts *every* other team's
in-flight work. We need to apply config changes **without downtime**, and only when
the new config is valid.

Scope was deliberately bounded during design:
- **No multi-host control plane.** Each server is its own control surface; there is
  no host registry, placement, or host-to-host push/pull. That was considered and
  rejected as over-engineering.
- **Oduflow is only a "reload target."** *How* the file arrives on the host
  (Salt, Ansible, GitOps, a bespoke config agent) is the operator's choice and out
  of scope — we add ~no code for delivery. This keeps the surface lean
  ([[0016-configuration-model]] "derive, don't ask").

## Decision

Add an in-place **config reload** triggered by **SIGHUP** (the universal reload
primitive: `systemctl reload`, `kill -HUP`), with a thin **`oduflow reload`** CLI
wrapper. The contract is **validate-before-apply**: the running server re-reads and
validates `oduflow.toml`; if valid it atomically swaps the `Settings` singleton and
runs an idempotent reconcile; if invalid it **leaves the running server entirely
untouched** and logs the error. Delivery of the file stays with the operator.

## How it works (macro)

- **Trigger.** A SIGHUP handler (installed in the main thread before either
  transport starts) only sets an event; a dedicated worker thread runs the reload
  off the signal / event-loop path. A PID file (`{base_data_dir}/oduflow.pid`) lets
  `oduflow reload` find and signal the server without systemd; `systemd.py` gains
  `ExecReload=/bin/kill -HUP $MAINPID`. `oduflow reload --check` validates the
  on-disk file in-process and exits non-zero on error — a gate for a
  render→check→reload deploy step.
- **Validate-before-apply.** `_do_reload` builds a fresh `Settings.from_toml()` and
  calls `validate()` *before* touching anything. Any failure aborts with the
  previous config still serving.
- **Atomic swap + idempotent reconcile.** On success the module singleton is
  rebound (an atomic pointer swap; handlers call `_get_settings()` fresh and hold no
  long-lived reference, and `Settings` is frozen) under the system lock, then
  reconcile re-runs the **existing** idempotent startup steps for the new config —
  `_ensure_initialized` (shared infra + per-team dirs/networks/PostgreSQL/agent
  containers, which already self-heal on config drift, [[0029-agent-console-and-chat]])
  plus disk-quota application. One-time migrations ([[0025-startup-data-migrations]])
  are **not** re-run. So reconcile is a re-run of proven code, not a new mechanism.
- **Dynamic auth.** For a hot-added team to authenticate in HTTP mode, the auth
  layer must not be pinned to the startup snapshot: the Bearer verifier now reads
  settings through a getter, and the OAuth provider (which pre-registers a client
  per team token) gains `refresh_clients()`, called on reload to add/drop team
  clients. This makes "add a client without a restart" true for the actual hosting
  case (HTTP + multi-team), not just stdio.
- **Partial applicability, reported.** `classify_settings_change` splits the diff
  into hot-applied vs restart-required. Fields consumed only at process/transport
  start — `host`, `port`, `routing.mode`, `allow_insecure_http`, `data_dir`,
  database creds/image, `oauth_base_url` — still need a restart; the swap is
  harmless but the reload **warns** which changed fields require one. Everything
  else (teams, quotas, lifecycle hours, agent settings, hostnames) takes effect now.
- **Removed team ≠ destroy.** Reconcile only *creates*, so a team dropped from the
  TOML keeps its data and containers intact and is simply no longer served; the
  reload logs this. Destruction stays a deliberate, separate action.

## Consequences

- Provisioning a customer (`[team.X]`) or changing a quota/lifecycle/agent setting
  no longer requires a restart and no longer disrupts other teams.
- The reload contract is delivery-agnostic: Salt/Ansible/GitOps drive it with
  render → `oduflow reload --check` → `oduflow reload`; Oduflow depends on none of
  them.
- Reload outcome is reported via **logs** (the trigger is asynchronous, like nginx
  `reload`); `--check` gives synchronous pre-validation. Structural changes
  (host/port/routing/data_dir/db) and turning auth on/off still require a restart,
  by design and surfaced in the report.

## Deliberately deferred

Admin HTTP/MCP reload endpoint; inotify auto-reload; a built-in GitOps/config-center
driver; a persisted `reload_state.json` for `oduflow reload --status`; and any
multi-host control plane. All are compatible follow-ups on top of this contract.

## History

- 2026-07-04 (`litnimax/odoo-prod-hosting`): initial config hot-reload — SIGHUP +
  `oduflow reload`, validate-before-apply, idempotent reconcile, dynamic auth.
