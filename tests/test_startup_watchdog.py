"""The startup watchdog must fire on silence and stay quiet on progress.

Startup runs before the HTTP listener binds, so a Docker call that never returns
leaves a live process serving nothing — the failure mode this watchdog exists to
convert into a restart.
"""

from __future__ import annotations

import logging

from oduflow.startup_watchdog import StartupWatchdog, guard_startup


def _watchdog(stall_seconds: float = 0.0):
    fired: list[float] = []
    watchdog = StartupWatchdog(
        stall_seconds=stall_seconds, on_stall=lambda silence: fired.append(silence)
    )
    return watchdog, fired


def test_fires_when_startup_goes_silent():
    watchdog, fired = _watchdog(stall_seconds=60.0)
    watchdog._last_beat -= 3600  # an hour without a single log record
    assert watchdog.check() is True
    assert fired and fired[0] > 60


def test_stays_quiet_while_startup_logs_progress():
    watchdog, fired = _watchdog(stall_seconds=60.0)
    watchdog.beat()
    assert watchdog.check() is False
    assert not fired


def test_log_records_are_heartbeats():
    # Any record on the oduflow logger counts as progress, so a slow but
    # advancing start (image pulls, template restore) is never killed.
    watchdog, fired = _watchdog(stall_seconds=60.0)
    logger = logging.getLogger("oduflow")
    previous = logger.level
    logger.setLevel(logging.INFO)  # the level the server runs at
    watchdog.start()
    try:
        watchdog._last_beat -= 3600  # pretend we have been silent for an hour
        assert watchdog.silence() > 60
        logger.info("still working")
        assert watchdog.silence() < 1
    finally:
        watchdog.stop()
        logger.setLevel(previous)
    assert not fired


def test_env_var_widens_the_window_and_can_disable_the_watchdog(monkeypatch):
    monkeypatch.setenv("ODUFLOW_STARTUP_STALL_SECONDS", "3600")
    with guard_startup(stall_seconds=1.0, on_stall=lambda silence: None) as watchdog:
        assert watchdog.stall_seconds == 3600.0

    monkeypatch.setenv("ODUFLOW_STARTUP_STALL_SECONDS", "0")
    fired: list[float] = []
    with guard_startup(stall_seconds=0.0, on_stall=fired.append) as watchdog:
        assert watchdog.check() is False
    assert not fired


def test_guard_removes_its_handler_when_startup_completes():
    logger = logging.getLogger("oduflow")
    before = list(logger.handlers)
    with guard_startup(stall_seconds=60.0, on_stall=lambda silence: None):
        assert len(logger.handlers) == len(before) + 1
    assert logger.handlers == before
