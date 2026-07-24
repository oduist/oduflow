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


def test_chat_assets_share_one_positive_integer_cache_version(tmp_path):
    client = _client(tmp_path)
    dashboard = client.get("/")
    assert dashboard.status_code == 200

    # chat.js and acp-client.js are an interdependent pair; the dashboard holds
    # ONE shared cache version (CHAT_V) so a single bump busts both at once.
    match = re.search(r"var CHAT_V = '([1-9][0-9]*)'", dashboard.text)
    assert match is not None, "CHAT_V positive integer cache version not found"
    version = match.group(1)

    for filename in ("chat.js", "acp-client.js"):
        # Both assets must reference the shared CHAT_V, not a hardcoded number,
        # so they can never drift out of sync.
        assert re.search(
            rf"/static/{re.escape(filename)}\?v='\s*\+\s*CHAT_V", dashboard.text
        ), f"{filename} does not use the shared CHAT_V cache version"

        versioned = client.get(f"/static/{filename}?v={version}")
        unversioned = client.get(f"/static/{filename}")

        assert versioned.status_code == 200
        assert versioned.headers["content-type"].startswith("application/javascript")
        assert versioned.headers["cache-control"] == "public, max-age=86400"
        assert versioned.content == unversioned.content


def test_environment_metadata_shows_live_mount_path(tmp_path):
    client = _client(tmp_path)
    dashboard = client.get("/")

    assert dashboard.status_code == 200
    assert "env.local_path ? '<span>Live-mount:" in dashboard.text
