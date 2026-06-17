# 0018 — Onboarding: stdio default transport + auto-init on startup

**Status:** Adopted (still in force)
**Type:** Architecture
**First introduced:** `f33dfe6` "auto-init on startup, add stdio transport (default)" (2026-03-04)
**Key code today:** `server.py` (`main`, `--transport` default `stdio`, `_ensure_initialized`, `_start_stdio`/`_start_http`)

## Context

By this point Oduflow could run remotely over HTTP for many users
([[0002-remote-multi-user-mcp-access]]) and had grown real infrastructure to set
up: shared Docker networking, the Traefik proxy
([[0004-stable-addressing-port-registry-and-traefik]]), per-team directories,
bundled configs, sanitize scripts, and agent guides ([[0009-agent-guidance-system]]).
All of that was provisioned by a mandatory `oduflow init` step the user had to
run before the server would work, and the server's default transport assumed the
remote, networked case.

That order of operations is backwards for the **most common use**: a single
developer running Oduflow locally so one local agent can drive Odoo. For that
person there is no network to configure and no second tenant — yet they had to
(1) pick and configure a transport and (2) remember a separate init command
before anything worked, and hit "Run init_system first" errors if they didn't.
Every required step before "it just works" is friction that loses the simple
single-local-agent case to confusion.

The tension: HTTP/remote is a real and important path, so the simplification
must not break it — it must only stop *forcing* network setup on people who don't
need it.

## Decision

Make the zero-config local case the default, and remove the manual init step
entirely.

- **stdio is the default transport.** With no flags, `oduflow` starts over
  **stdio** — the right transport for a single local agent, requiring no port,
  no host, no auth. `--transport http` opts into the remote/multi-user server
  ([[0002-remote-multi-user-mcp-access]]); container and systemd deployments set
  that explicitly. The default serves the common case; remote is one flag away.
- **Auto-initialize on startup.** The mandatory `oduflow init` command is
  replaced by `_ensure_initialized()`, run automatically every time the server
  starts. It is **idempotent**: it creates shared infrastructure, copies bundled
  configs and agent guides, and sets up per-team directories only if missing, so
  running it on every boot is safe and cheap. There is no separate init step to
  forget; "System not initialized" becomes "restart the server."
- **Config bootstraps itself too.** First start writes a default config if none
  exists (searching the same fallback path the server actually writes to), so the
  tool starts with **zero configuration** — install, run, connect an agent.

## How it works (macro)

- **One entry point, mode by flag.** `main()` parses `--transport` (default
  `stdio`), ensures the system is initialized, records the active transport, then
  dispatches to `_start_stdio()` or `_start_http()`. The same binary covers local
  and remote; only the flag (and what gets initialized/served) differs.
- **Idempotent init as a startup phase, not a command.** `_ensure_initialized`
  brings up shared infra and, per configured team, creates directories and seeds
  bundled artifacts (configs, sanitize scripts, agent guides) only where absent.
  Because every step is "create if missing," the server can run it unconditionally
  at boot.
- **Self-seeding config.** On first run the config file is created at the
  resolved data/etc dir if absent, removing the previously mandatory "configure"
  step from the onboarding path.

## Consequences

- The local onboarding collapses to **install → run → connect** — no init
  command, no transport choice, no config file to write first. This makes the
  default experience match the most common intent (one developer, one local
  agent) and removes a whole class of "did you run init?" failures.
- Auto-init being idempotent means startup is self-healing: a half-set-up data
  dir is completed on the next boot rather than requiring a remembered command,
  which also simplified upgrades and container restarts.
- The remote path is preserved but now **explicit**: HTTP, auth
  ([[0020-authentication-oauth]]), and multi-tenancy are opt-in via
  `--transport http`, keeping the dangerous-by-default surface (open network,
  shared tenants) out of the local case. Dockerfile/systemd set the flag so
  hosted deployments are unaffected.
- Making stdio the default also set up the later **live-mount** fast path, which
  is gated specifically to the stdio transport ([[0021-code-delivery-modes]]).

## History

- `f33dfe6` (2026-03-04) — replace `oduflow init` with idempotent
  `_ensure_initialized()` at startup; add `--transport` (stdio default, http for
  remote); `_start_stdio` via FastMCP `run_stdio_async`; bootstrap config copy on
  every start.
- `34e42fa` (2026-03-09) — ship the simplification: docs drop `oduflow init` as a
  required step, document `--transport`, default Dockerfile/systemd to
  `--transport http`; auto-create config on first start; fix the config search
  path so zero-config startup works (#7).
