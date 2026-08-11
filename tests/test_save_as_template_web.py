"""End-to-end web-route tests for POST /api/environments/{branch}/save-as-template.

Drives the dashboard "New Template" action through the Starlette app: a valid
name reaches system_ops.publish_env_as_template WITHOUT overwrite (so the UI can
only create fresh templates), an empty name is rejected before any Docker call,
and a duplicate name surfaces the backend ConflictError as a failed response.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock, patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.errors import BusyError, ConflictError
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _client_with_locks(tmp_path):
    team = TeamSettings(team_id="1", hostname="example.com", data_dir=str(tmp_path))
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    locks = LockManager()
    app = Starlette()
    mount_web_ui(app, lambda: settings, locks)
    return TestClient(app), locks


def _client(tmp_path):
    return _client_with_locks(tmp_path)[0]


def test_save_as_template_creates_new(tmp_path):
    client = _client(tmp_path)
    result = {
        "status": "promoted",
        "env_name": "feature-x",
        "template_db": "oduflow_1_t_prod",
        "dump": "/srv/oduflow/templates/prod/dump.pgdump",
        "filestore": "/srv/oduflow/templates/prod/filestore",
        "affected_envs": [],
        "remount_failures": [],
    }
    with patch(
        "oduflow.web_ui.system_ops.publish_env_as_template", return_value=result
    ) as publish:
        response = client.post(
            "/api/environments/feature-x/save-as-template",
            json={"template_name": "prod"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert "dump" not in body["result"]
    assert "filestore" not in body["result"]
    assert body["result"]["template_db"] == "oduflow_1_t_prod"
    assert publish.call_args.args[2] == "feature-x"
    assert publish.call_args.kwargs["template_name"] == "prod"
    # The UI never overwrites: no overwrite flag is passed (defaults to False).
    assert publish.call_args.kwargs.get("overwrite") in (None, False)


def test_save_as_template_does_not_block_dashboard(tmp_path):
    client = _client(tmp_path)
    operation_started = Event()
    release_operation = Event()

    def publish(*args, **kwargs):
        operation_started.set()
        assert release_operation.wait(timeout=5)
        return {
            "status": "promoted",
            "env_name": "feature-x",
            "template_db": "oduflow_1_t_prod",
        }

    with (
        client,
        patch(
            "oduflow.web_ui.system_ops.publish_env_as_template",
            side_effect=publish,
        ),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        save = executor.submit(
            client.post,
            "/api/environments/feature-x/save-as-template",
            json={"template_name": "prod"},
        )
        try:
            assert operation_started.wait(timeout=2)
            dashboard = executor.submit(client.get, "/").result(timeout=2)
            assert dashboard.status_code == 200
        finally:
            release_operation.set()

        assert save.result(timeout=2).status_code == 200


def test_running_server_returns_durable_operation_ticket(tmp_path):
    client = _client(tmp_path)
    manager = Mock(started=True)
    manager.submit.return_value = {
        "operation_id": "server-operation-id",
        "state": "queued",
        "resources": ["env:1:feature-x", "template:1:prod"],
    }

    with patch(
        "oduflow.web_ui.get_operation_manager",
        return_value=manager,
    ):
        response = client.post(
            "/api/environments/feature-x/save-as-template",
            json={"template_name": "prod"},
        )

    assert response.status_code == 202
    assert response.json()["operation_id"] == "server-operation-id"
    manager.submit.assert_called_once()
    assert manager.submit.call_args.args[3] == [
        "env:1:feature-x",
        "template:1:prod",
    ]


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


def test_save_as_template_validates_name_before_team_lock(tmp_path):
    client, locks = _client_with_locks(tmp_path)
    locks.acquire_team("1")
    try:
        with patch("oduflow.web_ui.system_ops.publish_env_as_template") as publish:
            response = client.post(
                "/api/environments/feature-x/save-as-template",
                json={"template_name": "../etc"},
            )
    finally:
        locks.release_team("1")

    assert response.status_code == 400
    assert response.json()["ok"] is False
    publish.assert_not_called()


def test_save_as_template_busy_when_env_operation_in_flight(tmp_path):
    client, locks = _client_with_locks(tmp_path)
    locks.acquire_env("feature-x", "1")
    try:
        with patch("oduflow.web_ui.system_ops.publish_env_as_template") as publish:
            response = client.post(
                "/api/environments/feature-x/save-as-template",
                json={"template_name": "prod"},
            )
            publish.assert_not_called()

        assert response.json()["ok"] is False
        with pytest.raises(BusyError):
            locks.acquire_env("feature-x", "1")
    finally:
        locks.release_env("feature-x")


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
