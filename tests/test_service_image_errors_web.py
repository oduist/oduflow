"""Regression tests for safe auxiliary-service image pull errors."""

from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.errors import NotFoundError, PrerequisiteNotMetError
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui

IMAGE = "example/missing:0.7.2"
NOT_FOUND_MESSAGE = (
    f"Docker image '{IMAGE}' was not found or is not accessible. "
    "Check the image name, tag, and registry permissions."
)


def _client(tmp_path) -> TestClient:
    settings = Settings(
        routing_mode="port",
        base_data_dir=str(tmp_path),
        teams={"1": TeamSettings(team_id="1")},
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    "endpoint",
    ["/api/services/create", "/api/service-presets/restore"],
)
def test_missing_image_returns_safe_404_for_create_and_restore(tmp_path, endpoint):
    client = _client(tmp_path)
    internal_detail = "http+docker://localhost/v1.53/images/create?secret=token"

    with patch(
        "oduflow.web_ui.service_ops.create_service",
        side_effect=NotFoundError(NOT_FOUND_MESSAGE),
    ):
        response = client.post(
            endpoint,
            json={"name": "missing", "image": IMAGE, "port": 8080},
        )

    assert response.status_code == 404
    assert response.json() == {"ok": False, "error": NOT_FOUND_MESSAGE}
    assert internal_detail not in response.text
    assert "http+docker" not in response.text


def test_registry_failure_returns_safe_400(tmp_path):
    client = _client(tmp_path)
    message = (
        f"Could not pull Docker image '{IMAGE}'. Check Docker connectivity, "
        "registry availability, and registry credentials."
    )

    with patch(
        "oduflow.web_ui.service_ops.create_service",
        side_effect=PrerequisiteNotMetError(message),
    ):
        response = client.post(
            "/api/services/create",
            json={"name": "missing", "image": IMAGE, "port": 8080},
        )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": message}


def test_unexpected_service_error_stays_masked(tmp_path):
    client = _client(tmp_path)
    internal_detail = "registry failure with secret credentials"

    with patch(
        "oduflow.web_ui.service_ops.create_service",
        side_effect=RuntimeError(internal_detail),
    ):
        response = client.post(
            "/api/services/create",
            json={"name": "missing", "image": IMAGE, "port": 8080},
        )

    assert response.status_code == 500
    assert response.json() == {"ok": False, "error": "Internal server error."}
    assert internal_detail not in response.text
