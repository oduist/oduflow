"""Tests for activity tracking and the environment reaper."""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from oduflow import activity, reaper
from oduflow.errors import ProtectedError
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings


def _team(tmp_path) -> TeamSettings:
    return TeamSettings(team_id="1", data_dir=str(tmp_path))


def _settings(tmp_path, stop_hours: int = 48, delete_hours: int = 72) -> Settings:
    return Settings(
        auto_stop_hours=stop_hours,
        auto_delete_hours=delete_hours,
        teams={"1": _team(tmp_path)},
    )


def _iso_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(
        timespec="seconds"
    )


def _write_activity(team: TeamSettings, records: dict) -> None:
    with open(activity.activity_path(team), "w") as f:
        json.dump(records, f)


def _env(name: str, running: bool = True, protected: bool = False) -> dict:
    return {
        "env_name": name,
        "protected": protected,
        "containers": [
            {"name": f"x-{name}", "status": "running" if running else "exited"}
        ],
    }


# --- activity module ------------------------------------------------------


def test_touch_and_get_all(tmp_path):
    team = _team(tmp_path)
    activity.touch(team, "env-a")
    rec = activity.get_all(team)["env-a"]
    assert activity.parse_ts(rec["last_activity"]) is not None
    assert "stopped_at" not in rec


def test_mark_stopped_keeps_first_timestamp_but_upgrades_attribution(tmp_path):
    team = _team(tmp_path)
    activity.mark_stopped(team, "env-a", by="observed")
    first = activity.get_all(team)["env-a"]["stopped_at"]
    activity.mark_stopped(team, "env-a", by="auto")
    rec = activity.get_all(team)["env-a"]
    assert rec["stopped_at"] == first
    assert rec["stopped_by"] == "auto"


def test_mark_started_clears_stopped_clock(tmp_path):
    team = _team(tmp_path)
    activity.mark_stopped(team, "env-a", by="auto")
    activity.mark_started(team, "env-a")
    rec = activity.get_all(team)["env-a"]
    assert "stopped_at" not in rec
    assert "stopped_by" not in rec
    assert rec["last_activity"]


def test_remove_and_prune(tmp_path):
    team = _team(tmp_path)
    activity.touch(team, "keep")
    activity.touch(team, "gone")
    activity.remove(team, "gone")
    assert "gone" not in activity.get_all(team)
    activity.touch(team, "stale")
    activity.prune(team, {"keep"})
    assert set(activity.get_all(team)) == {"keep"}


def test_concurrent_touch_no_corruption(tmp_path):
    team = _team(tmp_path)
    names = [f"env-{i}" for i in range(16)]
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda n: activity.touch(team, n), names))
    records = activity.get_all(team)
    assert set(records) == set(names)
    leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert leftovers == []


# --- reaper ---------------------------------------------------------------


def test_sweep_stops_idle_running_env(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    team = settings.teams["1"]
    _write_activity(team, {"idle": {"last_activity": _iso_ago(50)}})
    stopped: list[str] = []

    monkeypatch.setattr(
        reaper.env_ops, "list_environments", lambda s, t: [_env("idle")]
    )
    monkeypatch.setattr(
        reaper.env_ops,
        "stop_environment",
        lambda s, t, name: stopped.append(name),
    )
    monkeypatch.setattr(reaper.env_ops, "delete_environment", lambda s, t, name: None)

    reaper.sweep(settings, LockManager())
    assert stopped == ["idle"]
    rec = activity.get_all(team)["idle"]
    assert rec["stopped_by"] == "auto"
    assert rec["stopped_at"]


def test_sweep_spares_fresh_protected_and_busy(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    team = settings.teams["1"]
    _write_activity(
        team,
        {
            "fresh": {"last_activity": _iso_ago(1)},
            "shielded": {"last_activity": _iso_ago(100)},
            "busy": {"last_activity": _iso_ago(100)},
        },
    )
    stopped: list[str] = []
    monkeypatch.setattr(
        reaper.env_ops,
        "list_environments",
        lambda s, t: [
            _env("fresh"),
            _env("shielded", protected=True),
            _env("busy"),
        ],
    )
    monkeypatch.setattr(
        reaper.env_ops, "stop_environment", lambda s, t, name: stopped.append(name)
    )

    locks = LockManager()
    locks.acquire_env("busy")  # an operation is in flight
    try:
        reaper.sweep(settings, locks)
    finally:
        locks.release_env("busy")
    assert stopped == []


def test_sweep_seeds_clock_for_unknown_running_env(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    team = settings.teams["1"]
    stopped: list[str] = []
    monkeypatch.setattr(
        reaper.env_ops, "list_environments", lambda s, t: [_env("newcomer")]
    )
    monkeypatch.setattr(
        reaper.env_ops, "stop_environment", lambda s, t, name: stopped.append(name)
    )

    reaper.sweep(settings, LockManager())
    assert stopped == []
    assert activity.get_all(team)["newcomer"]["last_activity"]


def test_sweep_deletes_long_stopped_env(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    team = settings.teams["1"]
    _write_activity(
        team,
        {
            "old": {"last_activity": _iso_ago(200), "stopped_at": _iso_ago(80)},
            "recent": {"last_activity": _iso_ago(200), "stopped_at": _iso_ago(10)},
        },
    )
    deleted: list[str] = []
    monkeypatch.setattr(
        reaper.env_ops,
        "list_environments",
        lambda s, t: [_env("old", running=False), _env("recent", running=False)],
    )
    monkeypatch.setattr(
        reaper.env_ops,
        "delete_environment",
        lambda s, t, name: deleted.append(name),
    )

    reaper.sweep(settings, LockManager())
    assert deleted == ["old"]


def test_sweep_marks_observed_stop_before_deleting(tmp_path, monkeypatch):
    """A manually stopped env without a record gets its delete clock started
    on first sight, not deleted immediately."""
    settings = _settings(tmp_path)
    team = settings.teams["1"]
    deleted: list[str] = []
    monkeypatch.setattr(
        reaper.env_ops,
        "list_environments",
        lambda s, t: [_env("manual", running=False)],
    )
    monkeypatch.setattr(
        reaper.env_ops,
        "delete_environment",
        lambda s, t, name: deleted.append(name),
    )

    reaper.sweep(settings, LockManager())
    assert deleted == []
    rec = activity.get_all(team)["manual"]
    assert rec["stopped_by"] == "observed"
    assert rec["stopped_at"]


def test_sweep_delete_respects_protected_error(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    team = settings.teams["1"]
    _write_activity(
        team, {"keep": {"last_activity": _iso_ago(200), "stopped_at": _iso_ago(80)}}
    )

    def boom(s, t, name):
        raise ProtectedError("protected")

    monkeypatch.setattr(
        reaper.env_ops,
        "list_environments",
        lambda s, t: [_env("keep", running=False)],
    )
    monkeypatch.setattr(reaper.env_ops, "delete_environment", boom)

    reaper.sweep(settings, LockManager())  # must not raise
    assert "keep" in activity.get_all(team)


def test_sweep_disabled_by_zero_settings(tmp_path, monkeypatch):
    settings = _settings(tmp_path, stop_hours=0, delete_hours=0)
    team = settings.teams["1"]
    _write_activity(
        team,
        {
            "idle": {"last_activity": _iso_ago(500)},
            "old": {"last_activity": _iso_ago(500), "stopped_at": _iso_ago(400)},
        },
    )
    touched: list[str] = []
    monkeypatch.setattr(
        reaper.env_ops,
        "list_environments",
        lambda s, t: [_env("idle"), _env("old", running=False)],
    )
    monkeypatch.setattr(
        reaper.env_ops, "stop_environment", lambda s, t, n: touched.append(n)
    )
    monkeypatch.setattr(
        reaper.env_ops, "delete_environment", lambda s, t, n: touched.append(n)
    )

    reaper.sweep(settings, LockManager())
    assert touched == []


def test_sweep_clears_stopped_clock_when_started_externally(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    team = settings.teams["1"]
    _write_activity(
        team,
        {"woken": {"last_activity": _iso_ago(300), "stopped_at": _iso_ago(80)}},
    )
    monkeypatch.setattr(
        reaper.env_ops, "list_environments", lambda s, t: [_env("woken")]
    )
    monkeypatch.setattr(
        reaper.env_ops,
        "stop_environment",
        lambda s, t, n: (_ for _ in ()).throw(AssertionError("must not stop")),
    )

    reaper.sweep(settings, LockManager())
    rec = activity.get_all(team)["woken"]
    assert "stopped_at" not in rec


def test_start_reaper_disabled_returns_none(tmp_path):
    settings = _settings(tmp_path, stop_hours=0, delete_hours=0)
    assert reaper.start_reaper(lambda: settings, LockManager()) is None


# --- ensure_running (pull_and_apply wake-up) -------------------------------


def test_ensure_running_starts_stopped_env(monkeypatch, tmp_path):
    from oduflow.docker_ops import env_ops

    state = {"status": "exited"}

    class FakeContainer:
        labels = {"oduflow.team": "1"}

        @property
        def status(self):
            return state["status"]

    class FakeContainers:
        def get(self, name):
            return FakeContainer()

    class FakeClient:
        containers = FakeContainers()

    started: list[str] = []
    monkeypatch.setattr(env_ops, "get_client", lambda: FakeClient())
    monkeypatch.setattr(
        env_ops, "start_environment", lambda s, n, t=None: started.append(n)
    )

    settings = _settings(tmp_path)
    assert env_ops.ensure_running(settings, "env-x", settings.teams["1"]) is True
    assert started == ["env-x"]

    state["status"] = "running"
    assert env_ops.ensure_running(settings, "env-x", settings.teams["1"]) is False
    assert started == ["env-x"]


def test_ensure_running_wakes_a_stopped_env_once(monkeypatch, tmp_path):
    """The odoo_* tools wake environments without holding the env lock, so two
    calls can meet here — the wake must still start the container only once."""
    from oduflow.docker_ops import env_ops

    state = {"status": "exited"}

    class FakeContainer:
        labels = {"oduflow.team": "1"}

        @property
        def status(self):
            return state["status"]

    class FakeContainers:
        def get(self, name):
            return FakeContainer()

    class FakeClient:
        containers = FakeContainers()

    started: list[str] = []

    def fake_start(s, n, t=None):
        time.sleep(0.05)  # widen the check-then-start window
        started.append(n)
        state["status"] = "running"

    monkeypatch.setattr(env_ops, "get_client", lambda: FakeClient())
    monkeypatch.setattr(env_ops, "start_environment", fake_start)

    settings = _settings(tmp_path)
    team = settings.teams["1"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            f.result()
            for f in [
                pool.submit(env_ops.ensure_running, settings, "env-race", team)
                for _ in range(2)
            ]
        ]

    assert started == ["env-race"]
    assert sorted(results) == [False, True]


# --- _wake_for_work (tool-level wake-up) -----------------------------------


def test_wake_for_work_starts_and_notes(monkeypatch, tmp_path):
    from oduflow import server
    from oduflow.docker_ops import env_ops

    team = _team(tmp_path)
    settings = _settings(tmp_path)
    activity.mark_stopped(team, "env-x", by="auto")

    monkeypatch.setattr(env_ops, "ensure_running", lambda s, n, t=None: True)
    note = server._wake_for_work(settings, team, "env-x")
    assert note == "Note: environment was stopped; started it for this call.\n"
    rec = activity.get_all(team)["env-x"]
    assert "stopped_at" not in rec  # the stopped clock is cleared

    monkeypatch.setattr(env_ops, "ensure_running", lambda s, n, t=None: False)
    assert server._wake_for_work(settings, team, "env-x") == ""
