"""Persistent store for minted OAuth access/refresh tokens.

The self-hosted OAuth server (:mod:`oduflow.oauth_provider`) issues independent,
opaque, expiring access tokens (with rotating refresh tokens) instead of handing
the team ``auth_token`` to the OAuth client. Those minted tokens must survive a
server restart — otherwise every restart (upgrade, config edit) would drop live
claude.ai / IDE connections — so they are persisted to a JSON file, using the
same lock + atomic-write pattern as :mod:`oduflow.port_registry`.

``load_access_token`` runs on every authenticated request, so reads are served
from an in-memory cache; the JSON file is the durable source of truth, read on
startup and re-read on a cache miss (so a token minted or revoked by another
worker process is eventually picked up). Writes (mint / rotate / revoke / expiry
cleanup) run under a cross-thread + cross-process lock and rewrite the file
atomically with ``0o600`` perms (the file holds bearer secrets).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, cast

logger = logging.getLogger("oduflow")

# {"access": {token: record}, "refresh": {token: record}}; records are plain
# JSON dicts (see ``mint_pair`` for their shape).
StoreData = dict[str, dict[str, Any]]

# Bytes of entropy per opaque token (``secrets.token_urlsafe`` argument).
_TOKEN_BYTES = 32


# The store is process-shared state on the data dir: a per-path thread mutex
# (parallel requests) plus an flock on a sidecar file (multiple oduflow
# processes) guard every read-modify-write cycle. Mirrors ``port_registry``.
_locks_guard = threading.Lock()
_path_locks: dict[str, threading.Lock] = {}


def _thread_lock(path: str) -> threading.Lock:
    with _locks_guard:
        lock = _path_locks.get(path)
        if lock is None:
            lock = threading.Lock()
            _path_locks[path] = lock
        return lock


@contextmanager
def _file_lock(path: str) -> Iterator[None]:
    """Serialize a read-modify-write across threads and processes."""
    with _thread_lock(path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _empty() -> StoreData:
    data: StoreData = {"access": {}, "refresh": {}}
    return data


def _load_file(path: str) -> StoreData:
    """Load the store from disk; empty dict if missing or corrupt."""
    if not os.path.isfile(path):
        return _empty()
    try:
        with open(path) as f:
            raw = json.load(f)
    except (json.JSONDecodeError, ValueError, OSError) as e:
        logger.warning("Could not load OAuth token store %s: %s", path, e)
        return _empty()
    if not isinstance(raw, dict):
        return _empty()
    access = raw.get("access", {})
    refresh = raw.get("refresh", {})
    if not isinstance(access, dict) or not isinstance(refresh, dict):
        return _empty()
    return cast(StoreData, {"access": access, "refresh": refresh})


def _save_file(path: str, data: StoreData) -> None:
    """Atomically save the store (mode ``0o600``). Callers hold ``_file_lock``."""
    dir_name = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix="oauth_tokens.", suffix=".tmp", dir=dir_name)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _prune_expired(data: StoreData, now: float) -> bool:
    """Drop expired access tokens and their refresh partners. Returns whether
    anything was removed."""
    removed = False
    for token, rec in list(data["access"].items()):
        expires_at = rec.get("expires_at")
        if expires_at is not None and expires_at < now:
            del data["access"][token]
            partner = rec.get("refresh")
            if partner:
                data["refresh"].pop(partner, None)
            removed = True
    return removed


class OAuthTokenStore:
    """Persistent, in-memory-cached store of minted OAuth tokens.

    A minted access token's stored ``client_id`` is the numeric ``team_id`` (the
    key in ``settings.teams``), so a resolved :class:`AccessToken` routes to the
    right team exactly like the preseeded ``auth_token`` does. ``scopes`` is
    empty for team tokens (full access).
    """

    def __init__(self, path: str, access_ttl: int) -> None:
        self._path = path
        self._access_ttl = access_ttl
        self._cache_lock = threading.Lock()
        self._access: dict[str, Any] = {}
        self._refresh: dict[str, Any] = {}
        self._mtime: float | None = None
        self._reload()

    # -- cache management --

    def _reload(self) -> None:
        """Refresh the cache from disk, pruning expired access tokens."""
        with _file_lock(self._path):
            data = _load_file(self._path)
            if _prune_expired(data, time.time()):
                _save_file(self._path, data)
            self._set_cache(data)

    def _set_cache(self, data: StoreData) -> None:
        with self._cache_lock:
            self._access = data["access"]
            self._refresh = data["refresh"]
            try:
                self._mtime = os.path.getmtime(self._path)
            except OSError:
                self._mtime = None

    def _maybe_reload(self) -> None:
        """Reload only if another process rewrote the file since our last read."""
        try:
            mtime: float | None = os.path.getmtime(self._path)
        except OSError:
            mtime = None
        if mtime != self._mtime:
            self._reload()

    # -- reads (per-request hot path) --

    def get_access(self, token: str) -> dict[str, Any] | None:
        with self._cache_lock:
            rec = self._access.get(token)
        if rec is None:
            self._maybe_reload()
            with self._cache_lock:
                rec = self._access.get(token)
        if rec is None:
            return None
        expires_at = rec.get("expires_at")
        if expires_at is not None and expires_at < time.time():
            self.revoke(token)
            return None
        return dict(rec)

    def get_refresh(self, token: str) -> dict[str, Any] | None:
        with self._cache_lock:
            rec = self._refresh.get(token)
        if rec is None:
            self._maybe_reload()
            with self._cache_lock:
                rec = self._refresh.get(token)
        return dict(rec) if rec is not None else None

    # -- writes --

    def mint_pair(self, client_id: str, scopes: list[str]) -> tuple[str, str, int]:
        """Mint a fresh opaque access+refresh pair. Returns
        ``(access_token, refresh_token, expires_at)``."""
        access = secrets.token_urlsafe(_TOKEN_BYTES)
        refresh = secrets.token_urlsafe(_TOKEN_BYTES)
        expires_at = int(time.time()) + self._access_ttl
        with _file_lock(self._path):
            data = _load_file(self._path)
            _prune_expired(data, time.time())
            data["access"][access] = {
                "client_id": client_id,
                "scopes": list(scopes),
                "expires_at": expires_at,
                "refresh": refresh,
            }
            data["refresh"][refresh] = {
                "client_id": client_id,
                "scopes": list(scopes),
                "access": access,
            }
            _save_file(self._path, data)
            self._set_cache(data)
        return access, refresh, expires_at

    def rotate(self, old_refresh: str) -> tuple[str, str, int, str, list[str]] | None:
        """Consume ``old_refresh`` and its access partner, mint a new pair with
        the same client_id/scopes. Returns
        ``(access, refresh, expires_at, client_id, scopes)`` or ``None`` if the
        refresh token is unknown (already rotated away / revoked)."""
        access = secrets.token_urlsafe(_TOKEN_BYTES)
        refresh = secrets.token_urlsafe(_TOKEN_BYTES)
        expires_at = int(time.time()) + self._access_ttl
        with _file_lock(self._path):
            data = _load_file(self._path)
            old = data["refresh"].get(old_refresh)
            if old is None:
                self._set_cache(data)
                return None
            client_id = str(old.get("client_id", ""))
            scopes = [str(s) for s in old.get("scopes", [])]
            # Invalidate the old pair (rotation).
            data["refresh"].pop(old_refresh, None)
            old_access = old.get("access")
            if old_access:
                data["access"].pop(old_access, None)
            _prune_expired(data, time.time())
            data["access"][access] = {
                "client_id": client_id,
                "scopes": scopes,
                "expires_at": expires_at,
                "refresh": refresh,
            }
            data["refresh"][refresh] = {
                "client_id": client_id,
                "scopes": scopes,
                "access": access,
            }
            _save_file(self._path, data)
            self._set_cache(data)
        return access, refresh, expires_at, client_id, scopes

    def revoke(self, token: str) -> None:
        """Delete ``token`` (whether access or refresh) and its partner."""
        with _file_lock(self._path):
            data = _load_file(self._path)
            changed = False
            access_rec = data["access"].pop(token, None)
            if access_rec is not None:
                changed = True
                partner = access_rec.get("refresh")
                if partner:
                    data["refresh"].pop(partner, None)
            refresh_rec = data["refresh"].pop(token, None)
            if refresh_rec is not None:
                changed = True
                partner = refresh_rec.get("access")
                if partner:
                    data["access"].pop(partner, None)
            if changed:
                _save_file(self._path, data)
            self._set_cache(data)
