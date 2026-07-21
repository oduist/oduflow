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
#   3. Write the base Codex config (feature flags + built-in Agent Browser MCP;
#      NO Oduflow endpoint or token here) and add Agent Browser to existing
#      Claude checkout configs; see the MCP note below.
#   4. Resolve provider auth: subscription if a subscription credential is
#      present, otherwise an API key (per provider).
#   5. Stay alive. Per-env checkouts are created on demand by the env_ops hooks
#      (`docker exec clone-env.sh ...`); the agent process itself is spawned on
#      demand by the web console via `docker exec` (claude | codex).
#
# MCP wiring is per SESSION, not per container: the container holds no Oduflow
# token at all (nothing in its env or on disk to read from a console). Each
# console/chat exec injects ODUFLOW_MCP_TOKEN — the SCOPED per-environment
# token (see ADR 0028) — plus the /mcp/<env> URL into its own environment;
# Claude reads them through the ${VAR} placeholders in the checkout's
# .mcp.json. Codex CLI uses `-c mcp_servers.*` overrides; the web relay adds
# the same scoped server to Codex ACP session-open requests. Agent Browser is a
# local stdio MCP available to both agents and contains no Oduflow credential.
#
# Expected environment (injected by _ensure_agent_container; team-wide):
#   ODUFLOW_GIT_USER    credential username to match in the store (optional)
# Provider auth (subscription takes precedence over key, per provider):
#   CLAUDE_CODE_OAUTH_TOKEN | ANTHROPIC_API_KEY   (+ optional ANTHROPIC_MODEL)
#   OPENAI_API_KEY                                 (+ optional CODEX_MODEL)
# Mounts:
#   HOME already contains the copied team git credential store; a readable
#   /run/oduflow/git-credentials is also accepted for standalone use.
#   /run/oduflow/codex-auth.json   optional Codex subscription seed (auth.json)
#   HOME (/home/agent)             persistent home volume (auth + sessions)
#   /workspace                     persistent workspace volume (per-env checkouts)

set -euo pipefail

log() { printf '[agent-entrypoint] %s\n' "$*" >&2; }

CRED_SRC="/run/oduflow/git-credentials"
CRED_FILE="$HOME/.git-credentials"

# --- 1. git credentials ------------------------------------------------------
git config --global user.name  "${ODUFLOW_GIT_USER:-oduflow-coder}"
git config --global user.email "${ODUFLOW_GIT_USER:-agent}@oduflow.local"
if [ -r "$CRED_SRC" ]; then
    # Copy so refresh/rotation never writes back to the shared store.
    cp "$CRED_SRC" "$CRED_FILE"
    chmod 600 "$CRED_FILE"
fi
if [ -f "$CRED_FILE" ]; then
    git config --global credential.helper "store --file=$CRED_FILE"
    log "git credential store wired"
else
    log "WARNING: no git credential store in HOME (private repos will fail)"
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

# --- 3. base Codex config ----------------------------------------------------
# Streamable-HTTP MCP needs the rmcp client. The Oduflow MCP server itself is
# deliberately NOT configured here: its URL and token are per environment and
# per session (scoped tokens, ADR 0028) and are supplied by the web console as
# `codex -c mcp_servers.oduflow.*` overrides + the session's exec env. Keeping
# them out of the global config means no session can see another environment's
# credentials — and the container itself holds no MCP secret at all. Agent
# Browser is safe to configure globally because it is local and credentialless;
# AGENT_BROWSER_SESSION on each exec isolates its browser state per environment.
mkdir -p "$CODEX_HOME"
{
    echo "[features]"
    echo "experimental_use_rmcp_client = true"
    echo
    echo "[mcp_servers.agent_browser]"
    echo 'command = "agent-browser"'
    echo 'args = ["mcp", "--tools", "all"]'
    echo 'env_vars = ["AGENT_BROWSER_SESSION", "AGENT_BROWSER_EXECUTABLE_PATH"]'
} > "$CODEX_HOME/config.toml"
log "base Codex config written (Agent Browser built in; Oduflow is per session)"

# Existing checkouts predate the built-in browser and survive image/container
# recreation on the workspace volume. Merge the local server into their
# generated Claude configs in place so they gain the capability immediately;
# preserve each checkout's scoped Oduflow entry and any custom MCP servers.
shopt -s nullglob
for git_dir in /workspace/*/.git; do
    checkout="${git_dir%/.git}"
    mcp_file="$checkout/.mcp.json"
    mcp_tmp="$mcp_file.tmp.$$"
    if [ -f "$mcp_file" ]; then
        if ! jq '.mcpServers = ((if (.mcpServers | type) == "object" then .mcpServers else {} end) + {agent_browser: {type: "stdio", command: "agent-browser", args: ["mcp", "--tools", "all"]}})' \
            "$mcp_file" > "$mcp_tmp"; then
            rm -f "$mcp_tmp"
            log "WARNING: could not add Agent Browser to malformed $mcp_file"
            continue
        fi
    else
        jq -n '{mcpServers: {agent_browser: {type: "stdio", command: "agent-browser", args: ["mcp", "--tools", "all"]}}}' \
            > "$mcp_tmp"
    fi
    mv "$mcp_tmp" "$mcp_file"
    mkdir -p "$checkout/.git/info"
    if ! grep -qxF '/.mcp.json' "$checkout/.git/info/exclude" 2>/dev/null; then
        echo '/.mcp.json' >> "$checkout/.git/info/exclude"
    fi
done
shopt -u nullglob
log "existing Claude checkout MCP configs refreshed"

# --- 4a. Claude auth ---------------------------------------------------------
# Subscription (CLAUDE_CODE_OAUTH_TOKEN) outranks a key. If the OAuth token is
# present, ANTHROPIC_API_KEY MUST be unset or it wins by precedence. With no
# credential at all, an interactive `claude` login persists to the home volume.
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    # ANTHROPIC_API_KEY is already excluded from the container env by
    # _agent_env_vars() in env_ops.py; this unset is belt-and-suspenders for
    # the entrypoint's own shell only (docker exec sessions inherit the
    # container-level env, which already lacks the key).
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
    # Current Codex releases require an explicit login to materialize auth.json;
    # merely inheriting OPENAI_API_KEY is not enough. This is idempotent and
    # intentionally runs on every start so key rotation updates the persistent
    # HOME volume. The key is passed on stdin and never appears in argv/logs.
    if printf '%s' "$OPENAI_API_KEY" | codex login --with-api-key >/dev/null 2>&1; then
        log "Codex auth: API key (OPENAI_API_KEY) -> auth.json written"
    else
        log "WARNING: codex login --with-api-key failed (Codex will prompt for auth)"
    fi
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
