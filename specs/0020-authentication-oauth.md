# 0020 — Authentication for MCP HTTP transport: GitHub OAuth → self-hosted OAuth Authorization Server

**Status:** Adopted (self-hosted AS is current; traefik uses a per-team, host-relative issuer; static Bearer tokens retained)
**Type:** Architecture
**First introduced:** `97c3fc8` "add GitHub OAuth support for MCP HTTP transport" (2026-03-13)
**Key code today:** `server.py` (`_build_auth`, transport/auth wiring), `oauth_provider.py` (`OduflowOAuthProvider`, host-relative `get_routes`), `settings.py` (`oauth_base_url`, `oauth_enabled`, per-team `auth_token`/`hostname`)

## Context

Remote, multi-user access ([[0002-remote-multi-user-mcp-access]]) established two
invariants: the server can run over HTTP for callers that aren't the local
process, and **identity is carried out-of-band in the transport, never as a tool
parameter**. The first authenticated form of that identity was a raw **static
Bearer token** per team (introduced with team-based multi-tenancy,
[[0014-team-based-multi-tenancy]]): the caller sets an `Authorization` header,
the server maps the token to a team and scopes every resource accordingly.

A static header token is fine for `curl`, CLI clients, and IDEs that let you
paste a header. But the most important MCP clients — claude.ai, MCP Inspector —
expect to authenticate via **OAuth**, not a hand-pasted Bearer token. To be a
first-class, hostable MCP service, Oduflow had to speak the auth protocol those
clients actually use, while keeping the "no token in tool args" rule intact.

The first answer leaned on GitHub as the identity provider. That worked but
created a hard dependency: every deployment needed a registered GitHub OAuth app
(`oauth_client_id`/`secret`), access control was a per-team `github_users`
whitelist, and the trust root was a third party. For a tool whose whole point is
self-contained, single-binary deployment, depending on an external IdP to log in
to your *own* environments was the wrong trade.

## Decision

Harden MCP HTTP auth in two steps, ending at a **self-hosted OAuth 2.1
Authorization Server** that issues and validates tokens itself, with static
Bearer tokens retained as the simple fallback.

- **Step 1 — GitHub OAuth (proxy).** Add OAuth 2.1 via GitHub alongside static
  tokens, auto-detected from config: setting `oauth_client_id`/`secret`/
  `base_url` enables the GitHub flow; `auth_token` keeps static tokens; both can
  coexist. Team resolution and access control move to GitHub login
  (`github_users` whitelist). This made claude.ai/Inspector connect with a real
  OAuth dance — but tied Oduflow to GitHub.
- **Step 2 — self-hosted Authorization Server.** Replace the GitHub proxy with a
  built-in OAuth 2.1 AS so MCP clients authenticate **without any external
  identity provider**. Oduflow exposes `/authorize`, `/token`, and
  `/.well-known/oauth-authorization-server`. Each team's existing `auth_token`
  *doubles as* `client_id`, `client_secret`, and the issued access token — so the
  plain Bearer-token path is unchanged and there is exactly one secret per team
  to manage. `oauth_base_url` (the instance's public URL) toggles the mode; the
  GitHub-specific config and `github_users` are removed.

The interface invariant from [[0002-remote-multi-user-mcp-access]] is preserved
throughout: auth never appears in a tool signature; identity is resolved from the
request context and threaded into per-team scoping.

## How it works (macro)

- **Auto-detected auth mode.** At startup `_build_auth` inspects settings: if
  `oauth_base_url` is set, Oduflow serves the self-hosted Authorization Server;
  otherwise it falls back to validating static Bearer tokens directly from the
  `Authorization` header. Both reduce to the same per-team `auth_token`.
- **One secret, three roles.** Because a team's `auth_token` is preregistered as
  an OAuth client where `client_id == client_secret == auth_token` and the access
  token equals it too, an OAuth client and a raw-curl client end up presenting
  the *same* credential. After the OAuth dance the team resolves exactly as it
  does for direct Bearer calls — no second identity system.
- **Scoping unchanged.** Once a request is authenticated to a team, the existing
  per-team resource scoping and locking apply; OAuth only changes *how the team
  is proven*, not what it can see.

## Consequences

- Oduflow can be **hosted as a real MCP service** that claude.ai and Inspector
  connect to via standard OAuth, with no tokens in tool arguments and no external
  IdP in the trust path — the operator controls token issuance and revocation
  (rotate `auth_token`).
- Collapsing client credentials and the issued token into the team's single
  `auth_token` kept the model tiny: no separate client registry, and the static
  Bearer path (curl/CLI/IDEs) keeps working with zero OAuth machinery.
- Dropping GitHub was a **breaking config change** (`oauth_client_id`,
  `oauth_client_secret`, per-team `github_users` removed; `oauth_base_url`
  added), traded for self-containment and operator control.
- Per-user **git** credentials for repo operations are a separate concern from
  MCP auth and are covered in [[0011-per-user-git-credentials]]; this record is
  only about authenticating the MCP transport.

## Evolution

- **Per-team, host-relative issuer (traefik).** The first self-hosted design used
  a single global issuer (`oauth_base_url`) for every team. Behind Traefik that
  forced a separate central hostname whose only job was to be the issuer — but
  Traefik requests a certificate only for hostnames it actually routes (each
  **team's** hostname, see [[0027-hard-tenant-isolation]]), so the central issuer
  host had no cert and the OAuth discovery it advertised was unreachable. Since a
  team is identified in the flow by its `auth_token`, not by the host, the issuer
  never needed to be global. Oduflow now **derives the issuer per request from the
  incoming host** in traefik mode: `OduflowOAuthProvider.get_routes` swaps the
  SDK's static discovery-metadata endpoints
  (`/.well-known/oauth-authorization-server`,
  `/.well-known/oauth-protected-resource`) for handlers that build `issuer`,
  `authorization_endpoint`, `token_endpoint`, and `resource` from the request's
  forwarded proto/host — validated against the registered team hostnames, so a
  forged `X-Forwarded-Host` cannot advertise a foreign issuer (and the 401
  `WWW-Authenticate` challenge is rewritten under the same check). Each team's
  OAuth flow therefore runs entirely on its own
  already-certificated hostname ([[0014-team-based-multi-tenancy]]), and
  `_build_auth` enables the Authorization Server automatically whenever routing is
  traefik. `oauth_base_url` becomes **optional** — needed only to pin a fixed
  issuer or in **port** mode (which has no per-team TLS host). The `/authorize`
  and `/token` operational routes are path-only and unchanged, and static Bearer
  tokens still work. In the same change, `[routing].hostname` is restricted to
  port mode (in traefik every team must set its own hostname, so a shared default
  is dead and would collide two teams on one host).

## History

- `97c3fc8` (2026-03-13) — GitHub OAuth 2.1 for MCP HTTP transport alongside
  static Bearer tokens; auto-detected mode; per-team `github_users` whitelist;
  GitHub login in MCP context (#9).
- `5f32f58` (2026-05-25) — replace the GitHub proxy with a **self-hosted OAuth
  Authorization Server**; `auth_token` doubles as client_id/secret/access token;
  remove `oauth_client_id`/`secret`/`github_users`; `oauth_base_url` toggles
  OAuth vs static tokens (breaking) (#19).
- `73aa9b9` (2026-06-10) — document self-hosted OAuth in the config reference,
  quick start, and llms docs (#33).
- `2026-07-04` — derive the OAuth issuer per-request from the team's own hostname
  in traefik mode (host-relative discovery metadata); enable the Authorization
  Server automatically in traefik and make `oauth_base_url` optional there;
  restrict `[routing].hostname` to port mode.
