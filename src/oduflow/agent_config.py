"""Coding-agent type validation and per-team default resolution.

The agent is configured statically in ``oduflow.toml``: per team
``agent_enabled`` / ``agent_default`` / ``[team.X.agent_env]`` (see
:class:`oduflow.settings.TeamSettings`). This module only owns the list of
valid agents and the resolution of a connection's agent type — there is no
runtime-editable state.
"""

from __future__ import annotations

from oduflow.settings import TeamSettings

# The coding agents Oduflow can run. This is the single source of truth for
# validation on the Python side — the web UI and the ACP/console relays validate
# through resolve_agent_type() rather than their own literals. The browser keeps
# a small mirror of this list in chat.js / dashboard.html (JS cannot import this
# constant), so a new agent must be added there too.
VALID_AGENTS: tuple[str, ...] = ("claude", "codex", "opencode")
FALLBACK_AGENT = "claude"


def effective_agent_default(team: TeamSettings) -> str:
    """The team's default agent: the configured ``agent_default`` when valid,
    else :data:`FALLBACK_AGENT`. Always valid."""
    configured = (team.agent_default or "").strip().lower()
    return configured if configured in VALID_AGENTS else FALLBACK_AGENT


def resolve_agent_type(requested: str | None, team: TeamSettings) -> str:
    """Pick the agent for a connection: the client-requested type if it is one of
    :data:`VALID_AGENTS`, otherwise the team default. Always returns a valid
    value, so callers never need their own agent-name check."""
    candidate = (requested or "").strip().lower()
    if candidate in VALID_AGENTS:
        return candidate
    return effective_agent_default(team)
