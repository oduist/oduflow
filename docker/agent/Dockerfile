# Oduflow per-team coding-agent image ("oduflow-coder").
#
# Published to Docker Hub as `oduist/oduflow-coder`. A SINGLE container serves
# one team (see env_ops lifecycle hooks): its HOME (auth + sessions) and
# /workspace (one checkout per environment at /workspace/<slug>) live on
# persistent named volumes, so a login done once survives container recreation
# and is shared by every environment's console. It drives environments purely
# through the Oduflow MCP server (git push -> pull_and_apply) -- it never
# touches host files. The web console execs the chosen agent on demand
# (`docker exec` with a TTY, workdir = the env checkout); ACP adapters serve
# the structured browser chat over the same bridge. The container just stays
# alive.
#
# LICENSING: this image bakes in only Apache-2.0 components (OpenAI Codex CLI
# and the ACP adapters' Codex side), which we may redistribute. Claude Code
# (@anthropic-ai/claude-code) and its ACP adapter are PROPRIETARY-adjacent
# (Anthropic Commercial Terms; the adapter pulls Anthropic's closed SDK), so
# they are NOT part of the published image: entrypoint.sh npm-installs them at
# first container start into an npm prefix on the persistent HOME volume. The
# end user downloads them from npm directly (like npx) -- we never
# redistribute them.
#
# Bump CODER_VERSION on every change and push :<version> + :latest
# (see AGENTS.md "Publishing the Coder Image").
# See specs/0029-agent-console-and-chat.md.

FROM node:22-bookworm-slim

ARG CODER_VERSION=0.1.0
LABEL org.opencontainers.image.title="oduflow-coder" \
      org.opencontainers.image.version="${CODER_VERSION}" \
      org.opencontainers.image.source="https://github.com/oduist/oduflow"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        jq \
    && rm -rf /var/lib/apt/lists/*

# Apache-2.0 components only (redistribution OK):
#   - OpenAI Codex CLI + its ACP (Agent Client Protocol) adapter.
# NOTE: the Codex ACP bridge package is still settling (published
# `@agentclientprotocol/codex-acp`, `@zed-industries/codex-acp` on GitHub, and an
# emerging native `codex --acp`). Confirm the working one at build time.
RUN npm install -g \
        @openai/codex \
        @agentclientprotocol/codex-acp \
    && npm cache clean --force

# Agent auth + sessions live under HOME (mounted from the persistent home
# volume: ~/.claude.json, ~/.claude/ incl. projects/ = sessions, ~/.codex/).
# Per-env checkouts live under /workspace (persistent workspace volume) at
# /workspace/<slug>. Config dirs are pinned under HOME explicitly so they land
# on the volume regardless of each CLI's default resolution. The npm prefix
# for the runtime-installed Claude packages also sits on the HOME volume
# (one-time download, survives recreation); its bin dir is on PATH for both
# the entrypoint and `docker exec` sessions.
ENV HOME=/root \
    CLAUDE_CONFIG_DIR=/root/.claude \
    CODEX_HOME=/root/.codex \
    NPM_CONFIG_PREFIX=/root/.npm-global \
    PATH=/root/.npm-global/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /workspace

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY clone-env.sh /usr/local/bin/clone-env.sh
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/clone-env.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
