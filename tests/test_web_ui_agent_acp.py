"""REST coverage for Agent Chat session history."""

from __future__ import annotations

import base64
import json

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import _annotate_acp_auth_error, mount_web_ui

_PW = "s3cret"
_BRANCH = "feature-x"
_INFO_URL = f"/api/environments/{_BRANCH}/agent-acp/info?type=claude"
_SESSION_URL = f"/api/environments/{_BRANCH}/agent-acp/session"


def _basic() -> dict[str, str]:
    blob = base64.b64encode(f"admin:{_PW}".encode()).decode()
    return {"Authorization": f"Basic {blob}"}


def _client(tmp_path) -> TestClient:
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team-1"), ui_password=_PW)
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app, headers=_basic())


def _post(client: TestClient, session_id: str, title: str | None = None):
    body = {"type": "claude", "session_id": session_id}
    if title is not None:
        body["title"] = title
    return client.post(_SESSION_URL, json=body)


def test_info_starts_with_empty_history(tmp_path):
    response = _client(tmp_path).get(_INFO_URL)

    assert response.status_code == 200
    assert response.json()["session_id"] is None
    assert response.json()["history"] == []


def test_post_adds_session_and_title_is_first_write_wins(tmp_path):
    client = _client(tmp_path)

    response = _post(client, "sid-1")
    assert response.status_code == 200
    assert response.json()["session_id"] == "sid-1"
    assert response.json()["history"][0]["title"] is None

    response = _post(client, "sid-1", "  Fix invoice totals  ")
    assert response.json()["history"][0]["title"] == "Fix invoice totals"

    response = _post(client, "sid-1", "Replacement")
    assert response.json()["history"][0]["title"] == "Fix invoice totals"


def test_empty_session_clears_current_but_preserves_history(tmp_path):
    client = _client(tmp_path)
    _post(client, "sid-1", "First")

    response = _post(client, "")

    assert response.status_code == 200
    assert response.json()["session_id"] is None
    assert [entry["session_id"] for entry in response.json()["history"]] == ["sid-1"]


def test_posting_historical_session_selects_and_moves_it_to_front(tmp_path):
    client = _client(tmp_path)
    _post(client, "sid-1", "First")
    _post(client, "sid-2", "Second")

    response = _post(client, "sid-1")

    assert response.status_code == 200
    assert response.json()["session_id"] == "sid-1"
    assert [entry["session_id"] for entry in response.json()["history"]] == [
        "sid-1",
        "sid-2",
    ]


@pytest.mark.parametrize(
    ("auth_mode", "expected"),
    [
        ("setup_token", "CLAUDE_CODE_OAUTH_TOKEN"),
        ("api_key", "ANTHROPIC_API_KEY"),
        ("interactive", "run `/login`"),
    ],
)
def test_claude_auth_errors_get_mode_specific_guidance(auth_mode, expected):
    original = {
        "jsonrpc": "2.0",
        "id": 3,
        "error": {
            "code": -32603,
            "message": (
                "Internal error: Failed to authenticate. API Error: 401 "
                '{"type":"error","error":{"type":"authentication_error",'
                '"message":"Invalid bearer token"}}'
            ),
            "data": {"provider": "anthropic"},
        },
    }

    annotated = json.loads(
        _annotate_acp_auth_error(json.dumps(original), "claude", auth_mode, "7")
    )

    assert annotated["id"] == original["id"]
    assert annotated["error"]["code"] == original["error"]["code"]
    assert annotated["error"]["data"] == original["error"]["data"]
    assert annotated["error"]["message"].startswith(original["error"]["message"])
    assert "Oduflow authentication guidance:" in annotated["error"]["message"]
    assert expected in annotated["error"]["message"]


@pytest.mark.parametrize(
    "provider_message",
    [
        "Invalid API key · Fix external API key",
        "OAuth token has expired. Please obtain a new token.",
        "OAuth token revoked",
    ],
)
def test_specific_claude_auth_errors_are_recognized(provider_message):
    frame = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32603, "message": provider_message},
        }
    )

    annotated = _annotate_acp_auth_error(frame, "claude", "setup_token", "1")

    assert (
        "Oduflow authentication guidance:" in json.loads(annotated)["error"]["message"]
    )


@pytest.mark.parametrize(
    ("frame", "agent_type"),
    [
        ("not-json", "claude"),
        (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {
                        "code": -32603,
                        "message": "You've hit your monthly spend limit",
                    },
                }
            ),
            "claude",
        ),
        (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": 401, "message": "Repository access denied"},
                }
            ),
            "claude",
        ),
        (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32603, "message": "Invalid bearer token"},
                }
            ),
            "codex",
        ),
    ],
)
def test_non_claude_auth_failures_are_unchanged(frame, agent_type):
    assert _annotate_acp_auth_error(frame, agent_type, "setup_token", "1") == frame


def test_auth_guidance_is_not_appended_twice():
    frame = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32603, "message": "Invalid bearer token"},
        }
    )
    once = _annotate_acp_auth_error(frame, "claude", "setup_token", "1")

    assert _annotate_acp_auth_error(once, "claude", "setup_token", "1") == once
