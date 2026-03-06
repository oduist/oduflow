# Traefik Routing (Auto-HTTPS)

[TOC]

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

## Service routing with Traefik

Auxiliary services also get Traefik routing. A service named `meilisearch` with base domain `dev.example.com` becomes accessible at `https://meilisearch.dev.example.com`. Custom hostnames are also supported.
