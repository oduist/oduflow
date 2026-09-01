"""Env-var handling for services in the dashboard REST API.

``api_service_env_vars`` prefills the update dialog with exactly the variables
an update would otherwise keep, and ``api_service_update`` treats an
``env_vars`` key present in the body as a full replacement — an empty mapping
clears every variable, an absent key keeps the current ones.
"""

from unittest.mock import MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import docker
from oduflow.errors import NotFoundError
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _client(tmp_path):
    team = TeamSettings(team_id="1", hostname="example.com", data_dir=str(tmp_path))
    settings = Settings(
        routing_mode="port",
        base_data_dir=str(tmp_path),
        teams={"1": team},
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app)


def _settings(tmp_path):
    team = TeamSettings(team_id="1", hostname="example.com", data_dir=str(tmp_path))
    return Settings(
        routing_mode="port", base_data_dir=str(tmp_path), teams={"1": team}
    ), team


def _container(env):
    container = MagicMock()
    container.labels = {"oduflow.service": "meili"}
    container.attrs = {"Config": {"Env": env}}
    return container


def test_env_vars_prefers_the_preset(tmp_path):
    """The preset is what an update reuses, so it is what the dialog shows."""
    from oduflow.docker_ops import service_ops

    settings, team = _settings(tmp_path)
    client = MagicMock()
    client.containers.get.return_value = _container(
        ["PATH=/usr/bin", "MEILI_ENV=production", "IMAGE_DEFAULT=1"]
    )
    with (
        patch("oduflow.docker_ops.service_ops.get_client", return_value=client),
        patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value={"name": "meili", "env_vars": {"MEILI_ENV": "production"}},
        ),
    ):
        assert service_ops.get_service_env_vars(settings, team, "meili") == {
            "MEILI_ENV": "production"
        }


def test_env_vars_falls_back_to_the_container(tmp_path):
    """A legacy service without a preset is read off its container instead."""
    from oduflow.docker_ops import service_ops

    settings, team = _settings(tmp_path)
    client = MagicMock()
    client.containers.get.return_value = _container(
        ["PATH=/usr/bin", "HOME=/root", "MEILI_ENV=production", "OPTIONS=a,b"]
    )
    with (
        patch("oduflow.docker_ops.service_ops.get_client", return_value=client),
        patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            side_effect=NotFoundError("no preset"),
        ),
    ):
        assert service_ops.get_service_env_vars(settings, team, "meili") == {
            "MEILI_ENV": "production",
            "OPTIONS": "a,b",
        }


def test_env_vars_propagates_an_unreadable_preset(tmp_path):
    """A preset that exists but cannot be read must not fall back.

    The container's environment carries the image defaults; offering those for
    editing would bake them into the preset on the first save.
    """
    from oduflow.docker_ops import service_ops

    settings, team = _settings(tmp_path)
    client = MagicMock()
    client.containers.get.return_value = _container(["MEILI_ENV=production"])
    with (
        patch("oduflow.docker_ops.service_ops.get_client", return_value=client),
        patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            side_effect=OSError("presets file is unreadable"),
        ),
    ):
        with pytest.raises(OSError):
            service_ops.get_service_env_vars(settings, team, "meili")


def test_env_vars_missing_service(tmp_path):
    from oduflow.docker_ops import service_ops

    settings, team = _settings(tmp_path)
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("missing")
    with patch("oduflow.docker_ops.service_ops.get_client", return_value=client):
        with pytest.raises(NotFoundError):
            service_ops.get_service_env_vars(settings, team, "meili")


def test_api_service_env_vars_returns_mapping(tmp_path):
    client = _client(tmp_path)
    with patch(
        "oduflow.web_ui.service_ops.get_service_env_vars",
        return_value={"MEILI_ENV": "production"},
    ):
        response = client.get("/api/services/meili/env-vars")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "env_vars": {"MEILI_ENV": "production"}}


def test_api_service_env_vars_reports_missing_service(tmp_path):
    client = _client(tmp_path)
    with patch(
        "oduflow.web_ui.service_ops.get_service_env_vars",
        side_effect=NotFoundError("Service 'meili' not found"),
    ):
        response = client.get("/api/services/meili/env-vars")

    assert response.status_code == 404
    assert response.json()["ok"] is False


def test_api_service_update_takes_a_mapping_verbatim(tmp_path):
    """The dashboard sends a mapping so values are never re-split server side."""
    client = _client(tmp_path)
    with patch("oduflow.web_ui.service_ops.update_service", return_value={}) as update:
        response = client.post(
            "/api/services/meili/update",
            json={"env_vars": {"OPTIONS": "a,b", " MEILI_ENV ": "production"}},
        )

    assert response.json()["ok"] is True
    assert update.call_args.kwargs["env_override"] == {
        "OPTIONS": "a,b",
        "MEILI_ENV": "production",
    }


def test_api_service_update_empty_mapping_clears(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.web_ui.service_ops.update_service", return_value={}) as update:
        response = client.post("/api/services/meili/update", json={"env_vars": {}})

    assert response.json()["ok"] is True
    assert update.call_args.kwargs["env_override"] == {}


def test_api_service_update_without_env_vars_keeps_them(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.web_ui.service_ops.update_service", return_value={}) as update:
        response = client.post("/api/services/meili/update", json={})

    assert response.json()["ok"] is True
    assert update.call_args.kwargs["env_override"] is None
