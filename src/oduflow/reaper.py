"""Automatic lifecycle management for idle environments.

A background daemon thread sweeps every team on an interval:

- a running environment with no recorded work for ``auto_stop_hours``
  (any env-scoped MCP tool call or dashboard lifecycle action counts as
  work; listing does not) is stopped;
- a stopped environment that nobody started for ``auto_delete_hours``
  after it stopped is deleted.

Protected environments are exempt from both. Actions are submitted through
the same named-resource operation queue as MCP and dashboard mutations, so
the reaper never races agent work. Eligibility is checked again when the
operation actually starts.

Disable either behavior with ``0`` in ``oduflow.toml``::

    [lifecycle]
    auto_stop_hours = 48
    auto_delete_hours = 0
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from oduflow import activity
from oduflow.docker_ops import env_ops
from oduflow.errors import BusyError, FlowError
from oduflow.locking import LockManager
from oduflow.operations import (
    get_operation_manager,
    register_operation,
    static_resource,
)
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")

SWEEP_INTERVAL_SECONDS = 300


def _auto_stop_operation(env_name: str) -> None:
    """Re-check eligibility at execution time, then stop the environment."""
    from oduflow.server import _get_settings, _resolve_team

    settings = _get_settings()
    team = _resolve_team(None)
    env = next(
        (
            item
            for item in env_ops.list_environments(settings, team)
            if item["env_name"] == env_name
        ),
        None,
    )
    if env is None or env.get("protected") or not _is_running(env):
        return
    record = activity.get_all(team).get(env_name, {})
    last = activity.parse_ts(record.get("last_activity"))
    if (
        last is None
        or settings.auto_stop_hours <= 0
        or time.time() - last <= settings.auto_stop_hours * 3600
    ):
        return
    env_ops.stop_environment(settings, team, env_name)
    activity.mark_stopped(team, env_name, by="auto")
    logger.info(
        "Auto-stopped environment '%s' (idle longer than %dh)",
        env_name,
        settings.auto_stop_hours,
    )


def _auto_delete_operation(env_name: str) -> None:
    """Re-check stopped age/protection immediately before destructive cleanup."""
    from oduflow.server import _get_settings, _resolve_team

    settings = _get_settings()
    team = _resolve_team(None)
    env = next(
        (
            item
            for item in env_ops.list_environments(settings, team)
            if item["env_name"] == env_name
        ),
        None,
    )
    if env is None or env.get("protected") or _is_running(env):
        return
    record = activity.get_all(team).get(env_name, {})
    stopped = activity.parse_ts(record.get("stopped_at"))
    if (
        stopped is None
        or settings.auto_delete_hours <= 0
        or time.time() - stopped <= settings.auto_delete_hours * 3600
    ):
        return
    env_ops.delete_environment(settings, team, env_name)
    logger.info(
        "Auto-deleted environment '%s' (stopped longer than %dh)",
        env_name,
        settings.auto_delete_hours,
    )


register_operation(
    "lifecycle.auto_stop",
    _auto_stop_operation,
    static_resource("env", "env_name"),
)
register_operation(
    "lifecycle.auto_delete",
    _auto_delete_operation,
    static_resource("env", "env_name"),
)


def _is_running(env: dict[str, Any]) -> bool:
    return any(c.get("status") == "running" for c in env.get("containers", []))


def _auto_stop(
    settings: Settings, team: TeamSettings, locks: LockManager, env_name: str
) -> None:
    try:
        locks.acquire_env(env_name, team.team_id, operation="auto-stop")
    except BusyError:
        return  # an operation is in flight; it counts as activity anyway
    try:
        env_ops.stop_environment(settings, team, env_name)
        activity.mark_stopped(team, env_name, by="auto")
        logger.info(
            "Auto-stopped environment '%s' (idle longer than %dh)",
            env_name,
            settings.auto_stop_hours,
        )
    except FlowError as e:
        logger.debug("Auto-stop of '%s' skipped: %s", env_name, e)
    except Exception:
        logger.warning("Auto-stop of '%s' failed", env_name, exc_info=True)
    finally:
        locks.release_env(env_name)


def _auto_delete(
    settings: Settings, team: TeamSettings, locks: LockManager, env_name: str
) -> None:
    try:
        locks.acquire_env(env_name, team.team_id, operation="auto-delete")
    except BusyError:
        return
    try:
        env_ops.delete_environment(settings, team, env_name)
        logger.info(
            "Auto-deleted environment '%s' (stopped longer than %dh)",
            env_name,
            settings.auto_delete_hours,
        )
    except FlowError as e:
        logger.debug("Auto-delete of '%s' skipped: %s", env_name, e)
    except Exception:
        logger.warning("Auto-delete of '%s' failed", env_name, exc_info=True)
    finally:
        locks.release_env(env_name)


def sweep(settings: Settings, locks: LockManager) -> None:
    """One pass over all teams. Safe to call concurrently with tool traffic."""
    now = time.time()
    stop_after = settings.auto_stop_hours * 3600
    delete_after = settings.auto_delete_hours * 3600
    manager = get_operation_manager(settings)
    queue_ready = manager.started

    for team in settings.teams.values():
        try:
            envs = env_ops.list_environments(settings, team)
        except Exception:
            logger.debug(
                "Reaper could not list environments for team %s",
                team.team_id,
                exc_info=True,
            )
            continue

        activity.prune(team, {e["env_name"] for e in envs})
        records = activity.get_all(team)

        for env in envs:
            env_name = env["env_name"]
            rec = records.get(env_name, {})

            if _is_running(env):
                if rec.get("stopped_at"):
                    # Started outside our hooks (e.g. plain `docker start`).
                    activity.mark_started(team, env_name)
                    continue
                last = activity.parse_ts(rec.get("last_activity"))
                if last is None:
                    # First sighting: seed the clock instead of guessing.
                    activity.touch(team, env_name)
                    continue
                if (
                    stop_after > 0
                    and not env.get("protected")
                    and now - last > stop_after
                ):
                    if queue_ready:
                        manager.submit(
                            "lifecycle.auto_stop",
                            team.team_id,
                            {"env_name": env_name},
                            [f"env:{team.team_id}:{env_name}"],
                            wait=False,
                            coalesce_key=f"auto-stop:{env_name}",
                        )
                    else:
                        _auto_stop(settings, team, locks, env_name)
            else:
                stopped = activity.parse_ts(rec.get("stopped_at"))
                if stopped is None:
                    # Stopped before we could record it (manual docker stop,
                    # pre-upgrade environments): the delete clock starts now.
                    activity.mark_stopped(team, env_name, by="observed")
                    continue
                if (
                    delete_after > 0
                    and not env.get("protected")
                    and now - stopped > delete_after
                ):
                    if queue_ready:
                        manager.submit(
                            "lifecycle.auto_delete",
                            team.team_id,
                            {"env_name": env_name},
                            [f"env:{team.team_id}:{env_name}"],
                            wait=False,
                            coalesce_key=f"auto-delete:{env_name}",
                        )
                    else:
                        _auto_delete(settings, team, locks, env_name)


def start_reaper(
    get_settings: Callable[[], Settings],
    locks: LockManager,
    interval: float = SWEEP_INTERVAL_SECONDS,
) -> threading.Thread | None:
    """Start the background sweep thread. Returns None when both behaviors
    are disabled in the config at startup."""
    settings = get_settings()
    if settings.auto_stop_hours <= 0 and settings.auto_delete_hours <= 0:
        logger.info("Environment auto-stop/auto-delete disabled by config")
        return None

    def loop() -> None:
        while True:
            time.sleep(interval)
            try:
                sweep(get_settings(), locks)
            except Exception:
                logger.warning("Reaper sweep failed", exc_info=True)

    thread = threading.Thread(target=loop, name="oduflow-reaper", daemon=True)
    thread.start()
    logger.info(
        "Environment reaper started (auto-stop after %dh idle, "
        "auto-delete after %dh stopped, sweep every %ds)",
        settings.auto_stop_hours,
        settings.auto_delete_hours,
        int(interval),
    )
    if settings.auto_delete_hours > 0:
        logger.warning(
            "auto_delete_hours=%d is ENABLED: unprotected environments stopped "
            "for longer than %dh will be PERMANENTLY DELETED (database and "
            "workspace). Set [lifecycle] auto_delete_hours = 0 to disable.",
            settings.auto_delete_hours,
            settings.auto_delete_hours,
        )
    return thread
