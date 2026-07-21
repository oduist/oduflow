"""REST coverage for Agent Chat session history."""

from __future__ import annotations

import base64

from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui

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
