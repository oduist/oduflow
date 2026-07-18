"""Tests for the POST /api/llm-usage REST endpoint (UID-authenticated, public path)."""

from __future__ import annotations

import os

from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow import usage
from oduflow.locking import LockManager
from oduflow.naming import get_workspace_path
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _settings(tmp_path) -> Settings:
    base = str(tmp_path)
    # ui_password set so BasicAuthMiddleware is active — proves /api/llm-usage is
    # reachable via its UID alone, without a dashboard login.
    team = TeamSettings(
        team_id="1", data_dir=os.path.join(base, "team_1"), ui_password="pw"
    )
    return Settings(base_data_dir=base, teams={"1": team})


def _app(settings: Settings) -> Starlette:
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return app


def _provision(settings: Settings, env_name: str) -> str:
    team = settings.teams["1"]
    os.makedirs(get_workspace_path(env_name, team.workspaces_dir), exist_ok=True)
    return usage.register_token(settings, team, env_name)


def test_usage_post_records_and_returns_aggregate(tmp_path):
    settings = _settings(tmp_path)
    uid = _provision(settings, "feature/x")
    client = TestClient(_app(settings))
    body = {
        "session_id": "A",
        "duration_seconds": 60,
        "models": {"claude-opus-4-8": {"input_tokens": 100, "output_tokens": 50}},
    }
    resp = client.post("/api/llm-usage", json=body, headers={"X-Oduflow-Env-Uid": uid})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["usage"]["totals"]["input_tokens"] == 100
    assert data["usage"]["sessions"] == 1

    # Idempotent per session_id.
    resp2 = client.post("/api/llm-usage", json=body, headers={"X-Oduflow-Env-Uid": uid})
    assert resp2.json()["usage"]["totals"]["input_tokens"] == 100
    assert resp2.json()["usage"]["sessions"] == 1


def test_usage_post_persists_to_workspace(tmp_path):
    settings = _settings(tmp_path)
    uid = _provision(settings, "x")
    client = TestClient(_app(settings))
    client.post(
        "/api/llm-usage",
        json={"session_id": "S", "models": {"m": {"input_tokens": 7}}},
        headers={"X-Oduflow-Env-Uid": uid},
    )
    team = settings.teams["1"]
    assert os.path.isfile(usage.env_usage_path(team, "x"))
    assert usage.get_env_usage(team, "x")["totals"]["input_tokens"] == 7


def test_usage_post_rejects_missing_or_bad_uid(tmp_path):
    settings = _settings(tmp_path)
    client = TestClient(_app(settings))
    body = {"session_id": "A", "models": {}}
    assert client.post("/api/llm-usage", json=body).status_code == 401
    assert (
        client.post(
            "/api/llm-usage", json=body, headers={"X-Oduflow-Env-Uid": "bogus"}
        ).status_code
        == 401
    )


def test_usage_post_requires_session_id(tmp_path):
    settings = _settings(tmp_path)
    uid = _provision(settings, "x")
    client = TestClient(_app(settings))
    resp = client.post(
        "/api/llm-usage", json={"models": {}}, headers={"X-Oduflow-Env-Uid": uid}
    )
    assert resp.status_code == 400


def test_usage_post_reachable_without_login(tmp_path):
    # No session cookie, no Basic auth — the UID is the only credential.
    settings = _settings(tmp_path)
    uid = _provision(settings, "x")
    client = TestClient(_app(settings))
    resp = client.post(
        "/api/llm-usage",
        json={"session_id": "S", "models": {"m": {"input_tokens": 1}}},
        headers={"X-Oduflow-Env-Uid": uid},
    )
    assert resp.status_code == 200
