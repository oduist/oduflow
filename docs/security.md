# Authentication & Security

## MCP HTTP Auth

When `auth_token` is set for a team in `oduflow.toml`, the MCP endpoint (`/mcp`) requires a Bearer token:

```
Authorization: Bearer <your-token>
```

Each team can have its own auth token:

```toml
[team.1]
auth_token = "secret-token-team-1"

[team.2]
auth_token = "secret-token-team-2"
```

The token is used to both authenticate and identify the team. This is implemented via FastMCP's `StaticTokenVerifier`.

Fresh configs get a generated `auth_token` for `[team.1]` on first startup. The
value is printed in the startup log and stored in `oduflow.toml`; use it as
`Authorization: Bearer <auth_token>` when connecting HTTP MCP clients.

## Self-hosted OAuth (for Claude.ai and other MCP clients)

Oduflow can act as its own OAuth 2.1 Authorization Server, so MCP clients that require an OAuth flow (e.g. Claude.ai Remote MCP, MCP Inspector) can connect without any external identity provider.

The team's OAuth **`client_id`** is a non-secret identifier, `team_<id>` (e.g. `team_1` for `[team.1]`); the **`client_secret`** is the team's `auth_token`. Only the `client_id` appears in the authorization URL — the secret is sent solely in the token request body, so it never leaks into logs or browser history. When the OAuth flow completes, Oduflow issues an **independent, opaque access token that expires** (with a refresh token to obtain a new one) — the client never receives the `auth_token` itself, so a compromised OAuth token has a bounded lifetime and can be revoked. The `auth_token` stays valid as a plain Bearer token for CLI clients (see [Bearer-only mode](#bearer-only-mode-cli-automation)).

### Setup

**In [traefik mode](traefik.md) it's automatic.** The Authorization Server is enabled out of the box and runs on **each team's own hostname** — the OAuth issuer is derived per request from the incoming host (which already has a Let's Encrypt certificate). Just give each team an `auth_token`; no `oauth_base_url` is needed:

```toml
[routing]
mode = "traefik"
acme_email = "admin@example.com"

[team.1]
hostname = "team-a.example.com"
auth_token = "secret-token-team-1"
```

**In port mode** (no per-team TLS host), set `oauth_base_url` to the public https URL where this instance is reachable, so the issuer is a fixed, reachable endpoint:

```toml
[oauth]
oauth_base_url = "https://your-server.com"

[team.1]
auth_token = "secret-token-team-1"
```

Either way, Oduflow exposes:

- `GET /.well-known/oauth-authorization-server` — discovery metadata
- `GET /authorize` — authorization endpoint (Authorization Code + PKCE)
- `POST /token` — token endpoint (mints/rotates the access + refresh token pair)
- `POST /revoke` — revoke a minted access or refresh token

Dynamic Client Registration (`/register`) is **disabled** — clients must use the preregistered credentials.

### Connecting from Claude.ai

1. Go to Claude.ai Settings → Connectors → Add custom MCP
2. Enter your Oduflow URL: `https://your-server.com/mcp` (in traefik mode, the team's own hostname, e.g. `https://team-a.example.com/mcp`)
3. In the OAuth fields, use the team's id as `Client ID` and its `auth_token` as `Client Secret` (the `Client ID` is `team_<N>` for `[team.N]` — e.g. `team_1` for `[team.1]`):

   ```
   Client ID     = team_1
   Client Secret = secret-token-team-1
   ```

4. Claude.ai performs the OAuth flow against your Oduflow instance, receives an access token, and connects.

The issued access token is an independent, expiring token bound to that team (not the `auth_token`), so each team's claude.ai connector ends up scoped to its own workspaces, templates, and credentials while Claude never stores the master secret. Claude.ai transparently uses its refresh token to obtain a new access token when the old one expires; the connection also survives an Oduflow restart because minted tokens are persisted.

### Bearer-only mode (CLI / automation)

For curl, IDE clients, or anything that doesn't need OAuth, simply send the `auth_token` as a Bearer header:

```
Authorization: Bearer secret-token-team-1
```

This works whether or not `oauth_base_url` is configured.

## Scoped single-environment access (`/mcp/<env>`)

The team `auth_token` unlocks the **full** tool surface — create, delete, and stop
environments, manage templates, services, and volumes. To hand an AI agent a
*confined* handle to one environment only, Oduflow exposes a scoped endpoint:

```
https://your-server.com/mcp/<env>
```

On this endpoint only the in-environment tools are available — sync
(`pull_and_apply`), install/upgrade modules, run tests, open the Odoo shell, run
SQL, read/write/search files, fetch logs and info, and `restart`. Lifecycle and
system tools (create/delete/stop/start/recreate, templates, services, volumes,
listing other environments) are **not exposed and cannot be called**. The
environment is taken from the URL, so the agent never passes — and cannot
override — which environment it operates on.

### Per-environment Secret Key

Every environment created after this feature gets its own access token, generated
at creation time and stored on the container. Use it as a **Bearer token** or as
an **OAuth** client credential — exactly like a team `auth_token`, but it only
unlocks its own `/mcp/<env>` endpoint:

```
Authorization: Bearer <environment-secret-key>
```

A per-environment token is rejected on the full `/mcp` endpoint and on any other
environment's URL, so the credential itself is the boundary.

### Getting the URL and Secret Key

In the web dashboard, open an environment's **More → MCP Access**. The dialog
shows the `/mcp/<env>` URL and the Secret Key (with copy buttons) ready to paste
into an agent's MCP configuration.

Environments created before this feature carry no Secret Key (Docker labels can't
be added to a live container); recreate the environment to issue one. Recreating
an environment also rotates its token.

## Web Dashboard Auth

The web dashboard and REST API use HTTP Basic authentication with a **separate** password:

- **Username**: `admin`
- **Password**: value of `ui_password` from `oduflow.toml`

This is independent from the MCP Bearer token (`auth_token`). Credentials are compared using `hmac.compare_digest` to prevent timing attacks.

Fresh configs get a generated `ui_password` for `[team.1]` on first startup.
Older HTTP configs with an empty `ui_password` are also auto-filled on startup
and written back to `oduflow.toml`, so an upgrade does not expose the dashboard.

### Brute-force protection

Failed sign-ins are throttled in two dimensions:

- **Per client IP** — 10 failures in a 5-minute sliding window locks that IP out
  for the rest of the window. A successful sign-in clears its counter.
- **Deployment-wide** — 100 failures in the same window, a backstop against
  guessing spread over many source addresses. It sits far above the per-IP
  threshold so one user mistyping their password cannot lock out a team.

A throttled request answers `429` with a `Retry-After` header and the same
generic *"Too many failed attempts"* page for every caller; a wrong password
answers `401` with the same body regardless of what was submitted, so nothing
reveals whether a given password half was close. Lockouts are logged (client IP
and retry window — never the submitted password).

Counters live in the server process; Oduflow runs the dashboard from a single
process, so a restart resets them.

### Which client IP the throttle sees

`X-Forwarded-For` is attacker-controlled: if it were believed unconditionally,
a client could mint a fresh throttle bucket per request just by varying the
header. So the header is honoured **only when the immediate TCP peer is a
trusted proxy**, in two places kept in step with each other — Uvicorn's
`proxy_headers` layer and the throttle's own resolution:

| Deployment | Immediate peer | Trusted to set `X-Forwarded-For` |
|---|---|---|
| Port mode, direct access | the browser/client itself | **nobody** — the peer address is used as-is |
| Traefik mode on Linux | the Traefik container | Traefik's Docker network CIDRs, resolved from the live network at startup |
| Traefik mode on Docker Desktop (macOS/Windows) | 127.0.0.1 (container→host traffic is NAT'd) | the Docker network CIDRs — **add `"127.0.0.1"` to `trusted_proxies`**, see below |
| Behind your own proxy (nginx, Cloudflare tunnel, load balancer) | that proxy | nothing until you list it in `[server].trusted_proxies` |

```toml
[server]
trusted_proxies = ["10.0.0.5", "172.18.0.0/16"]   # IPs and/or CIDRs
```

A wildcard (`*`, `0.0.0.0/0`, `::/0`) is **rejected**: the backend port is
reachable directly on the host, so blanket trust would let any client spoof its
address past the throttle. Malformed entries are rejected at config load.

When the peer is trusted, the client address is taken by walking
`X-Forwarded-For` right-to-left and using the first entry that is not itself a
trusted proxy — each hop appends, so anything further left was supplied by the
client and is ignored. With no trusted proxy configured (the default), the raw
peer address is used and the header has no effect at all.

#### Loopback is never trusted implicitly

`127.0.0.1` gets no special treatment. On a Linux host Traefik reaches the
backend from the bridge subnet, so an implicit loopback grant would only ever
benefit *local* processes — any script, cron job, or co-tenant container on the
box could then set `X-Forwarded-For` and cycle throttle buckets freely. If you
need it, grant it explicitly:

```toml
[server]
trusted_proxies = ["127.0.0.1"]
```

The one deployment that genuinely requires this is **Docker Desktop**
(macOS/Windows), where container→host traffic is NAT'd and Traefik therefore
arrives from `127.0.0.1` rather than the bridge subnet. Without the entry
Oduflow still runs and still throttles — it just sees every request as coming
from `127.0.0.1`, so the per-IP limit collapses into a deployment-wide one
(coarse, but fail-safe). Linux hosts need no such entry.

The `FORWARDED_ALLOW_IPS` environment variable — Uvicorn's own way to widen
this trust — is **not** honoured. Oduflow always passes Uvicorn an explicit
list computed from `oduflow.toml`, including an empty list when nothing is
trusted, so the effective trust is exactly what your config says and cannot be
widened from the environment.

Practical consequence: **if you front Oduflow with your own reverse proxy and
do not set `trusted_proxies`, every request shares the proxy's address** and
the per-IP limit degrades into a deployment-wide one. That fails safe (nobody
escapes throttling) but is coarse — configure `trusted_proxies` to get
per-client granularity. Traefik mode on Linux needs no configuration; it is
handled automatically.

### Sign-in URL

The sign-in page is served at `/auth_login`, configurable via
`[server].login_path`. The default is not `/login` because commodity scanners
probe the standard paths constantly, and moving off them removes most of that
log noise. `/login` and every other unknown path return a plain `404` — there
is no redirect or alias left behind, since one would keep the old surface fully
reachable.

> **A non-standard login path reduces automated scanner noise. It is not a
> security control and does not replace authentication or rate limiting.**

The URL is not a secret: it is plainly visible to anyone who loads the
dashboard, appears in browser history and proxy logs, and is one redirect away
for any authenticated user. What actually protects the endpoint is the
`ui_password` check and the throttle described above. Treat the rename purely
as log hygiene — it does not justify a weaker password, and it must never be
used as a reason to skip either control.

## When auth is disabled

MCP auth and Web UI auth are configured independently per team:

- If `auth_token` is empty, the MCP endpoint has no team Bearer token
- If `ui_password` is empty, the web dashboard has no login password

In HTTP mode, Oduflow refuses to start with an unauthenticated MCP endpoint or
dashboard unless the operator explicitly sets:

```toml
[server]
allow_insecure_http = true
```

Use that only behind your own authenticating proxy. In normal fresh HTTP
deployments, `auth_token` and `ui_password` are generated automatically and
startup logs show auth as enabled:

```
INFO  [team.1] http://localhost:8000/ (MCP token ON, OAuth OFF, UI auth ON)
```

When OAuth is enabled the status reads `OAuth ON (self-hosted)`.

## Git Credentials

![Credentials Management](img/credentials.png)

Private repository credentials are stored in the git credential store at `{team_data_dir}/.git-credentials` (per-team) via the `setup_repo_auth` tool. The clean URL (without credentials) is always used in Docker labels and logs — credentials are never exposed.

### Managing credentials via MCP

```bash
# Store credentials for a private repository
oduflow call setup_repo_auth https://user:PAT@github.com/owner/private-repo.git
```

The tool parses the URL, stores the credentials, and verifies access by running `git ls-remote`.

### Managing credentials via REST API and Web Dashboard

The Web Dashboard and REST API provide full credential lifecycle management:

| Action | REST API |
|---|---|
| **List** all stored credentials | `GET /api/credentials` |
| **Add** credentials for a repository | `POST /api/credentials/add` (body: `repo_url`) |
| **Delete** a stored credential | `POST /api/credentials/delete` (body: `host`, `username`) |
| **Validate** a credential against the provider | `POST /api/credentials/validate` (body: `host`, `username`) |

Validation checks the credential against the provider's API (GitHub, GitLab, Bitbucket). For other hosts, it reports `"valid"` if the credential exists. Tokens are always masked in API responses (e.g. `ghp_****`).

## iptables rule

On startup, an `iptables ACCEPT` rule is automatically added for the `oduflow-net` Docker bridge interface. This ensures that containers on the shared network can communicate with the host (required for Traefik `host.docker.internal` routing and PostgreSQL access). If `iptables` is not available, the rule is skipped with a warning.

## Odoo security defaults

The bundled `odoo.conf` template includes these security settings:

- `admin_passwd` set to a random value (prevents database manager access)
- `list_db = False` (hides database selector)
- `without_demo = all` (no demo data)
- `max_cron_threads = 0` (disables cron in dev environments)
