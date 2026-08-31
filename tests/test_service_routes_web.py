from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _client(tmp_path):
    team = TeamSettings(team_id="1", hostname="example.com", data_dir=str(tmp_path))
    settings = Settings(
        routing_mode="traefik",
        routing_tls=False,
        base_data_dir=str(tmp_path),
        teams={"1": team},
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app)


def test_create_service_accepts_structured_routes(tmp_path):
    client = _client(tmp_path)
    routes = [{"path": "/RPC2", "port": 8080, "strip_prefix": False}]
    with patch("oduflow.web_ui.service_ops.create_service") as create:
        create.return_value = {
            "name": "fs",
            "container_name": "oduflow-1-svc-fs",
            "image": "fs:1",
            "url": "https://fs.example.com",
            "routes": routes,
        }
        response = client.post(
            "/api/services/create",
            json={"name": "fs", "image": "fs:1", "routes": routes},
        )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert create.call_args.args[4] is None
    assert create.call_args.kwargs["routes"] == routes


def test_create_service_rejects_invalid_name_with_explanation(tmp_path):
    client = _client(tmp_path)

    response = client.post(
        "/api/services/create",
        json={"name": "Odoo MCP server", "image": "example/mcp:1", "port": 8080},
    )

    assert response.status_code == 400
    assert response.json() == {
        "ok": False,
        "error": (
            "Invalid service name 'Odoo MCP server': must start with a letter "
            "or digit and contain only letters, digits, dots, hyphens, and "
            "underscores. Spaces are not allowed; use hyphens instead (for "
            "example, 'odoo-mcp-server')."
        ),
    }


def test_update_service_distinguishes_missing_and_empty_routes(tmp_path):
    client = _client(tmp_path)
    result = {
        "name": "fs",
        "container_name": "oduflow-1-svc-fs",
        "image": "fs:1",
        "url": "https://fs.example.com",
    }
    with patch(
        "oduflow.web_ui.service_ops.update_service", return_value=result
    ) as update:
        response = client.post(
            "/api/services/fs/update", json={"routes": [], "port": 8080}
        )

    assert response.status_code == 200
    assert update.call_args.kwargs["routes_override"] == []
    assert update.call_args.kwargs["port_override"] == 8080


def test_create_service_accepts_command_as_string_or_argv(tmp_path):
    client = _client(tmp_path)
    result = {
        "name": "minio",
        "container_name": "oduflow-1-svc-minio",
        "image": "minio/minio:latest",
        "url": "https://minio.example.com",
    }
    with patch(
        "oduflow.web_ui.service_ops.create_service", return_value=result
    ) as create:
        client.post(
            "/api/services/create",
            json={
                "name": "minio",
                "image": "minio/minio:latest",
                "port": 9000,
                "command": "server /data",
            },
        )
        assert create.call_args.kwargs["command"] == ["server", "/data"]

        client.post(
            "/api/services/create",
            json={
                "name": "minio",
                "image": "minio/minio:latest",
                "port": 9000,
                # A list is argv already — an element may hold spaces.
                "command": ["server", "/data dir"],
            },
        )
        assert create.call_args.kwargs["command"] == ["server", "/data dir"]


def test_create_service_rejects_invalid_command_shapes(tmp_path):
    client = _client(tmp_path)

    with patch("oduflow.web_ui.service_ops.create_service") as create:
        for command in (True, 123, {"executable": "server"}, ["server", None]):
            response = client.post(
                "/api/services/create",
                json={
                    "name": "minio",
                    "image": "minio/minio:latest",
                    "port": 9000,
                    "command": command,
                },
            )

            assert response.status_code == 400
            assert response.json() == {
                "ok": False,
                "error": (
                    "command must be a shell-quoted string or an array of strings"
                ),
            }

    create.assert_not_called()


def test_update_service_distinguishes_missing_and_empty_command(tmp_path):
    client = _client(tmp_path)
    result = {
        "name": "minio",
        "container_name": "oduflow-1-svc-minio",
        "image": "minio/minio:latest",
        "url": "https://minio.example.com",
    }
    with patch(
        "oduflow.web_ui.service_ops.update_service", return_value=result
    ) as update:
        client.post("/api/services/minio/update", json={})
        assert update.call_args.kwargs["command_override"] is None

        client.post("/api/services/minio/update", json={"command": ""})
        assert update.call_args.kwargs["command_override"] == []

        client.post("/api/services/minio/update", json={"command": "server /data"})
        assert update.call_args.kwargs["command_override"] == ["server", "/data"]
