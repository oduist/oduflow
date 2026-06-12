"""Tests for Web UI auth, focused on the cookie fallback that lets WebSocket
handshakes (which cannot carry an Authorization header) authenticate.

Regression: the Console/SQL terminal buttons opened WebSocket connections that
``BasicAuthMiddleware`` rejected with HTTP 403, because the browser never sends
HTTP Basic credentials on a WebSocket upgrade. The fix mints a signed session
cookie on dashboard load and validates it for both HTTP and WebSocket scopes.

The cookie is an ``itsdangerous`` timed token signed with a persistent
server-side secret (stored in the data dir), so it survives restarts, expires
after ``_SESSION_MAX_AGE``, and never exposes the team password.
"""

from __future__ import annotations

import base64
import tempfile

import pytest
from starlette.applications import Starlette
from starlette.routing import Router, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from oduflow import web_ui
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import (
    _AUTH_COOKIE,
    BasicAuthMiddleware,
    _check_cookie_token,
    _get_signer,
    _make_ui_token,
    mount_web_ui,
)
from oduflow.locking import LockManager

_PW = "s3cret"

# A single shared data dir so every ``_settings()`` instance reads the same
# persisted signing secret (mirrors one real server with one data dir).
_DATA_DIR = tempfile.mkdtemp(prefix="oduflow-uiauth-test-")


def _settings(routing_mode: str = "port") -> Settings:
    return Settings(
        routing_mode=routing_mode,
        base_data_dir=_DATA_DIR,
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
    token = _make_ui_token(_team(settings), settings)
    assert _check_cookie_token(token, settings) is _team(settings)


def test_token_rejects_tampered_and_garbage():
    settings = _settings()
    token = _make_ui_token(_team(settings), settings)
    # Flip the last character -> signature no longer verifies.
    assert (
        _check_cookie_token(token[:-1] + ("0" if token[-1] != "0" else "1"), settings)
        is None
    )
    assert _check_cookie_token("1.deadbeef", settings) is None
    assert _check_cookie_token("nope", settings) is None
    assert _check_cookie_token("", settings) is None


def test_token_rejects_unknown_team():
    settings = _settings()
    # Validly-signed (same secret) but for a team that is not configured.
    token = _get_signer(settings).dumps(["99", "deadbeef"])
    assert _check_cookie_token(token, settings) is None


def test_token_rejects_foreign_secret(tmp_path):
    settings = _settings()
    token = _make_ui_token(_team(settings), settings)
    # A different data dir => a different signing secret => token must not pass.
    other = Settings(
        routing_mode="port",
        base_data_dir=str(tmp_path),
        teams={"1": TeamSettings(team_id="1", ui_password=_PW)},
    )
    web_ui._signers.pop(str(tmp_path), None)
    web_ui._secrets_cache.pop(str(tmp_path), None)
    assert _check_cookie_token(token, other) is None


def test_token_expires(monkeypatch):
    settings = _settings()
    token = _make_ui_token(_team(settings), settings)
    # Any positive age now exceeds the (negative) max age -> SignatureExpired.
    monkeypatch.setattr(web_ui, "_SESSION_MAX_AGE", -1)
    assert _check_cookie_token(token, settings) is None


def test_secret_persists_across_restart():
    """A persistent server secret means tokens survive a process restart."""
    settings = _settings()
    token = _make_ui_token(_team(settings), settings)
    # Simulate a restart: drop the in-memory caches; the secret file in the
    # data dir is re-read, yielding the same key.
    web_ui._signers.clear()
    web_ui._secrets_cache.clear()
    assert _check_cookie_token(token, settings) is _team(settings)


def test_token_revoked_on_password_change():
    """Changing ui_password invalidates outstanding cookies immediately."""
    settings = _settings()
    token = _make_ui_token(_team(settings), settings)
    # Same team, same data dir/secret, but a new password: the embedded
    # fingerprint no longer matches -> the cookie is rejected at once.
    changed = Settings(
        routing_mode="port",
        base_data_dir=_DATA_DIR,
        teams={"1": TeamSettings(team_id="1", ui_password="new-password")},
    )
    assert _check_cookie_token(token, changed) is None


def test_token_rejected_when_ui_password_cleared():
    settings = _settings()
    token = _make_ui_token(_team(settings), settings)
    # Auth turned off for the team afterwards -> cookie no longer authenticates.
    disabled = Settings(
        routing_mode="port",
        base_data_dir=_DATA_DIR,
        teams={"1": TeamSettings(team_id="1", ui_password="")},
    )
    assert _check_cookie_token(token, disabled) is None


# --- HTTP dashboard ------------------------------------------------------


def test_dashboard_redirects_to_login_when_unauthenticated():
    client = TestClient(_full_app(_settings()))
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"
    # No Basic dialog is triggered anymore.
    assert "www-authenticate" not in {k.lower() for k in resp.headers}


def test_login_page_is_public():
    client = TestClient(_full_app(_settings()))
    resp = client.get("/login")
    assert resp.status_code == 200
    assert "password" in resp.text.lower()


def test_login_success_sets_cookie_and_redirects():
    client = TestClient(_full_app(_settings()))
    resp = client.post("/login", data={"password": _PW}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert f"{_AUTH_COOKIE}=" in resp.headers.get("set-cookie", "")
    # The minted cookie now authenticates the dashboard without Basic.
    assert client.get("/").status_code == 200


def test_login_wrong_password_rejected():
    client = TestClient(_full_app(_settings()))
    resp = client.post("/login", data={"password": "nope"}, follow_redirects=False)
    assert resp.status_code == 401
    assert _AUTH_COOKIE not in resp.headers.get("set-cookie", "")


def test_login_redirects_when_already_authenticated():
    settings = _settings()
    token = _make_ui_token(_team(settings), settings)
    client = TestClient(_full_app(settings))
    client.cookies.set(_AUTH_COOKIE, token)
    resp = client.get("/login", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


def test_logout_clears_cookie():
    client = TestClient(_full_app(_settings()))
    client.post("/login", data={"password": _PW})
    resp = client.post("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    set_cookie = resp.headers.get("set-cookie", "").lower()
    assert _AUTH_COOKIE in set_cookie
    assert "max-age=0" in set_cookie or "expires=" in set_cookie


def test_api_unauthenticated_is_401_without_basic_challenge():
    client = TestClient(_full_app(_settings()))
    resp = client.get("/api/license", follow_redirects=False)
    assert resp.status_code == 401
    assert "www-authenticate" not in {k.lower() for k in resp.headers}


def test_api_basic_auth_still_works():
    client = TestClient(_full_app(_settings()))
    resp = client.get("/api/license", headers=_basic("admin", _PW))
    assert resp.status_code == 200


def test_dashboard_basic_sets_cookie():
    client = TestClient(_full_app(_settings()))
    resp = client.get("/", headers=_basic("admin", _PW))
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "")
    assert f"{_AUTH_COOKIE}=" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=strict" in set_cookie.lower()
    assert "max-age=" in set_cookie.lower()
    # The minted cookie validates back to the team.
    token = client.cookies.get(_AUTH_COOKIE)
    assert _check_cookie_token(token, _settings()) is not None


def test_dashboard_cookie_fallback_without_basic():
    """The core regression: a request carrying only the cookie (no Authorization
    header) is the exact shape of a browser WebSocket handshake."""
    settings = _settings()
    token = _make_ui_token(_team(settings), settings)
    client = TestClient(_full_app(settings))
    client.cookies.set(_AUTH_COOKIE, token)
    resp = client.get("/")
    assert resp.status_code == 200


def test_dashboard_invalid_cookie_redirects_to_login():
    client = TestClient(_full_app(_settings()))
    client.cookies.set(_AUTH_COOKIE, "1.deadbeef")
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


def test_non_ascii_cookie_does_not_crash():
    """A malformed/non-ASCII cookie must be rejected cleanly, never raise.

    The previous HMAC implementation called ``hmac.compare_digest`` on the raw
    cookie value, which raises ``TypeError`` on non-ASCII input (an
    unauthenticated 500). ``itsdangerous`` rejects it as ``BadData`` instead.
    """
    settings = _settings()
    assert _check_cookie_token("1.é", settings) is None
    assert _check_cookie_token("é", settings) is None


def test_cookie_secure_flag():
    # plain http, port mode -> no Secure
    client = TestClient(_full_app(_settings("port")))
    resp = client.get("/", headers=_basic("admin", _PW))
    assert "secure" not in resp.headers.get("set-cookie", "").lower()

    # X-Forwarded-Proto: https -> Secure
    headers = {**_basic("admin", _PW), "X-Forwarded-Proto": "https"}
    resp = client.get("/", headers=headers)
    assert "secure" in resp.headers.get("set-cookie", "").lower()

    # Chained proxies "https, http": first hop is https -> Secure
    headers = {**_basic("admin", _PW), "X-Forwarded-Proto": "https, http"}
    resp = client.get("/", headers=headers)
    assert "secure" in resp.headers.get("set-cookie", "").lower()

    # traefik mode but reached over plain http with no X-Forwarded-Proto:
    # NOT Secure, otherwise the browser would silently drop the cookie and the
    # terminal buttons would stay broken on that origin.
    client = TestClient(_full_app(_settings("traefik")))
    resp = client.get("/", headers=_basic("admin", _PW))
    assert "secure" not in resp.headers.get("set-cookie", "").lower()


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
    token = _make_ui_token(_team(settings), settings)
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
