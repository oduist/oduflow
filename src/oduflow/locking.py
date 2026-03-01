"""Granular lock manager for concurrent MCP tool execution.

Provides per-branch, per-team, and system-level locks so that operations on
different branches / teams can run in parallel while operations on the same
resource are serialised.
"""

from __future__ import annotations

import threading

from oduflow.errors import BusyError


class LockManager:
    """Thread-safe lock manager with per-branch and per-team granularity."""

    def __init__(self) -> None:
        self._branch_locks: dict[str, threading.Lock] = {}
        self._team_locks: dict[str, threading.Lock] = {}
        self._system_lock = threading.Lock()
        self._map_lock = threading.Lock()  # protects dict access

    # -- branch locks --

    def _get_branch_lock(self, branch_name: str) -> threading.Lock:
        with self._map_lock:
            if branch_name not in self._branch_locks:
                self._branch_locks[branch_name] = threading.Lock()
            return self._branch_locks[branch_name]

    def acquire_branch(self, branch_name: str) -> None:
        lock = self._get_branch_lock(branch_name)
        if not lock.acquire(blocking=False):
            raise BusyError(
                f"Another operation on branch '{branch_name}' is in progress. "
                "Try again later."
            )

    def release_branch(self, branch_name: str) -> None:
        lock = self._get_branch_lock(branch_name)
        try:
            lock.release()
        except RuntimeError:
            pass

    # -- team locks --

    def _get_team_lock(self, team_id: str) -> threading.Lock:
        with self._map_lock:
            if team_id not in self._team_locks:
                self._team_locks[team_id] = threading.Lock()
            return self._team_locks[team_id]

    def acquire_team(self, team_id: str) -> None:
        lock = self._get_team_lock(team_id)
        if not lock.acquire(blocking=False):
            raise BusyError(
                f"Another team-level operation (team '{team_id}') is in progress. "
                "Try again later."
            )

    def release_team(self, team_id: str) -> None:
        lock = self._get_team_lock(team_id)
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
