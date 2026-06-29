"""Automatic lifecycle management for idle environments.

A background daemon thread sweeps every team on an interval:

- a running environment with no recorded work for ``auto_stop_hours``
  (any env-scoped MCP tool call or dashboard lifecycle action counts as
  work; listing does not) is stopped;
- a stopped environment that nobody started for ``auto_delete_hours``
  after it stopped is deleted.

Protected environments are exempt from both. Environments busy with another
operation (per-environment lock held) are skipped until the next sweep, so
the reaper never races agents. Both actions are idempotent, so a second
oduflow process sweeping the same data dir is harmless.

Disable either behavior with ``0`` in ``oduflow.toml``::

    [lifecycle]
    auto_stop_hours = 48
    auto_delete_hours = 72
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from oduflow import activity
from oduflow.docker_ops import env_ops
from oduflow.errors import BusyError, FlowError
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")

SWEEP_INTERVAL_SECONDS = 300


def _is_running(env: dict[str, Any]) -> bool:
    return any(c.get("status") == "running" for c in env.get("containers", []))


def _auto_stop(
    settings: Settings, team: TeamSettings, locks: LockManager, env_name: str
) -> None:
    try:
        locks.acquire_env(env_name)
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
        locks.acquire_env(env_name)
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
