"""Regression test for issue #84 (item 2).

The dashboard's HTTP-500 handlers returned ``str(e)`` in the JSON body, which
could leak absolute paths / Docker internals to the browser. The handlers must
now return a generic message and keep the detail server-side (logged only).

MCP tool responses are intentionally left verbose and are out of scope here.
"""

from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _open_settings(tmp_path):
    # No ui_password -> the UI mounts without auth, so the GET reaches api_list.
    return Settings(
        routing_mode="port",
        base_data_dir=str(tmp_path),
        teams={"1": TeamSettings(team_id="1")},
    )


def test_unexpected_500_does_not_leak_exception_text(tmp_path):
    settings = _open_settings(tmp_path)
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    # The handler catches the exception and returns 500 itself, so the test
    # client must not re-raise it.
    client = TestClient(app, raise_server_exceptions=False)

    secret = "/srv/oduflow/data/team-1/secret-internal-path"
    with patch(
        "oduflow.web_ui.env_ops.list_environments",
        side_effect=RuntimeError(secret),
    ):
        resp = client.get("/api/environments")

    assert resp.status_code == 500
    body = resp.json()
    assert body["ok"] is False
    # Generic message only — the internal detail must not reach the browser.
    assert body["error"] == "Internal server error."
    assert secret not in resp.text
