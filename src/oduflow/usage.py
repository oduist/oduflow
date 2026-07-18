"""Per-environment LLM usage accounting (tokens / time / models).

The model itself cannot measure its own token consumption mid-conversation —
only the harness (Claude Code: the session transcript, ``/cost``) knows the
real numbers. So usage is fed by an external Claude Code hook that reads the
session transcript and POSTs it to the dashboard; there is deliberately no
self-report MCP tool (it would carry hallucinated numbers). Money is not
tracked — only tokens, wall time, and which models were used.

Three pieces of state:

1. **Live, per environment** — ``{workspace}/.usage.json`` (next to ``.note``).
   Sessions keyed by ``session_id`` so a hook firing repeatedly overwrites its
   own session (idempotent) while distinct sessions accumulate. Each session
   carries a per-model token breakdown, because the model can change within a
   single session.

2. **Archive, per team** — ``{team.data_dir}/usage.json``. When an environment
   is deleted (manually or by the reaper), its totals are folded in here keyed
   by ``env_name`` so accounting survives the ephemeral environment.

3. **Token index, per server** — ``{base_data_dir}/usage_tokens.json`` maps an
   opaque per-environment capability token (UID) to ``{team_id, env_name}``.
   The hook authenticates its POST with this UID via the ``X-Oduflow-Env-Uid``
   header; the UID both authorizes the call and identifies the environment, so
   no dashboard password is needed.

Writes follow the same concurrency discipline as ``oduflow.activity`` and
``oduflow.port_registry``: a per-path thread mutex plus an flock for sibling
processes, and an atomic temp-file + rename. All writes are best-effort and
must never break the operation they ride on.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import secrets
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from oduflow.naming import get_workspace_path
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")

_USAGE_FILE = ".usage.json"  # per-env, lives in the workspace
_ARCHIVE_FILE = "usage.json"  # per-team, lives in team.data_dir
_TOKENS_FILE = "usage_tokens.json"  # per-server, lives in base_data_dir

# Token counters tracked per model. Mirrors the Anthropic usage fields the
# transcript exposes (input/output plus the two cache buckets).
_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
)

_locks_guard = threading.Lock()
_path_locks: dict[str, threading.Lock] = {}


# -- paths --


def env_usage_path(team: TeamSettings, env_name: str) -> str:
    return os.path.join(get_workspace_path(env_name, team.workspaces_dir), _USAGE_FILE)


def archive_path(team: TeamSettings) -> str:
    return os.path.join(team.data_dir, _ARCHIVE_FILE)


def tokens_path(settings: Settings) -> str:
    return os.path.join(settings.base_data_dir, _TOKENS_FILE)


# -- concurrency + IO helpers (same discipline as oduflow.activity) --


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _thread_lock(path: str) -> threading.Lock:
    with _locks_guard:
        lock = _path_locks.get(path)
        if lock is None:
            lock = threading.Lock()
            _path_locks[path] = lock
        return lock


@contextmanager
def _file_lock(path: str) -> Iterator[None]:
    with _thread_lock(path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _load(path: str) -> dict[str, Any]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, ValueError, OSError) as e:
        logger.warning("Could not load usage file %s: %s", path, e)
        return {}


def _save(path: str, data: dict[str, Any]) -> None:
    dir_name = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix="usage.", suffix=".tmp", dir=dir_name)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _mutate(path: str, fn: Callable[[dict[str, Any]], bool]) -> None:
    """Run ``fn(data)`` under the lock; save when it returns True. Best-effort:
    a failed write logs a warning and never propagates."""
    try:
        with _file_lock(path):
            data = _load(path)
            if fn(data):
                _save(path, data)
    except OSError as e:
        logger.warning("Could not update usage file %s: %s", path, e)


# -- token normalization + aggregation --


def _clean_models(models: Any) -> dict[str, dict[str, int]]:
    """Coerce an incoming per-model token map to ``{model: {token_key: int}}``."""
    out: dict[str, dict[str, int]] = {}
    if not isinstance(models, dict):
        return out
    for name, toks in models.items():
        if not isinstance(toks, dict):
            continue
        cleaned = {k: _as_int(toks.get(k)) for k in _TOKEN_KEYS}
        if any(cleaned.values()) or str(name):
            out[str(name)] = cleaned
    return out


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _merge_models(
    into: dict[str, dict[str, int]], extra: dict[str, dict[str, int]]
) -> None:
    for name, toks in extra.items():
        agg = into.setdefault(name, {k: 0 for k in _TOKEN_KEYS})
        for k in _TOKEN_KEYS:
            agg[k] += _as_int(toks.get(k))


def _aggregate(sessions: dict[str, Any]) -> dict[str, Any]:
    """Roll up a per-session map into env-level totals + per-model breakdown."""
    models: dict[str, dict[str, int]] = {}
    duration = 0.0
    count = 0
    for sess in sessions.values():
        if not isinstance(sess, dict):
            continue
        count += 1
        duration += float(sess.get("duration_seconds") or 0)
        _merge_models(models, _clean_models(sess.get("models")))
    totals = {k: sum(m[k] for m in models.values()) for k in _TOKEN_KEYS}
    return {
        "sessions": count,
        "duration_seconds": duration,
        "models": models,
        "totals": totals,
    }


# -- live per-env recording --


def record(
    team: TeamSettings,
    env_name: str,
    *,
    session_id: str,
    models: dict[str, Any],
    duration_seconds: float = 0.0,
) -> None:
    """Upsert one session's usage into the env's live ``.usage.json``.

    Idempotent per ``session_id``: a hook re-firing for the same session
    overwrites its record (no double counting); distinct sessions accumulate.
    Skips quietly if the workspace no longer exists (deleted environment).
    """
    if not session_id:
        return
    workspace = get_workspace_path(env_name, team.workspaces_dir)
    if not os.path.isdir(workspace):
        return
    path = env_usage_path(team, env_name)
    now = _now_iso()

    def fn(data: dict[str, Any]) -> bool:
        sessions = data.setdefault("sessions", {})
        prev = sessions.get(session_id, {})
        sessions[session_id] = {
            "models": _clean_models(models),
            "duration_seconds": float(duration_seconds or 0),
            "first_seen": prev.get("first_seen", now)
            if isinstance(prev, dict)
            else now,
            "last_updated": now,
        }
        return True

    _mutate(path, fn)


def get_env_usage(team: TeamSettings, env_name: str) -> dict[str, Any]:
    """Aggregated usage for a live environment (totals + per-model + duration)."""
    data = _load(env_usage_path(team, env_name))
    sessions = data.get("sessions", {})
    return _aggregate(sessions if isinstance(sessions, dict) else {})


# -- archive on delete --


def archive_on_delete(team: TeamSettings, env_name: str) -> None:
    """Fold a deleted environment's totals into the team archive. Call BEFORE
    the workspace (and its ``.usage.json``) is removed."""
    live = get_env_usage(team, env_name)
    if not live["sessions"] and not any(live["totals"].values()):
        return
    path = archive_path(team)
    now = _now_iso()

    def fn(data: dict[str, Any]) -> bool:
        rec = data.get(env_name)
        if not isinstance(rec, dict):
            rec = {"models": {}, "duration_seconds": 0.0, "sessions": 0}
            data[env_name] = rec
        rec["duration_seconds"] = (
            float(rec.get("duration_seconds") or 0) + live["duration_seconds"]
        )
        rec["sessions"] = _as_int(rec.get("sessions")) + live["sessions"]
        rmodels = rec.setdefault("models", {})
        if not isinstance(rmodels, dict):
            rmodels = rec["models"] = {}
        _merge_models(rmodels, live["models"])
        rec["archived_at"] = now
        return True

    _mutate(path, fn)


def get_archive(team: TeamSettings) -> dict[str, Any]:
    """All archived (deleted-environment) usage records for a team."""
    return _load(archive_path(team))


# -- capability token index --


def _find_token(data: dict[str, Any], team_id: str, env_name: str) -> str:
    for token, rec in data.items():
        if (
            isinstance(rec, dict)
            and rec.get("team_id") == team_id
            and rec.get("env_name") == env_name
        ):
            return token
    return ""


def register_token(settings: Settings, team: TeamSettings, env_name: str) -> str:
    """Generate and persist a fresh capability token (UID) for an environment,
    dropping any stale token for the same (team, env). Returns the UID."""
    token = secrets.token_urlsafe(24)
    path = tokens_path(settings)

    def fn(data: dict[str, Any]) -> bool:
        stale = [
            k
            for k, v in data.items()
            if isinstance(v, dict)
            and v.get("team_id") == team.team_id
            and v.get("env_name") == env_name
        ]
        for k in stale:
            del data[k]
        data[token] = {"team_id": team.team_id, "env_name": env_name}
        return True

    _mutate(path, fn)
    return token


def get_token(settings: Settings, team: TeamSettings, env_name: str) -> str:
    """Return the existing capability token for an environment, or ``""``."""
    return _find_token(_load(tokens_path(settings)), team.team_id, env_name)


def get_or_create_token(settings: Settings, team: TeamSettings, env_name: str) -> str:
    """Return the environment's capability token, creating one if absent."""
    existing = get_token(settings, team, env_name)
    return existing or register_token(settings, team, env_name)


def resolve_token(settings: Settings, token: str) -> tuple[TeamSettings, str] | None:
    """Resolve a capability token to ``(team, env_name)`` or ``None``."""
    if not token:
        return None
    rec = _load(tokens_path(settings)).get(token)
    if not isinstance(rec, dict):
        return None
    team_id = rec.get("team_id")
    env_name = rec.get("env_name")
    if not team_id or not env_name or team_id not in settings.teams:
        return None
    return settings.teams[team_id], str(env_name)


def revoke_token(settings: Settings, team: TeamSettings, env_name: str) -> None:
    """Drop all capability tokens for an environment (call on delete)."""
    path = tokens_path(settings)

    def fn(data: dict[str, Any]) -> bool:
        stale = [
            k
            for k, v in data.items()
            if isinstance(v, dict)
            and v.get("team_id") == team.team_id
            and v.get("env_name") == env_name
        ]
        for k in stale:
            del data[k]
        return bool(stale)

    _mutate(path, fn)
