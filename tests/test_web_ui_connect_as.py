"""REST endpoints behind the dashboard's Connect As button (issue #78).

api_connect_as mints a session and returns the cookie payload; api_env_users
feeds the user picker. Both delegate to odoo_ops and are exercised here with the
docker layer mocked, mirroring test_web_ui_create_lock's TestClient harness.
"""

from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.errors import NotFoundError
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _open_settings(tmp_path):
    # No ui_password -> the UI mounts without auth, so requests reach the API.
    return Settings(
        routing_mode="port",
        base_data_dir=str(tmp_path),
        teams={"1": TeamSettings(team_id="1")},
    )


def _client(settings):
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app)


_MINT = {
    "sid": "s" * 80,
    "login": "jane@acme.com",
    "uid": "7",
    "base_url": "http://localhost:50001",
    "cookie_domain": "localhost",
    "url": "http://localhost:50001/web",
    "expires_at": "2026-07-20T00:00:00Z",
}


def test_connect_as_returns_cookie(tmp_path):
    client = _client(_open_settings(tmp_path))
    with (
        patch(
            "oduflow.web_ui.odoo_ops.connect_as_user", return_value=dict(_MINT)
        ) as mock,
        patch("oduflow.web_ui.activity.touch"),
    ):
        resp = client.post(
            "/api/environments/18.0/connect-as", json={"user": "jane@acme.com"}
        )
    data = resp.json()
    assert data["ok"] is True
    result = data["result"]
    assert result["cookie"] == {
        "name": "session_id",
        "value": "s" * 80,
        "domain": "localhost",
        "path": "/",
    }
    assert result["url"] == "http://localhost:50001/web"
    assert result["login"] == "jane@acme.com"
    # (settings, team, branch, user) — branch from the URL, user from the body.
    assert mock.call_args.args[2] == "18.0"
    assert mock.call_args.args[3] == "jane@acme.com"


def test_connect_as_defaults_to_admin(tmp_path):
    client = _client(_open_settings(tmp_path))
    with (
        patch(
            "oduflow.web_ui.odoo_ops.connect_as_user", return_value=dict(_MINT)
        ) as mock,
        patch("oduflow.web_ui.activity.touch"),
    ):
        client.post("/api/environments/18.0/connect-as", json={})
    assert mock.call_args.args[3] == "admin"


def test_connect_as_error_surfaced(tmp_path):
    client = _client(_open_settings(tmp_path))
    with (
        patch(
            "oduflow.web_ui.odoo_ops.connect_as_user",
            side_effect=NotFoundError("User 'ghost' not found"),
        ),
        patch("oduflow.web_ui.activity.touch"),
    ):
        resp = client.post("/api/environments/18.0/connect-as", json={"user": "ghost"})
    data = resp.json()
    assert data["ok"] is False
    assert data.get("error")


def test_env_users_lists(tmp_path):
    client = _client(_open_settings(tmp_path))
    users = [
        {"login": "admin", "name": "Mitchell Admin", "share": False, "portal": False},
        {"login": "portal", "name": "Portal User", "share": True, "portal": True},
    ]
    with patch("oduflow.web_ui.odoo_ops.list_env_users", return_value=users):
        resp = client.get("/api/environments/18.0/users")
    data = resp.json()
    assert data["ok"] is True
    assert data["result"]["users"] == users
