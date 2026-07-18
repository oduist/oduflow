"""Config hot-reload: classifier, in-place reload, OAuth client refresh, CLI."""

from __future__ import annotations

import asyncio
from unittest import mock

import pytest

import oduflow.server as server
from oduflow.oauth_provider import OduflowOAuthProvider
from oduflow.settings import Settings, TeamSettings, classify_settings_change


def _team(tid: str, token: str, lo: int, hi: int, **over) -> TeamSettings:
    return TeamSettings(
        team_id=tid,
        auth_token=token,
        hostname=f"{tid}.example.com",
        port_range_start=lo,
        port_range_end=hi,
        **over,
    )


def _mk(teams=None, **over) -> Settings:
    if teams is None:
        teams = {"1": _team("1", "tok-a", 50000, 50100)}
    # Lifecycle off by default so _do_reload never spawns a real reaper thread.
    over.setdefault("auto_stop_hours", 0)
    over.setdefault("auto_delete_hours", 0)
    return Settings(teams=teams, **over)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# --- classify_settings_change ------------------------------------------------


class TestClassifier:
    def test_restart_required_fields(self):
        old = _mk()
        new = _mk(host="127.0.0.1", port=9000, routing_mode="port")
        # host + port differ (routing_mode unchanged).
        delta = classify_settings_change(old, new)
        joined = " ".join(delta.restart_required)
        assert "[server] host" in joined
        assert "[server] port" in joined
        assert not delta.hot
        assert delta.changed

    def test_db_and_data_dir_are_restart_required(self):
        old = _mk()
        new = _mk(db_password="new-secret", base_data_dir="/other")
        delta = classify_settings_change(old, new)
        joined = " ".join(delta.restart_required)
        assert "[database] password" in joined
        assert "[storage] data_dir" in joined

    def test_hot_fields(self):
        old = _mk()
        new = _mk(auto_stop_hours=12, overlay_threshold_mb=99)
        delta = classify_settings_change(old, new)
        joined = " ".join(delta.hot)
        assert "auto_stop_hours" in joined
        assert "overlay_threshold_mb" in joined
        assert not delta.restart_required

    def test_team_add_remove_change_are_hot(self):
        one = {"1": _team("1", "tok-a", 50000, 50100)}
        two = {
            "1": _team("1", "tok-a", 50000, 50100),
            "2": _team("2", "tok-b", 50100, 50200),
        }
        added = classify_settings_change(_mk(teams=one), _mk(teams=two))
        assert added.hot == ["team 2 added"]
        assert not added.removed_teams

        removed = classify_settings_change(_mk(teams=two), _mk(teams=one))
        assert removed.removed_teams == ["2"]

        changed_one = {"1": _team("1", "tok-a", 50000, 50100, db_quota_gb=10)}
        changed = classify_settings_change(_mk(teams=one), _mk(teams=changed_one))
        assert changed.hot == ["team 1 settings changed"]

    def test_no_change(self):
        assert not classify_settings_change(_mk(), _mk()).changed


# --- _do_reload --------------------------------------------------------------


@pytest.fixture
def reload_env(monkeypatch):
    """Patch out reconcile + reaper so _do_reload does no Docker/thread work."""
    recon = mock.Mock()
    start_reaper = mock.Mock(return_value="reaper-thread")
    monkeypatch.setattr(server, "_reconcile", recon)
    monkeypatch.setattr(server.reaper, "start_reaper", start_reaper)
    monkeypatch.setattr(server, "find_toml", lambda: "/x/oduflow.toml")
    monkeypatch.setattr(server, "_reaper_thread", object())  # already running
    monkeypatch.setattr(server, "_auth_provider", None)
    return {"recon": recon, "start_reaper": start_reaper}


def _patch_from_toml(monkeypatch, result):
    def _from_toml(_path):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(server.Settings, "from_toml", staticmethod(_from_toml))


def test_reload_valid_swaps_and_reconciles(monkeypatch, reload_env):
    old = _mk()
    new = _mk(
        teams={
            "1": _team("1", "tok-a", 50000, 50100),
            "2": _team("2", "tok-b", 50100, 50200),
        }
    )
    monkeypatch.setattr(server, "_settings", old)
    _patch_from_toml(monkeypatch, new)

    server._do_reload()

    assert server._settings is new
    reload_env["recon"].assert_called_once_with(new)


def test_reload_invalid_keeps_previous(monkeypatch, reload_env):
    old = _mk()
    monkeypatch.setattr(server, "_settings", old)
    _patch_from_toml(monkeypatch, ValueError("broken toml"))

    server._do_reload()

    assert server._settings is old
    reload_env["recon"].assert_not_called()


def test_reload_rejects_config_failing_validate(monkeypatch, reload_env):
    old = _mk()
    monkeypatch.setattr(server, "_settings", old)
    # from_toml succeeds but the object fails validate() (no teams).
    _patch_from_toml(monkeypatch, Settings(teams={}))

    server._do_reload()

    assert server._settings is old
    reload_env["recon"].assert_not_called()


def test_reload_removed_team_does_not_destroy(monkeypatch, reload_env):
    old = _mk(
        teams={
            "1": _team("1", "tok-a", 50000, 50100),
            "2": _team("2", "tok-b", 50100, 50200),
        }
    )
    new = _mk()  # only team 1
    monkeypatch.setattr(server, "_settings", old)
    _patch_from_toml(monkeypatch, new)

    server._do_reload()

    # Swapped + reconciled (create-only); no delete path is ever invoked.
    assert server._settings is new
    reload_env["recon"].assert_called_once_with(new)


def test_reload_starts_reaper_when_lifecycle_enabled(monkeypatch, reload_env):
    old = _mk()
    new = _mk(auto_stop_hours=24)
    monkeypatch.setattr(server, "_settings", old)
    monkeypatch.setattr(server, "_reaper_thread", None)  # not running
    _patch_from_toml(monkeypatch, new)

    server._do_reload()

    reload_env["start_reaper"].assert_called_once()
    assert server._reaper_thread == "reaper-thread"


# --- OAuth client refresh (hot team add/remove in HTTP) ----------------------


class TestOAuthRefresh:
    def _oauth(self, teams):
        return _mk(teams=teams, oauth_base_url="https://oduflow.example.com")

    def test_refresh_adds_and_removes_team_clients(self):
        two = {
            "1": _team("1", "tok-a", 50000, 50100),
            "2": _team("2", "tok-b", 50100, 50200),
        }
        state = {"s": self._oauth(two)}
        provider = OduflowOAuthProvider(lambda: state["s"])

        # Clients are keyed by the public client_id (team_<id>); the secret
        # auth_token is preseeded as a Bearer access token.
        assert _run(provider.get_client("team_1")) is not None
        assert _run(provider.get_client("team_3")) is None
        assert _run(provider.load_access_token("tok-a")) is not None

        # Add team 3 → its client and token authenticate after refresh.
        three = dict(two, **{"3": _team("3", "tok-c", 50200, 50300)})
        state["s"] = self._oauth(three)
        provider.refresh_clients()
        assert _run(provider.get_client("team_3")) is not None
        assert _run(provider.load_access_token("tok-c")) is not None

        # Remove team 2 → its client and token are dropped after refresh.
        state["s"] = self._oauth(
            {
                "1": _team("1", "tok-a", 50000, 50100),
                "3": _team("3", "tok-c", 50200, 50300),
            }
        )
        provider.refresh_clients()
        assert _run(provider.get_client("team_2")) is None
        assert _run(provider.load_access_token("tok-b")) is None
        assert _run(provider.get_client("team_1")) is not None
        assert _run(provider.load_access_token("tok-a")) is not None

    def test_refresh_invalidates_rotated_token(self):
        state = {"s": self._oauth({"1": _team("1", "tok-old", 50000, 50100)})}
        provider = OduflowOAuthProvider(lambda: state["s"])
        assert _run(provider.load_access_token("tok-old")) is not None

        state["s"] = self._oauth({"1": _team("1", "tok-new", 50000, 50100)})
        provider.refresh_clients()
        assert _run(provider.load_access_token("tok-old")) is None
        assert _run(provider.load_access_token("tok-new")) is not None


# --- CLI `oduflow reload` ----------------------------------------------------


def test_cli_reload_check_invalid_exits_nonzero(monkeypatch):
    monkeypatch.setattr(server, "find_toml", lambda: "/x/oduflow.toml")
    _patch_from_toml(monkeypatch, ValueError("bad"))
    with pytest.raises(SystemExit) as exc:
        server._run_config_reload(check=True)
    assert exc.value.code == 1


def test_cli_reload_check_ok(monkeypatch, capsys):
    monkeypatch.setattr(server, "find_toml", lambda: "/x/oduflow.toml")
    _patch_from_toml(monkeypatch, _mk())
    server._run_config_reload(check=True)
    assert "OK" in capsys.readouterr().out


def test_cli_reload_no_pid_exits_nonzero(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "find_toml", lambda: "/x/oduflow.toml")
    _patch_from_toml(monkeypatch, _mk(base_data_dir=str(tmp_path)))
    with pytest.raises(SystemExit) as exc:
        server._run_config_reload(check=False)
    assert exc.value.code == 1


def test_cli_reload_sends_sighup(monkeypatch, tmp_path):
    (tmp_path / "oduflow.pid").write_text("4242\n")
    monkeypatch.setattr(server, "find_toml", lambda: "/x/oduflow.toml")
    _patch_from_toml(monkeypatch, _mk(base_data_dir=str(tmp_path)))
    sent = {}
    monkeypatch.setattr(
        server.os, "kill", lambda pid, sig: sent.update(pid=pid, sig=sig)
    )
    server._run_config_reload(check=False)
    assert sent["pid"] == 4242
    assert sent["sig"] == server.signal.SIGHUP


# --- PID file safety ---------------------------------------------------------


def test_remove_pid_file_only_deletes_own_pid(tmp_path):
    # A PID file owned by another process (e.g. a long-running HTTP server) must
    # not be deleted by a transient process's atexit hook.
    pid_path = tmp_path / "oduflow.pid"
    pid_path.write_text("999999\n")  # someone else's PID
    server._remove_pid_file(str(pid_path))
    assert pid_path.exists()

    pid_path.write_text(f"{server.os.getpid()}\n")  # our own PID
    server._remove_pid_file(str(pid_path))
    assert not pid_path.exists()


# --- systemd unit ------------------------------------------------------------


def test_systemd_unit_supports_reload():
    from oduflow.systemd import UNIT_TEMPLATE

    assert "ExecReload=/bin/kill -HUP $MAINPID" in UNIT_TEMPLATE
