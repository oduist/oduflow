"""Persistent ACP chat session ids, per environment and agent.

The ACP chat (dashboard modal, see ``web_ui.ws_agent_acp``) keeps **one durable
conversation per environment per agent**: opening the chat resumes the stored
session via ACP ``session/load`` instead of starting a fresh one, so the user
always continues the same thread. The underlying agent process is spawned per
WebSocket connection (``docker exec`` of the ACP adapter) and the transcript
itself lives on the agent home volume; here we only persist the *session id*
needed to reattach.

A single JSON bag per team at ``<team.data_dir>/agent_sessions.json`` maps
``{branch: {agent_type: session_id}}``. The server runs as a single process,
so a module-level lock guards the read-modify-write of the whole bag against
concurrent chats to different environments (each ``session/new`` persists
here). See specs/0029-agent-console-and-chat.md.
"""

from __future__ import annotations

import json
import os
import threading

from oduflow.settings import TeamSettings

# The server is single-process; this guards the whole-file read-modify-write so
# concurrent chats (different branches) don't clobber each other's session ids.
_LOCK = threading.Lock()


def _path(team: TeamSettings) -> str:
    return os.path.join(team.data_dir, "agent_sessions.json")


def _load(team: TeamSettings) -> dict[str, dict[str, str]]:
    path = _path(team)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for branch, per_agent in data.items():
        if isinstance(per_agent, dict):
            out[str(branch)] = {str(k): str(v) for k, v in per_agent.items() if v}
    return out


def _save(team: TeamSettings, data: dict[str, dict[str, str]]) -> None:
    os.makedirs(team.data_dir, exist_ok=True)
    path = _path(team)
    tmp = f"{path}.tmp"
    # 0o600 from birth: os.replace carries the tmp file's mode over, so the
    # session ids are never readable beyond the owner, even briefly.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def get_session(team: TeamSettings, branch: str, agent_type: str) -> str | None:
    """Return the stored session id for (branch, agent_type), or None."""
    with _LOCK:
        return _load(team).get(branch, {}).get(agent_type) or None


def set_session(
    team: TeamSettings, branch: str, agent_type: str, session_id: str
) -> None:
    """Persist the current session id for (branch, agent_type)."""
    with _LOCK:
        data = _load(team)
        data.setdefault(branch, {})[agent_type] = session_id
        _save(team, data)


def clear_session(
    team: TeamSettings, branch: str, agent_type: str | None = None
) -> None:
    """Forget the stored session(s). Clears one agent, or the whole branch when
    ``agent_type`` is None (used when an environment is deleted)."""
    with _LOCK:
        data = _load(team)
        if branch not in data:
            return
        if agent_type is None:
            data.pop(branch, None)
        else:
            data[branch].pop(agent_type, None)
            if not data[branch]:
                data.pop(branch, None)
        _save(team, data)
