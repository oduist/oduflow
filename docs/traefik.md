# Traefik Routing (Auto-HTTPS)

By default Oduflow uses **port mode**: each environment gets a dedicated host port (e.g. `http://server:50001`). This is simple and works well for local or single-developer setups.

For production-like access with HTTPS, Oduflow can deploy a **Traefik** reverse proxy that gives every environment its own subdomain with an automatically issued Let's Encrypt certificate.

## Setup

1. **Configure a wildcard DNS record.** Point `*.dev.example.com` to your server's IP address:

   ```
   *.dev.example.com  →  A  →  203.0.113.10
   ```

   Every environment will get a subdomain: `feature-login.dev.example.com`, `fix-invoice.dev.example.com`, etc.

2. **Set the configuration** in `oduflow.toml`:

   ```toml
   [routing]
   mode = "traefik"
   acme_email = "admin@example.com"

   [team.1]
   hostname = "dev.example.com"
   ```

3. **Start (or restart) Oduflow.** On startup, Oduflow will create a Traefik v3 container that:
   - Listens on ports 80 and 443
   - Automatically redirects HTTP to HTTPS
   - Obtains a separate TLS certificate from Let's Encrypt for each environment subdomain via HTTP-01 challenge
   - Routes requests to the correct Odoo container based on the subdomain
   - Also routes the Oduflow server itself via the team `hostname`

## How certificates work

Traefik requests a **per-subdomain certificate** from Let's Encrypt each time a new environment is created. This works out of the box with any DNS provider since it uses HTTP-01 validation (Traefik responds to the ACME challenge on port 80).

Wildcard certificates (`*.dev.example.com`) via DNS-01 validation are also possible but require additional Traefik configuration with a provider-specific plugin.

## OAuth on each team's hostname

In traefik mode the self-hosted [OAuth Authorization Server](security.md#self-hosted-oauth-for-claudeai-and-other-mcp-clients) is enabled **automatically** and runs on **each team's own hostname** — the OAuth issuer is derived per request from the incoming host, which already has a Let's Encrypt certificate. You do **not** need to set `oauth_base_url`: point Claude.ai at `https://<team-hostname>/mcp` and complete the OAuth flow there.

## Service routing with Traefik

Auxiliary services also get Traefik routing. A service named `meilisearch` with base domain `dev.example.com` becomes accessible at `https://meilisearch.dev.example.com`. Custom hostnames are also supported.

## Behind a Cloudflare tunnel (or other TLS-terminating upstream)

If HTTPS is terminated upstream — for example by a **Cloudflare tunnel** (`cloudflared`) that already serves a valid certificate — Traefik should not obtain its own certificates or redirect to HTTPS. Set `tls = false`:

```toml
[routing]
mode = "traefik"
tls = false          # Traefik listens on plain HTTP :80 only

[team.1]
hostname = "dev.example.com"
```

With `tls = false` Traefik:

- Listens on **port 80 only** (443 is not published), serving plain HTTP.
- Does **not** redirect HTTP→HTTPS and does **not** request Let's Encrypt certificates (`acme_email` is not required).
- Routes by the same `Host` rules as before, so each environment keeps its own subdomain.

Point the tunnel at the server's port 80 and route the wildcard hostname to it (e.g. `*.dev.example.com → http://localhost:80`). Cloudflare provides the certificate and forwards requests over HTTP; the tunnel sets `X-Forwarded-Proto: https`. On the `web` entrypoint Oduflow enables `forwardedHeaders.insecure` so Traefik passes those headers through (by default Traefik would overwrite `X-Forwarded-Proto` with the plain-HTTP connection scheme), letting Oduflow see the request as secure — the dashboard's session cookie stays `Secure` and every environment/service URL Oduflow reports is still `https://…`. Because this entrypoint trusts all forwarded headers, expose port 80 **only** to the tunnel, not to the public internet.

> **Changing `tls` on a running deployment recreates Traefik but not your environments.** Each environment and service bakes its Traefik routing labels in at creation time — `entrypoints=websecure` (with Let's Encrypt) when `tls = true`, `entrypoints=web` when `tls = false`. Restarting Oduflow recreates the Traefik container in the new mode, but pre-existing environments and services keep their old labels: after the switch their routers point at an entrypoint that no longer matches, so they become unreachable until **recreated** (or, going `false → true`, get caught by the HTTP→HTTPS redirect). Treat `tls` as a deploy-time choice; if you must flip it on a live server, recreate the existing environments and services afterwards. The default is `tls = true` (Traefik terminates TLS with Let's Encrypt, as described above).
