"""Env-var handling in the dashboard REST API.

api_update treats an ``env_vars`` key present in the body as a full
replacement (an empty string or an empty mapping clears every user-supplied
variable), while an absent key keeps the current ones. A mapping is taken
verbatim; the legacy string form is parsed for existing REST clients. api_env_vars returns the persisted
``oduflow.env_vars`` label for a single environment so the update dialog can
prefill the current values.
"""

from unittest.mock import MagicMock, patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

import docker
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _client(tmp_path):
    settings = Settings(
        routing_mode="port",
        base_data_dir=str(tmp_path),
        teams={"1": TeamSettings(team_id="1")},
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app)


def test_api_update_parses_env_vars(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.web_ui.env_ops.update_environment", return_value={}) as update:
        resp = client.post(
            "/api/environments/main/update",
            json={"env_vars": "WORKERS=2\nLIMIT_TIME_CPU=600"},
        )
    assert resp.json()["ok"] is True
    assert update.call_args.kwargs["env_override"] == {
        "WORKERS": "2",
        "LIMIT_TIME_CPU": "600",
    }


def test_api_update_accepts_mapping_verbatim(tmp_path):
    """The dashboard sends a mapping so values are never re-split server side."""
    client = _client(tmp_path)
    with patch("oduflow.web_ui.env_ops.update_environment", return_value={}) as update:
        resp = client.post(
            "/api/environments/main/update",
            json={"env_vars": {"OPTIONS": "a,b", "WEIRD": "x,B=c", " WORKERS ": "2"}},
        )
    assert resp.json()["ok"] is True
    assert update.call_args.kwargs["env_override"] == {
        "OPTIONS": "a,b",
        "WEIRD": "x,B=c",
        "WORKERS": "2",
    }


def test_api_update_empty_mapping_clears(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.web_ui.env_ops.update_environment", return_value={}) as update:
        resp = client.post("/api/environments/main/update", json={"env_vars": {}})
    assert resp.json()["ok"] is True
    assert update.call_args.kwargs["env_override"] == {}


def test_api_update_keeps_commas_inside_values(tmp_path):
    """A stack file can set a value holding commas; editing must not truncate it."""
    client = _client(tmp_path)
    with patch("oduflow.web_ui.env_ops.update_environment", return_value={}) as update:
        resp = client.post(
            "/api/environments/main/update",
            json={"env_vars": "CONNECT_MCP_TOOL_GROUPS=write,collaboration\nWORKERS=2"},
        )
    assert resp.json()["ok"] is True
    assert update.call_args.kwargs["env_override"] == {
        "CONNECT_MCP_TOOL_GROUPS": "write,collaboration",
        "WORKERS": "2",
    }


def test_api_update_comma_separated_pairs_still_split(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.web_ui.env_ops.update_environment", return_value={}) as update:
        resp = client.post(
            "/api/environments/main/update",
            json={"env_vars": "WORKERS=2,LIMIT_TIME_CPU=600"},
        )
    assert resp.json()["ok"] is True
    assert update.call_args.kwargs["env_override"] == {
        "WORKERS": "2",
        "LIMIT_TIME_CPU": "600",
    }


def test_api_update_empty_env_vars_clears(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.web_ui.env_ops.update_environment", return_value={}) as update:
        resp = client.post("/api/environments/main/update", json={"env_vars": ""})
    assert resp.json()["ok"] is True
    assert update.call_args.kwargs["env_override"] == {}


def test_api_update_absent_env_vars_keeps_current(tmp_path):
    client = _client(tmp_path)
    with patch("oduflow.web_ui.env_ops.update_environment", return_value={}) as update:
        resp = client.post("/api/environments/main/update", json={})
    assert resp.json()["ok"] is True
    assert update.call_args.kwargs["env_override"] is None


def test_api_create_accepts_mapping(tmp_path):
    """The create dialog posts a mapping too, so both dialogs share a contract."""
    client = _client(tmp_path)
    with patch("oduflow.web_ui.env_ops.create_environment", return_value={}) as create:
        resp = client.post(
            "/api/environments/create",
            json={
                "env_name": "main",
                "repo_url": "https://example.invalid/repo.git",
                "odoo_image": "odoo:19.0",
                "env_vars": {"OPTIONS": "a,b"},
            },
        )
    assert resp.json()["ok"] is True, resp.json()
    assert create.call_args.kwargs["env_vars"] == {"OPTIONS": "a,b"}


def test_api_env_vars_returns_label(tmp_path):
    client = _client(tmp_path)
    container = MagicMock()
    container.labels = {"oduflow.env_vars": '{"WORKERS": "2"}'}
    docker_client = MagicMock()
    docker_client.containers.get.return_value = container
    with patch("oduflow.docker_ops.client.get_client", return_value=docker_client):
        resp = client.get("/api/environments/main/env-vars")
    data = resp.json()
    assert data["ok"] is True
    assert data["env_vars"] == {"WORKERS": "2"}


def test_api_env_vars_missing_env_is_404(tmp_path):
    client = _client(tmp_path)
    docker_client = MagicMock()
    docker_client.containers.get.side_effect = docker.errors.NotFound("gone")
    with patch("oduflow.docker_ops.client.get_client", return_value=docker_client):
        resp = client.get("/api/environments/main/env-vars")
    assert resp.status_code == 404
    assert resp.json()["ok"] is False
