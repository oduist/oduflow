"""Granular lock manager for concurrent MCP tool execution.

Provides per-environment, per-team, and system-level locks so that operations on
different environments / teams can run in parallel while operations on the same
resource are serialised.
"""

from __future__ import annotations

import threading

from oduflow.errors import BusyError


class LockManager:
    """Thread-safe lock manager with per-environment and per-team granularity."""

    def __init__(self) -> None:
        self._env_locks: dict[str, threading.Lock] = {}
        self._team_locks: dict[str, threading.Lock] = {}
        self._active_team_locks: set[str] = set()
        self._env_lock_teams: dict[str, str] = {}
        self._active_env_counts_by_team: dict[str, int] = {}
        self._system_lock = threading.Lock()
        self._map_lock = threading.Lock()  # protects dict access

    # -- environment locks --

    def _get_env_lock(self, env_name: str) -> threading.Lock:
        with self._map_lock:
            if env_name not in self._env_locks:
                self._env_locks[env_name] = threading.Lock()
            return self._env_locks[env_name]

    def acquire_env(self, env_name: str, team_id: str | None = None) -> None:
        with self._map_lock:
            if team_id is not None and team_id in self._active_team_locks:
                raise BusyError(
                    f"Another team-level operation (team '{team_id}') is in progress. "
                    "Try again later."
                )
            if env_name not in self._env_locks:
                self._env_locks[env_name] = threading.Lock()
            lock = self._env_locks[env_name]
            if not lock.acquire(blocking=False):
                raise BusyError(
                    f"Another operation on environment '{env_name}' is in progress. "
                    "Try again later."
                )
            if team_id is not None:
                self._env_lock_teams[env_name] = team_id
                self._active_env_counts_by_team[team_id] = (
                    self._active_env_counts_by_team.get(team_id, 0) + 1
                )

    def release_env(self, env_name: str) -> None:
        with self._map_lock:
            lock = self._env_locks.get(env_name)
            if lock is None:
                return
            try:
                lock.release()
            except RuntimeError:
                return
            team_id = self._env_lock_teams.pop(env_name, None)
            if team_id is not None:
                count = self._active_env_counts_by_team.get(team_id, 0) - 1
                if count > 0:
                    self._active_env_counts_by_team[team_id] = count
                else:
                    self._active_env_counts_by_team.pop(team_id, None)

    # -- team locks --

    def _get_team_lock(self, team_id: str) -> threading.Lock:
        with self._map_lock:
            if team_id not in self._team_locks:
                self._team_locks[team_id] = threading.Lock()
            return self._team_locks[team_id]

    def acquire_team(self, team_id: str) -> None:
        with self._map_lock:
            if team_id not in self._team_locks:
                self._team_locks[team_id] = threading.Lock()
            lock = self._team_locks[team_id]
            if (
                team_id in self._active_team_locks
                or self._active_env_counts_by_team.get(team_id, 0) > 0
                or not lock.acquire(blocking=False)
            ):
                raise BusyError(
                    f"Another team-level operation (team '{team_id}') is in progress. "
                    "Try again later."
                )
            self._active_team_locks.add(team_id)

    def release_team(self, team_id: str) -> None:
        with self._map_lock:
            lock = self._team_locks.get(team_id)
            if lock is None:
                return
            self._active_team_locks.discard(team_id)
            try:
                lock.release()
            except RuntimeError:
                pass

    # -- system lock --

    def acquire_system(self) -> None:
        if not self._system_lock.acquire(blocking=False):
            raise BusyError("A system-level operation is in progress. Try again later.")

    def release_system(self) -> None:
        try:
            self._system_lock.release()
        except RuntimeError:
            pass
