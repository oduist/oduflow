"""Regression tests for dashboard static asset cache versioning."""

from __future__ import annotations

import re

from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui


def _client(tmp_path) -> TestClient:
    settings = Settings(
        base_data_dir=str(tmp_path),
        teams={"1": TeamSettings(team_id="1")},
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app)


def test_chat_script_uses_positive_integer_cache_version(tmp_path):
    client = _client(tmp_path)
    dashboard = client.get("/")

    assert dashboard.status_code == 200
    match = re.search(r"/static/chat\.js\?v=([1-9][0-9]*)", dashboard.text)
    assert match is not None

    versioned = client.get(match.group(0))
    unversioned = client.get("/static/chat.js")

    assert versioned.status_code == 200
    assert versioned.headers["content-type"].startswith("application/javascript")
    assert versioned.headers["cache-control"] == "public, max-age=86400"
    assert versioned.content == unversioned.content
