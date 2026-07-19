"""Per-environment activity tracking for automatic lifecycle management.

Records, per team, when an environment was last *worked with* (any env-scoped
MCP tool call or dashboard lifecycle action) and when it stopped. The reaper
(`oduflow.reaper`) reads these records to auto-stop idle environments and
auto-delete long-stopped ones.

Listing environments and dashboard polling deliberately do NOT count as
activity — an open dashboard tab must not keep the whole fleet "active".

State lives in ``activity.json`` next to ``ports.json`` in the team data dir:

    {"<env>": {"last_activity": iso, "stopped_at": iso, "stopped_by": "auto"}}

``stopped_at``/``stopped_by`` are present only while the environment is
stopped. Writes follow the same concurrency discipline as the port registry:
a per-path mutex for threads plus an flock for sibling processes, and a
unique temp file per write.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from oduflow.settings import TeamSettings

logger = logging.getLogger("oduflow")

_FILENAME = "activity.json"

_locks_guard = threading.Lock()
_path_locks: dict[str, threading.Lock] = {}


def activity_path(team: TeamSettings) -> str:
    return os.path.join(team.data_dir, _FILENAME)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: str | None) -> float | None:
    """ISO timestamp -> unix epoch seconds, or None if absent/invalid."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Records written by _now_iso() are tz-aware; a legacy/hand-edited naive
    # value would otherwise be read as host-local time, shifting reaper
    # stop/delete decisions by the UTC offset. Treat naive as UTC.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


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


def _load(path: str) -> dict[str, dict[str, Any]]:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path) as f:
            data = json.load(f)
        return {k: dict(v) for k, v in data.items() if isinstance(v, dict)}
    except (json.JSONDecodeError, ValueError, OSError) as e:
        logger.warning("Could not load activity file %s: %s", path, e)
        return {}


def _save(path: str, records: dict[str, dict[str, Any]]) -> None:
    dir_name = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix="activity.", suffix=".tmp", dir=dir_name)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(records, f, indent=2)
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


def _mutate(team: TeamSettings, fn: Any) -> None:
    """Run ``fn(records)`` under the lock; save when it returns True."""
    if not team.data_dir:
        return
    path = activity_path(team)
    try:
        with _file_lock(path):
            records = _load(path)
            if fn(records):
                _save(path, records)
    except OSError as e:
        # Activity tracking is best-effort; never break the operation it rides on.
        logger.warning("Could not update activity file %s: %s", path, e)


def get_all(team: TeamSettings) -> dict[str, dict[str, Any]]:
    """All activity records for a team (read-only snapshot)."""
    if not team.data_dir:
        return {}
    return _load(activity_path(team))


def touch(team: TeamSettings, env_name: str) -> None:
    """Record that the environment was worked with just now."""

    def fn(records: dict[str, dict[str, Any]]) -> bool:
        rec = records.setdefault(env_name, {})
        rec["last_activity"] = _now_iso()
        return True

    _mutate(team, fn)


def mark_stopped(team: TeamSettings, env_name: str, by: str = "manual") -> None:
    """Record when (and how) the environment stopped. Keeps an existing
    ``stopped_at`` so re-marking does not extend the auto-delete clock,
    but upgrades the attribution (e.g. observed -> auto)."""

    def fn(records: dict[str, dict[str, Any]]) -> bool:
        rec = records.setdefault(env_name, {})
        rec.setdefault("stopped_at", _now_iso())
        rec["stopped_by"] = by
        return True

    _mutate(team, fn)


def mark_started(team: TeamSettings, env_name: str) -> None:
    """The environment is running again: clear the stopped clock and count
    the start itself as activity."""

    def fn(records: dict[str, dict[str, Any]]) -> bool:
        rec = records.setdefault(env_name, {})
        rec.pop("stopped_at", None)
        rec.pop("stopped_by", None)
        rec["last_activity"] = _now_iso()
        return True

    _mutate(team, fn)


def remove(team: TeamSettings, env_name: str) -> None:
    """Drop the record of a deleted environment."""

    def fn(records: dict[str, dict[str, Any]]) -> bool:
        return records.pop(env_name, None) is not None

    _mutate(team, fn)


def prune(team: TeamSettings, existing: set[str]) -> None:
    """Drop records of environments that no longer exist."""

    def fn(records: dict[str, dict[str, Any]]) -> bool:
        stale = [name for name in records if name not in existing]
        for name in stale:
            del records[name]
        return bool(stale)

    _mutate(team, fn)
