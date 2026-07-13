# 0031 — Passwordless "Connect as user" session minting (`connect_as_user`)

**Status:** Adopted
**Type:** MCP capability
**First introduced:** passwordless Odoo session minting for agent UI testing (this change)
**Key code today:** `docker_ops/odoo_ops.py` (`connect_as_user`, `_extract_sentinel`), `docker_ops/env_ops.py` (`get_env_base_url`), `server.py` (`connect_as_user` tool), `scoped_access.py` (allowlist)

## Context

The advertised agent feedback loop ends in browser testing: the agent writes a
module, installs it, then drives the UI with Playwright to validate acceptance
criteria. In practice that loop broke at `/web/login` — Playwright has no
credentials to type, so all UI/UX testing needed a human to log in first, or the
agent had to set a real password and type it into the form. The password path is
also a security footgun: it leaves a weak, known password on a database whose URL
is reachable from the internet under Traefik auto-HTTPS routing — exactly the
environments handed to customers for UAT.

Odoo.sh solves this with a **Connect** button that mints a session server-side and
hands the browser the resulting cookie — no password created, transmitted, or
stored; authorization is gated on the control plane, not on Odoo. Oduflow already
has that control-plane position: it can run arbitrary ORM code in the container
via `run_odoo_shell`, so the trust boundary already exists.

## Decision

Add an MCP tool `connect_as_user(env_name, user)` that mints an Odoo session for
an arbitrary user (by login or numeric id) and returns the `session_id` **cookie
payload + base URL**. The agent adds the cookie to a Playwright context and
`goto`s the URL, landing in an authenticated session as that user — no login form,
no credentials, no human. A fresh session per user lets one test run exercise a
feature across roles (admin, sales manager, portal).

Deliberate scope choices:

- **Cookie-only in v1; the convenience token-URL is deferred.** A URL that *sets*
  the cookie needs a route that issues `Set-Cookie` for the *Odoo env's* host.
  Oduflow's own web app can't: UI/MCP are served on the parent host (traefik) or a
  different port, while envs live on subdomains, so a host-only cookie never
  reaches the env and a `Domain=`-broadcast cookie would leak across all of a
  team's envs. The only clean home for such a route is inside the Odoo container —
  i.e. an addon installed per environment, which the platform-layer approach
  exists precisely to avoid. Playwright needs the cookie, not the URL, so cookie +
  base URL closes the loop; the token-URL is future work.
- **No new authorization gate.** Whoever can call this can already
  `run_odoo_shell`, so it is no privilege escalation; consistent with
  `reset_admin_password`/`run_odoo_shell` (which are not `.protected`-gated), it is
  allowed on every environment, and no `allow_impersonation` config flag is added.
- **Reachable by scoped per-env tokens.** Added to the `SCOPED_ALLOWLIST` of
  [[0028-scoped-environment-mcp-access]]. Its only environment selector is
  `env_name` (the target is a plain login/id, never another env), so the scoped
  middleware's inject-`env_name`-from-URL invariant keeps it structurally confined
  to its own environment.

## How it works (macro)

- **Mint in `odoo shell`.** Like `run_odoo_shell`, a script is tar-streamed into
  the container and run under the environment's scoped PostgreSQL role
  (`load_credentials(..., allow_fallback=False)`, [[0013-per-environment-db-credentials-and-sanitization]]).
  It resolves the user, creates a session via `odoo.http.root.session_store`, sets
  the exact state a login produces (`db`/`login`/`uid`/`session_token =
  _compute_session_token(sid)`/`context`), and `save()`s it. The store is a
  filesystem store shared by the shell process and the live HTTP server (same
  container, same data dir), so the server honours the session on the next request
  carrying that `session_id` cookie.
- **Sentinel-framed extraction.** `odoo shell` merges its banner and logging with
  `print()` output on one stream, so the sid can't be recovered by last-line
  parsing. The script frames every value (`__ODUFLOW_SID__…__END__`) and the whole
  mint runs in try/except that prints a framed traceback on failure — so drift in
  the internal session API (rewritten in the Odoo 17.0 HTTP stack; the tool must
  work across the supported 15–19) surfaces as a debuggable error, not silence.
- **Cookie domain + URL.** `get_env_base_url` returns `(base_url, cookie_domain)`
  from the same routing logic as `get_environment_info` (traefik subdomain vs.
  published host port), so the returned cookie is scoped to a host the browser
  will actually send it to.
- **Thin tool layer.** The `@mcp.tool()` wrapper wears the standard
  `@handle_errors`/`@with_env_lock` stack, so the call counts as environment
  activity (reaper idle-timer reset) and `_wake_for_work` starts a stopped env
  first — guaranteeing the HTTP server is up to honour the session. It returns a
  **string** (not a dict): `handle_errors` logs non-string results verbatim at
  INFO, so a dict would dump the live session secret whole into the server log.

## Consequences

- The "agent click-tests its own work" loop closes end to end, including
  multi-role testing (admin/manager/portal) in a single run, with no passwords
  created or stored.
- **The session id is a live credential.** By design it is returned to the caller
  (it needs it), so it appears in the MCP client transcript, in the in-memory
  `output_cache`, and in the INFO-log preview `handle_errors` emits. This is
  acceptable because environments are ephemeral and per-branch and the session
  carries Odoo's own TTL, but it is a real exposure — the tool's docstring says to
  treat the value like a password. Returning a string (not a dict) bounds the log
  leak to the preview rather than the whole payload.
- Additive and free: same server, no new config knobs, existing tools unchanged.
- **Future work:** the human-convenience token-URL form (a consuming route that
  sets the cookie on the Odoo host), and — if ever needed — a short-lived,
  single-use token in place of handing back a full-lifetime session.

## History

- (this change) — `connect_as_user`: server-side passwordless session minting via
  `odoo shell` + filesystem session store, sentinel-framed sid extraction,
  `get_env_base_url` cookie-domain helper, scoped-allowlist entry, and the
  Playwright-loop docs update.
