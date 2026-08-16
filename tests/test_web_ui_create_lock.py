"""Regression test for issue #42.

api_create acquired the env lock inside the try whose finally always released
it, so a BusyError (the lock is held by another in-flight request) caused the
failing request to release a lock it never owned. The fix acquires the lock in
its own try and only releases it in a finally that runs solely when this request
acquired it.
"""

from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.errors import BusyError
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _open_settings(tmp_path):
    # No ui_password -> the UI mounts without auth, so the POST reaches api_create.
    return Settings(
        routing_mode="port",
        base_data_dir=str(tmp_path),
        teams={"1": TeamSettings(team_id="1")},
    )


def test_api_create_busy_keeps_foreign_lock(tmp_path):
    settings = _open_settings(tmp_path)
    locks = LockManager()
    # Request A holds the env lock for "main".
    locks.acquire_env("main")

    app = Starlette()
    mount_web_ui(app, lambda: settings, locks)
    client = TestClient(app)

    with patch("oduflow.web_ui.env_ops.create_environment") as create:
        resp = client.post(
            "/api/environments/create",
            json={"env_name": "main", "repo_url": "r", "odoo_image": "i"},
        )
        # The busy request must never start real provisioning.
        create.assert_not_called()

    assert resp.json()["ok"] is False  # reported busy

    # The lock held by request A must still be held: the failed request must not
    # have released it. A fresh acquire therefore still reports busy.
    with pytest.raises(BusyError):
        locks.acquire_env("main")


def test_api_create_invalid_json_no_lock_error(tmp_path):
    # A malformed body must not raise on the finally (env_name was unbound before
    # the fix) — it should return a clean 400.
    settings = _open_settings(tmp_path)
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    client = TestClient(app)

    resp = client.post(
        "/api/environments/create",
        content=b"not json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json()["ok"] is False


def test_api_create_passes_short_hostname(tmp_path):
    settings = _open_settings(tmp_path)
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    client = TestClient(app)

    with patch(
        "oduflow.web_ui.env_ops.create_environment",
        return_value={"url": "https://dev2.example.com"},
    ) as create:
        resp = client.post(
            "/api/environments/create",
            json={
                "env_name": "main",
                "hostname": "dev2",
                "repo_url": "r",
                "odoo_image": "i",
            },
        )

    assert resp.json()["ok"] is True
    assert create.call_args.kwargs["hostname"] == "dev2"
