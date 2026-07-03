#!/usr/bin/env bash
# Runtime setup for the Oduflow per-team agent container.
#
# A single container serves every environment of one team. Its HOME (auth +
# sessions) and /workspace (one checkout per environment) are persistent named
# volumes, so a login done once survives recreation and is shared by all of the
# team's environments.
#
# Responsibilities (all idempotent, all team-wide):
#   1. Wire git credentials (the team's Oduflow credential store, mounted
#      read-only) into HOME (on the persistent home volume).
#   2. Install Claude Code + its ACP adapter at FIRST start, from npm, into
#      the npm prefix on the persistent home volume. They are proprietary
#      (Anthropic Commercial Terms), so the published image must not
#      redistribute them; the container downloads them directly, once.
#   3. Write the GLOBAL Oduflow MCP config for Codex (team endpoint/token).
#   4. Resolve provider auth: subscription if a subscription credential is
#      present, otherwise an API key (per provider).
#   5. Stay alive. Per-env checkouts are created on demand by the env_ops hooks
#      (`docker exec clone-env.sh ...`); the agent process itself is spawned on
#      demand by the web console via `docker exec` (claude | codex).
#
# Expected environment (injected by _ensure_agent_container; team-wide):
#   ODUFLOW_GIT_USER    credential username to match in the store (optional)
#   ODUFLOW_MCP_URL     Oduflow MCP endpoint, e.g. http://host.docker.internal:8000/mcp
#   ODUFLOW_MCP_TOKEN   Bearer token for the MCP endpoint (team auth_token)
# Provider auth (subscription takes precedence over key, per provider):
#   CLAUDE_CODE_OAUTH_TOKEN | ANTHROPIC_API_KEY   (+ optional ANTHROPIC_MODEL)
#   OPENAI_API_KEY                                 (+ optional CODEX_MODEL)
# Mounts:
#   /run/oduflow/git-credentials   git credential store (read-only)
#   /run/oduflow/codex-auth.json   optional Codex subscription seed (auth.json)
#   HOME (/root)                   persistent home volume (auth + sessions)
#   /workspace                     persistent workspace volume (per-env checkouts)

set -euo pipefail

log() { printf '[agent-entrypoint] %s\n' "$*" >&2; }

CRED_SRC="/run/oduflow/git-credentials"
CRED_FILE="$HOME/.git-credentials"

# --- 1. git credentials ------------------------------------------------------
git config --global user.name  "${ODUFLOW_GIT_USER:-oduflow-coder}"
git config --global user.email "${ODUFLOW_GIT_USER:-agent}@oduflow.local"
if [ -f "$CRED_SRC" ]; then
    # Copy so refresh/rotation never writes back to the shared store.
    cp "$CRED_SRC" "$CRED_FILE"
    chmod 600 "$CRED_FILE"
    git config --global credential.helper "store --file=$CRED_FILE"
    log "git credential store wired"
else
    log "WARNING: no git credential store at $CRED_SRC (private repos will fail)"
fi

# --- 2. Claude Code: runtime install onto the home volume --------------------
# Not baked into the published image (proprietary; see the Dockerfile header).
# NPM_CONFIG_PREFIX points at $HOME/.npm-global (persistent volume), so this
# downloads once per volume, not once per container. Failure is non-fatal:
# Codex still works; Claude consoles/chats will report the missing binary.
if command -v claude >/dev/null 2>&1 && command -v claude-code-acp >/dev/null 2>&1; then
    log "Claude Code present ($(command -v claude))"
else
    log "installing Claude Code + ACP adapter from npm (first start; persists on the home volume)"
    if npm install -g @anthropic-ai/claude-code @zed-industries/claude-code-acp >/dev/null 2>&1; then
        log "Claude Code installed"
    else
        log "WARNING: Claude Code install failed (no network?); Claude consoles/chats will not work until it succeeds on a restart"
    fi
fi

# --- 3. global MCP config for Codex -----------------------------------------
# The Oduflow endpoint/token are team-wide, so one global config serves every
# checkout. (Claude Code reads a project-scoped .mcp.json written per checkout
# by clone-env.sh.) Streamable-HTTP MCP needs the rmcp client; auth is via
# `bearer_token_env_var` (NOT `bearer_token`, which Codex rejects for
# streamable_http) and only when a token is actually set.
mkdir -p "$CODEX_HOME"
if [ -n "${ODUFLOW_MCP_URL:-}" ]; then
    {
        echo "[features]"
        echo "experimental_use_rmcp_client = true"
        echo ""
        echo "[mcp_servers.oduflow]"
        echo "url = \"$ODUFLOW_MCP_URL\""
        if [ -n "${ODUFLOW_MCP_TOKEN:-}" ]; then
            echo 'bearer_token_env_var = "ODUFLOW_MCP_TOKEN"'
        fi
    } > "$CODEX_HOME/config.toml"
    log "global MCP config written for oduflow -> $ODUFLOW_MCP_URL"
else
    log "WARNING: ODUFLOW_MCP_URL unset; agents cannot reach environments"
fi

# --- 4a. Claude auth ---------------------------------------------------------
# Subscription (CLAUDE_CODE_OAUTH_TOKEN) outranks a key. If the OAuth token is
# present, ANTHROPIC_API_KEY MUST be unset or it wins by precedence. With no
# credential at all, an interactive `claude` login persists to the home volume.
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    unset ANTHROPIC_API_KEY || true
    log "Claude auth: subscription (CLAUDE_CODE_OAUTH_TOKEN)"
elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    log "Claude auth: API key (ANTHROPIC_API_KEY)"
else
    log "Claude auth: NONE configured (use an interactive login; it persists on the home volume)"
fi

# --- 4b. Codex auth ----------------------------------------------------------
# API key is the reliable path. Subscription via auth.json is seed-if-missing
# only (never overwrite -- Codex rewrites it on refresh). With no credential, an
# interactive `codex login` persists to the home volume. Note: one subscription
# shared across many concurrent sessions can still hit refresh_token_reused --
# prefer an API key.
mkdir -p "$CODEX_HOME"
if [ -n "${OPENAI_API_KEY:-}" ]; then
    log "Codex auth: API key (OPENAI_API_KEY)"
elif [ -f "/run/oduflow/codex-auth.json" ] && [ ! -f "$CODEX_HOME/auth.json" ]; then
    cp "/run/oduflow/codex-auth.json" "$CODEX_HOME/auth.json"
    chmod 600 "$CODEX_HOME/auth.json"
    log "Codex auth: subscription seed (auth.json copied; container owns it now)"
else
    log "Codex auth: ${OPENAI_API_KEY:+key}${OPENAI_API_KEY:-none/pre-seeded (interactive login persists)}"
fi

# --- 5. stay alive -----------------------------------------------------------
log "ready; per-env checkouts and agent processes are spawned on demand"
exec sleep infinity
