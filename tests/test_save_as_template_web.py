"""End-to-end web-route tests for POST /api/environments/{branch}/save-as-template.

Drives the dashboard "New Template" action through the Starlette app: a valid
name reaches system_ops.publish_env_as_template WITHOUT overwrite (so the UI can
only create fresh templates), an empty name is rejected before any Docker call,
and a duplicate name surfaces the backend ConflictError as a failed response.
"""

from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.errors import ConflictError
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _client(tmp_path):
    team = TeamSettings(team_id="1", hostname="example.com", data_dir=str(tmp_path))
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app)


def test_save_as_template_creates_new(tmp_path):
    client = _client(tmp_path)
    result = {"env_name": "feature-x", "template_db": "oduflow_1_t_prod"}
    with patch(
        "oduflow.web_ui.system_ops.publish_env_as_template", return_value=result
    ) as publish:
        response = client.post(
            "/api/environments/feature-x/save-as-template",
            json={"template_name": "prod"},
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert publish.call_args.args[2] == "feature-x"
    assert publish.call_args.kwargs["template_name"] == "prod"
    # The UI never overwrites: no overwrite flag is passed (defaults to False).
    assert publish.call_args.kwargs.get("overwrite") in (None, False)


def test_save_as_template_requires_name(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.web_ui.system_ops.publish_env_as_template") as publish:
        response = client.post(
            "/api/environments/feature-x/save-as-template",
            json={"template_name": "   "},
        )

    assert response.status_code == 400
    assert response.json()["ok"] is False
    publish.assert_not_called()


def test_save_as_template_rejects_duplicate(tmp_path):
    client = _client(tmp_path)
    with patch(
        "oduflow.web_ui.system_ops.publish_env_as_template",
        side_effect=ConflictError("Template 'prod' already exists."),
    ):
        response = client.post(
            "/api/environments/feature-x/save-as-template",
            json={"template_name": "prod"},
        )

    body = response.json()
    assert body["ok"] is False
    assert "already exists" in body["error"]
