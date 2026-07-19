# 0011 — Per-user git credentials and repository authentication

**Status:** Adopted (still in force)
**Type:** Architecture
**First introduced:** `5c28100` "per-user git credentials for repo operations" (2026-02-20), `259164e` "credentials management UI + git auth improvements" (2026-02-20); precursor URL-credential stripping in `b545cb4` (2026-02-11)
**Key code today:** `git_ops.py` (credential store, `setup_repo_auth`, list/validate/delete, `inject_credential_user`), `server.py` (`setup_repo_auth` tool), `settings.py` (`TeamSettings.git_credentials_file`), `naming.py` (`sanitize_repo_url`), `web_ui.py` (Credentials tab)

## Context

To clone and fetch a user's Odoo addons, Oduflow must authenticate to **private
git remotes** (GitHub/GitLab/Bitbucket) on the user's behalf. Once Oduflow
became a remotely hosted, multi-tenant service
([[0014-team-based-multi-tenancy]]), this raised problems that a single shared
credential cannot solve:

- **Multi-tenant private repos.** Different teams/users have different private
  repositories and different personal access tokens; one global token would
  grant every tenant access to every other tenant's repos, or fail to reach
  some entirely.
- **No secret sharing, no SSH prompts.** Tokens must not be passed as tool
  arguments visible to the agent, and SSH URLs are unusable on a headless server
  (host-key prompts hang). The practical, scriptable path is **HTTPS + a stored
  credential**.
- **Leakage risk.** Authenticated clone URLs (`https://user:TOKEN@host/...`)
  naturally flow into Docker labels, logs, and dashboard API responses. A token
  pasted once must not resurface anywhere it can be read.

This is distinct from MCP **client** authentication (who may call the server —
see [[0020-authentication-oauth]]): this record is about Oduflow authenticating
**outbound** to upstream git remotes for clone/fetch.

## Decision

Manage git credentials **per team/tenant**, stored via git's own credential
mechanism in the tenant's private data dir, and **strip credentials from every
URL** that is logged, labelled, or shown.

- **HTTPS-only, token-based.** SSH and non-HTTPS URLs are rejected early;
  authentication is always HTTPS with a username + token. This keeps the server
  non-interactive and the credential format uniform.
- **Per-tenant credential store.** Each team has its own `.git-credentials` file
  under its isolated `data_dir` (`TeamSettings.git_credentials_file()`).
  Credentials are written through git's `credential approve` / `store` helper, so
  later `git fetch`/`clone` pick them up automatically with no token in the
  command line. Tenants never share a credential file.
- **A `setup_repo_auth` flow.** A dedicated MCP tool (and matching dashboard
  Credentials tab) takes a URL with embedded credentials, **verifies access**
  with a real `git ls-remote` against the remote, stores the credential on
  success, and returns only the *clean* (credential-free) URL. After setup,
  `create_environment` and extra-repo clones use the clean URL and the cached
  credential.
- **Credentials never leak into artifacts.** `sanitize_repo_url` removes any
  embedded `user:password` before a repo URL is logged, stored in a Docker
  label, or returned by the dashboard API.

## How it works (macro)

- **Cache once, reuse silently.** `setup_repo_auth` parses the
  `https://user:TOKEN@host/...` URL, stores the credential in the team's file via
  the git credential helper, and proves it works with `git ls-remote`. Subsequent
  clone/fetch operations run with a per-team git environment whose
  `credential.helper` points at that file, so the token is supplied by git itself
  and never appears as an argument.
- **Per-user selection at create time.** Environments and extra-repo clones carry
  a `git_user` (selectable in the Create Environment / Add Extra Repo modals,
  persisted in the Docker label and template metadata). `inject_credential_user`
  embeds that username into the clone URL so git matches the correct stored
  credential when several are present for the same host.
- **Manage credentials from the dashboard.** The Credentials tab lists stored
  entries (token masked), validates them against the provider's API
  (GitHub/GitLab/Bitbucket `user` endpoint), and deletes them — all scoped to the
  calling team's credential file.
- **Strip on the way out.** Any path that exposes a repo URL — startup logs, the
  environment list API, container labels — routes it through `sanitize_repo_url`
  first, so a token can be entered once but is never readable afterward.
- **Never write the secret in the first place.** At `create_environment` time,
  `extract_and_store_inline_credentials` moves any inline `user:PAT@host`
  credential out of the URL into the team credential store and persists only the
  sanitized URL in the `oduflow.repo` label (and thus in saved template
  metadata). This closes a write-path gap where the label previously stored the
  raw URL and only read/log paths sanitized it — a token was recoverable via
  `docker inspect` until the environment was recreated. git stderr is likewise
  scrubbed (`redact_url_credentials`) so a credential-bearing remote echoed in an
  error never reaches the caller or the logs.

## Consequences

- **Multi-tenant private repos work safely:** each team authenticates to its own
  remotes with its own tokens, with no cross-tenant access and no shared secret.
- The "verify before storing" step in `setup_repo_auth` turns a silent
  misconfiguration (bad token, no access) into an immediate, explicit failure
  instead of a confusing clone error later.
- Storing credentials through git's native helper keeps tokens **out of process
  arguments and out of the agent-visible tool surface**, consistent with the
  project rule that secrets never appear as tool parameters.
- Systematic URL sanitization closed the obvious leakage channels (logs, labels,
  API), making it safe to handle credential-bearing URLs internally.
- HTTPS-only is a deliberate limitation: it sidesteps SSH host-key prompts on a
  headless server at the cost of not supporting SSH-key auth.

## History

- `b545cb4` (2026-02-11) — `sanitize_repo_url`; strip credentials from repo URLs
  in the dashboard API and logs.
- `5c28100` (2026-02-20) — per-user git credentials: `inject_credential_user`,
  `git_user` on `create_environment` / extra repos, persisted in Docker labels and
  template metadata, with a Git Credential picker in the UI.
- `259164e` (2026-02-20) — credentials management: auth verification via
  `git ls-remote` (replacing a shallow clone), list/validate/delete API + the
  dashboard Credentials tab, persistent `.git-credentials` store, and a
  credential helper configured at startup.
- security audit hardening (2026-07-19) — enforced "creds never in labels" on the
  **write** path: `extract_and_store_inline_credentials` moves inline
  `user:PAT@host` credentials into the credential store at `create_environment`
  time so the `oduflow.repo` label / template metadata only ever hold a sanitized
  URL (previously the raw URL was stored and only read paths sanitized).
  `redact_url_credentials` scrubs credential-bearing remotes from git stderr;
  `.git-credentials` dir/file modes forced to `0o700`/`0o600`.
