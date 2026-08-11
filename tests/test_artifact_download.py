"""Tests for the one-time artifact download channel (/oduflow-artifact).

This is how a file generated inside an environment reaches an agent without
being pasted through its context window, so the properties that matter are: it
works without a dashboard session, and the link is worthless the moment it has
been used or has aged out.
"""

from __future__ import annotations

import tempfile

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow import artifact_tokens
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import _PUBLIC_PATHS, mount_web_ui

_PW = "s3cret"
_DATA_DIR = tempfile.mkdtemp(prefix="oduflow-artifact-test-")


def _client() -> TestClient:
    settings = Settings(
        base_data_dir=_DATA_DIR,
        teams={"1": TeamSettings(team_id="1", ui_password=_PW)},
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app)


class TestTokenStore:
    def test_roundtrip(self):
        token = artifact_tokens.issue("sale.pot", b'msgid "x"\n')
        assert artifact_tokens.consume(token) == ("sale.pot", b'msgid "x"\n')

    def test_single_use(self):
        token = artifact_tokens.issue("sale.pot", b"body")
        assert artifact_tokens.consume(token) is not None
        assert artifact_tokens.consume(token) is None

    def test_expires(self):
        token = artifact_tokens.issue("sale.pot", b"body", now=0.0)
        assert artifact_tokens.consume(token, now=599.0) is not None
        token = artifact_tokens.issue("sale.pot", b"body", now=0.0)
        assert artifact_tokens.consume(token, now=601.0) is None

    def test_unknown_token(self):
        assert artifact_tokens.consume("nope") is None
        assert artifact_tokens.consume("") is None

    def test_oversized_artifact_is_refused(self):
        with pytest.raises(ValueError, match="too large"):
            artifact_tokens.issue("big.pot", b"x" * (20 * 1024 * 1024 + 1))

    def test_store_is_bounded(self):
        # An unbounded dict of multi-KB artifacts would be a memory leak with a
        # URL attached; the oldest pending entries are evicted instead.
        tokens = [
            artifact_tokens.issue("f.pot", b"body", now=float(i)) for i in range(25)
        ]
        alive = [t for t in tokens if artifact_tokens.consume(t, now=1.0) is not None]
        assert len(alive) <= 20


class TestDownloadRoute:
    def test_download_needs_no_dashboard_session(self):
        # The agent fetching this with curl has no session; the token is the
        # only credential, so the path must bypass auth like /oduflow-connect.
        assert "/oduflow-artifact" in _PUBLIC_PATHS

        token = artifact_tokens.issue("sale_custom.pot", b'msgid "Budget"\n')
        resp = _client().get(f"/oduflow-artifact?token={token}")

        assert resp.status_code == 200
        assert resp.content == b'msgid "Budget"\n'
        assert (
            resp.headers["content-disposition"]
            == 'attachment; filename="sale_custom.pot"'
        )

    def test_second_fetch_is_404(self):
        token = artifact_tokens.issue("sale_custom.pot", b"body")
        client = _client()
        assert client.get(f"/oduflow-artifact?token={token}").status_code == 200
        assert client.get(f"/oduflow-artifact?token={token}").status_code == 404

    def test_missing_or_bogus_token_is_404(self):
        client = _client()
        assert client.get("/oduflow-artifact").status_code == 404
        assert client.get("/oduflow-artifact?token=bogus").status_code == 404
