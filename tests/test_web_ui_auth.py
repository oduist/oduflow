"""Tests for Web UI auth, focused on the cookie fallback that lets WebSocket
handshakes (which cannot carry an Authorization header) authenticate.

Regression: the Console/SQL terminal buttons opened WebSocket connections that
``BasicAuthMiddleware`` rejected with HTTP 403, because the browser never sends
HTTP Basic credentials on a WebSocket upgrade. The fix mints a signed cookie on
dashboard load and validates it for both HTTP and WebSocket scopes.
"""

from __future__ import annotations

import base64

import pytest
from starlette.applications import Starlette
from starlette.routing import Router, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import (
    _AUTH_COOKIE,
    BasicAuthMiddleware,
    _check_cookie_token,
    _make_ui_token,
    mount_web_ui,
)
from oduflow.locking import LockManager

_PW = "s3cret"


def _settings(routing_mode: str = "port") -> Settings:
    return Settings(
        routing_mode=routing_mode,
        teams={
            "1": TeamSettings(team_id="1", ui_password=_PW),
        },
    )


def _team(settings: Settings) -> TeamSettings:
    return settings.teams["1"]


def _basic(user: str, password: str) -> dict[str, str]:
    blob = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {blob}"}


def _full_app(settings: Settings) -> Starlette:
    """The real web UI sub-app (auto-wrapped in BasicAuthMiddleware since a team
    has a ui_password)."""
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return app


# --- token helpers -------------------------------------------------------


def test_token_roundtrip():
    settings = _settings()
    token = _make_ui_token(_team(settings))
    assert token.startswith("1.")
    assert _check_cookie_token(token, settings) is _team(settings)


def test_token_rejects_tampered_and_garbage():
    settings = _settings()
    token = _make_ui_token(_team(settings))
    assert (
        _check_cookie_token(token[:-1] + ("0" if token[-1] != "0" else "1"), settings)
        is None
    )
    assert _check_cookie_token("1.deadbeef", settings) is None
    assert _check_cookie_token("nope", settings) is None
    assert _check_cookie_token("", settings) is None
    # Unknown team id
    assert _check_cookie_token("99." + token.split(".", 1)[1], settings) is None


def test_parse_cookie():
    parse = BasicAuthMiddleware._parse_cookie
    assert parse("a=1; oduflow_ui_auth=tok; b=2", _AUTH_COOKIE) == "tok"
    assert parse("oduflow_ui_auth=tok", _AUTH_COOKIE) == "tok"
    assert parse("", _AUTH_COOKIE) is None
    assert parse("other=1", _AUTH_COOKIE) is None


# --- HTTP dashboard ------------------------------------------------------


def test_dashboard_requires_auth():
    client = TestClient(_full_app(_settings()))
    resp = client.get("/")
    assert resp.status_code == 401
    assert "Basic" in resp.headers.get("www-authenticate", "")


def test_dashboard_basic_sets_cookie():
    client = TestClient(_full_app(_settings()))
    resp = client.get("/", headers=_basic("admin", _PW))
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "")
    assert f"{_AUTH_COOKIE}=" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=strict" in set_cookie.lower()
    # The minted cookie validates back to the team.
    token = client.cookies.get(_AUTH_COOKIE)
    assert _check_cookie_token(token, _settings()) is not None


def test_dashboard_cookie_fallback_without_basic():
    """The core regression: a request carrying only the cookie (no Authorization
    header) is the exact shape of a browser WebSocket handshake."""
    settings = _settings()
    token = _make_ui_token(_team(settings))
    client = TestClient(_full_app(settings))
    client.cookies.set(_AUTH_COOKIE, token)
    resp = client.get("/")
    assert resp.status_code == 200


def test_dashboard_invalid_cookie_rejected():
    client = TestClient(_full_app(_settings()))
    client.cookies.set(_AUTH_COOKIE, "1.deadbeef")
    resp = client.get("/")
    assert resp.status_code == 401


def test_cookie_secure_flag():
    # plain http, port mode -> no Secure
    client = TestClient(_full_app(_settings("port")))
    resp = client.get("/", headers=_basic("admin", _PW))
    assert "secure" not in resp.headers.get("set-cookie", "").lower()

    # X-Forwarded-Proto: https -> Secure
    headers = {**_basic("admin", _PW), "X-Forwarded-Proto": "https"}
    resp = client.get("/", headers=headers)
    assert "secure" in resp.headers.get("set-cookie", "").lower()

    # routing_mode=traefik -> Secure even on plain http
    client = TestClient(_full_app(_settings("traefik")))
    resp = client.get("/", headers=_basic("admin", _PW))
    assert "secure" in resp.headers.get("set-cookie", "").lower()


# --- WebSocket handshake -------------------------------------------------


async def _stub_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    await websocket.send_text("ok")
    await websocket.close()


def _ws_app(settings: Settings):
    router = Router(
        routes=[WebSocketRoute("/api/environments/{branch:path}/terminal", _stub_ws)]
    )
    return BasicAuthMiddleware(router, lambda: settings)


_WS_URL = "/api/environments/main/terminal"


def test_ws_accepts_valid_cookie():
    settings = _settings()
    token = _make_ui_token(_team(settings))
    client = TestClient(_ws_app(settings))
    with client.websocket_connect(
        _WS_URL, headers={"cookie": f"{_AUTH_COOKIE}={token}"}
    ) as ws:
        assert ws.receive_text() == "ok"


def test_ws_rejects_without_cookie():
    client = TestClient(_ws_app(_settings()))
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(_WS_URL):
            pass


def test_ws_rejects_invalid_cookie():
    client = TestClient(_ws_app(_settings()))
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            _WS_URL, headers={"cookie": f"{_AUTH_COOKIE}=1.deadbeef"}
        ):
            pass


def test_ws_accepts_basic_header():
    """Header path still works (e.g. non-browser clients)."""
    client = TestClient(_ws_app(_settings()))
    with client.websocket_connect(_WS_URL, headers=_basic("admin", _PW)) as ws:
        assert ws.receive_text() == "ok"
