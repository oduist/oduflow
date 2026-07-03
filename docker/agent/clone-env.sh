#!/usr/bin/env bash
# Create/refresh one environment's working checkout inside the team's agent
# container. Invoked from the env_ops create hook via `docker exec`. Idempotent.
#
# Usage: clone-env.sh REPO_URL BRANCH SLUG [MCP_URL] [MCP_TOKEN] [GIT_USER]
#   REPO_URL    origin repo URL (no embedded creds; git creds are wired globally)
#   BRANCH      branch to check out
#   SLUG        filesystem-safe env slug; checkout goes to /workspace/<SLUG>
#   MCP_URL     Oduflow MCP endpoint (optional; enables Claude's .mcp.json)
#   MCP_TOKEN   Bearer token for the MCP endpoint (optional)
#   GIT_USER    commit author for this checkout (optional; global default otherwise)
#
# Full clone (not --depth 1): the agent commits and pushes. The checkout lives on
# the persistent workspace volume, so it survives container recreation and every
# other environment's checkout is visible under /workspace for cross-branch reads.

set -euo pipefail

log() { printf '[clone-env] %s\n' "$*" >&2; }

REPO_URL="${1:-}"
BRANCH="${2:-}"
SLUG="${3:-}"
MCP_URL="${4:-}"
MCP_TOKEN="${5:-}"
GIT_USER="${6:-}"

if [ -z "$REPO_URL" ] || [ -z "$BRANCH" ] || [ -z "$SLUG" ]; then
    log "WARNING: REPO_URL/BRANCH/SLUG required; skipping"
    exit 0
fi

DEST="/workspace/$SLUG"

if [ ! -d "$DEST/.git" ]; then
    log "cloning $BRANCH into $DEST"
    git clone --branch "$BRANCH" "$REPO_URL" "$DEST"
else
    # Refresh an existing checkout to the target branch at origin's tip. Fetch
    # failures are NOT suppressed (set -e aborts) so a broken refresh surfaces to
    # the caller instead of silently opening a console on stale code.
    log "checkout exists; refreshing $BRANCH in $DEST"
    git -C "$DEST" fetch origin "$BRANCH"
    # Only *tracked* modifications count as work to protect: `checkout -B` cannot
    # clobber untracked files (e.g. the generated .mcp.json), so ignoring them
    # keeps the refresh from being permanently blocked by our own artifacts.
    if [ -n "$(git -C "$DEST" status --porcelain --untracked-files=no)" ]; then
        # The agent has uncommitted work here. Never clobber it — but be loud so a
        # "refresh" is not mistaken for a clean sync to origin.
        log "WARNING: uncommitted changes to tracked files in $DEST; leaving the working tree as-is (NOT synced to origin/$BRANCH)"
    else
        # Clean tree: nothing to lose. Pin it exactly on the target branch at
        # origin's tip (fixes both a wrong branch and a stale commit).
        git -C "$DEST" checkout -B "$BRANCH" "origin/$BRANCH"
        log "synced $DEST to origin/$BRANCH"
    fi
fi

# Per-checkout commit author (the container's global identity is a shared default;
# a single container serves many environments/users).
if [ -n "$GIT_USER" ]; then
    git -C "$DEST" config user.name "$GIT_USER"
    git -C "$DEST" config user.email "${GIT_USER}@oduflow.local"
fi

# Claude Code: project-scoped .mcp.json at the checkout root.
if [ -n "$MCP_URL" ]; then
    jq -n \
        --arg url "$MCP_URL" \
        --arg auth "Bearer ${MCP_TOKEN}" \
        '{mcpServers: {oduflow: {type: "http", url: $url, headers: {Authorization: $auth}}}}' \
        > "$DEST/.mcp.json"
    log "wrote $DEST/.mcp.json -> $MCP_URL"
fi
