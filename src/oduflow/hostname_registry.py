from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import threading
from contextlib import contextmanager
from typing import Iterator

from oduflow.errors import ConflictError, FlowError

logger = logging.getLogger("oduflow")

_locks_guard = threading.Lock()
_path_locks: dict[str, threading.Lock] = {}


def _thread_lock(registry_path: str) -> threading.Lock:
    with _locks_guard:
        lock = _path_locks.get(registry_path)
        if lock is None:
            lock = threading.Lock()
            _path_locks[registry_path] = lock
        return lock


@contextmanager
def _registry_lock(registry_path: str) -> Iterator[None]:
    """Serialize hostname reservations across threads and server processes."""
    with _thread_lock(registry_path):
        os.makedirs(os.path.dirname(registry_path) or ".", exist_ok=True)
        fd = os.open(registry_path + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _load_registry(registry_path: str) -> dict[str, str]:
    if not os.path.isfile(registry_path):
        return {}
    try:
        with open(registry_path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("Ignoring malformed hostname registry %s", registry_path)
            return {}
        return {str(key): str(value) for key, value in data.items() if value}
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        logger.warning("Could not load hostname registry %s: %s", registry_path, exc)
        return {}


def _save_registry(registry_path: str, registry: dict[str, str]) -> None:
    directory = os.path.dirname(registry_path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix="hostnames.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(registry, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, registry_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def allocate_hostname(
    registry_path: str,
    env_name: str,
    slot_count: int,
    *,
    requested_hostname: str = "",
    hostname_prefix: str,
    active_envs: set[str] | None = None,
    used_hostnames: set[str] | None = None,
) -> str:
    """Reserve a stable short hostname for one environment.

    Automatic reservations append a number to ``hostname_prefix``. An explicitly
    requested hostname still consumes one of the team's configured environment
    slots, so the setting remains a hard cap on concurrent environments.
    """
    active_envs = active_envs or set()
    used_hostnames = used_hostnames or set()

    with _registry_lock(registry_path):
        registry = _load_registry(registry_path)
        current = registry.get(env_name, "")
        occupied_envs = (set(registry) | active_envs) - {env_name}
        if slot_count > 0 and len(occupied_envs) >= slot_count:
            raise FlowError(
                f"No free environment slots (configured: {slot_count}). "
                "Delete an unused environment to free a slot."
            )

        occupied_hostnames = {
            value for key, value in registry.items() if key != env_name
        } | used_hostnames

        if requested_hostname:
            if requested_hostname in occupied_hostnames:
                raise ConflictError(
                    f"Hostname '{requested_hostname}' is already used by another "
                    "environment."
                )
            if current != requested_hostname:
                registry[env_name] = requested_hostname
                _save_registry(registry_path, registry)
            return requested_hostname

        if current and current not in occupied_hostnames:
            return current
        if slot_count <= 0:
            raise FlowError(
                "Automatic hostname allocation requires environment_slots > 0."
            )

        for number in range(1, slot_count + 1):
            candidate = f"{hostname_prefix}{number}"
            if candidate not in occupied_hostnames:
                registry[env_name] = candidate
                _save_registry(registry_path, registry)
                logger.info(
                    "Allocated hostname %s for environment '%s'", candidate, env_name
                )
                return candidate

    raise FlowError(
        f"No free environment hostnames in {hostname_prefix}1-"
        f"{hostname_prefix}{slot_count}. "
        "Delete an unused environment to free a slot."
    )


def release_hostname(registry_path: str, env_name: str) -> None:
    with _registry_lock(registry_path):
        registry = _load_registry(registry_path)
        hostname = registry.pop(env_name, None)
        if hostname is not None:
            _save_registry(registry_path, registry)
            logger.info("Released hostname %s for environment '%s'", hostname, env_name)


def get_hostname(registry_path: str, env_name: str) -> str | None:
    return _load_registry(registry_path).get(env_name)
