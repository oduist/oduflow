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
from starlette.requests import HTTPConnection
from starlette.routing import Router, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from oduflow import web_ui
from oduflow.licensing import LicenseInfo, TYPE_INDIVIDUAL, TYPE_UNLICENSED
from oduflow.settings import DEFAULT_LOGIN_PATH, Settings, TeamSettings
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


def _settings(
    routing_mode: str = "port",
    etc_dir: str = "",
    *,
    prod_enabled: bool = False,
    login_path: str = DEFAULT_LOGIN_PATH,
    trusted_proxies: tuple[str, ...] = (),
) -> Settings:
    return Settings(
        routing_mode=routing_mode,
        prod_enabled=prod_enabled,
        web_login_path=login_path,
        trusted_proxies=trusted_proxies,
        base_data_dir=_DATA_DIR,
        etc_dir=etc_dir,
        teams={
            "1": TeamSettings(team_id="1", ui_password=_PW),
        },
    )


def _team(settings: Settings) -> TeamSettings:
    return settings.teams["1"]


def _conn(peer: str, headers: dict[str, str]) -> HTTPConnection:
    """A bare ASGI connection with a given TCP peer and headers."""
    return HTTPConnection(
        {
            "type": "http",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": (peer, 12345),
        }
    )


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
    # Flip the first character (payload start) -> signature no longer matches.
    # (The *last* character is unsafe to flip: it is the final base64 char of
    # the signature, whose two low bits are padding — '0' vs '1' can decode to
    # the same bytes, which made this test flaky.)
    flipped = ("0" if token[0] != "0" else "1") + token[1:]
    assert _check_cookie_token(flipped, settings) is None
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
    assert resp.headers["location"] == DEFAULT_LOGIN_PATH
    # No Basic dialog is triggered anymore.
    assert "www-authenticate" not in {k.lower() for k in resp.headers}


def test_login_page_is_public():
    client = TestClient(_full_app(_settings()))
    resp = client.get(DEFAULT_LOGIN_PATH)
    assert resp.status_code == 200
    assert "password" in resp.text.lower()


def test_login_success_sets_cookie_and_redirects():
    client = TestClient(_full_app(_settings()))
    resp = client.post(
        DEFAULT_LOGIN_PATH, data={"password": _PW}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert f"{_AUTH_COOKIE}=" in resp.headers.get("set-cookie", "")
    # The minted cookie now authenticates the dashboard without Basic.
    assert client.get("/").status_code == 200


def test_login_wrong_password_rejected():
    client = TestClient(_full_app(_settings()))
    resp = client.post(
        DEFAULT_LOGIN_PATH, data={"password": "nope"}, follow_redirects=False
    )
    assert resp.status_code == 401
    assert _AUTH_COOKIE not in resp.headers.get("set-cookie", "")


def test_login_rate_limited_after_repeated_failures():
    # Issue #56: the login endpoint must throttle brute-force attempts.
    client = TestClient(_full_app(_settings()))
    for _ in range(10):
        resp = client.post(
            DEFAULT_LOGIN_PATH, data={"password": "nope"}, follow_redirects=False
        )
        assert resp.status_code == 401
    # The 11th attempt (and beyond) is locked out, even with the right password.
    resp = client.post(
        DEFAULT_LOGIN_PATH, data={"password": _PW}, follow_redirects=False
    )
    assert resp.status_code == 429
    assert _AUTH_COOKIE not in resp.headers.get("set-cookie", "")
    # A locked-out client is told when to come back.
    assert int(resp.headers["retry-after"]) > 0


def test_login_lockout_expires_after_window(monkeypatch):
    """Once the sliding window passes, the same client may try again."""
    client = TestClient(_full_app(_settings()))
    for _ in range(10):
        client.post(DEFAULT_LOGIN_PATH, data={"password": "nope"})
    assert client.post(DEFAULT_LOGIN_PATH, data={"password": _PW}).status_code == 429

    # Advance the clock past the 300s window (the limiter uses time.monotonic).
    real_monotonic = web_ui.time.monotonic
    monkeypatch.setattr(
        web_ui.time, "monotonic", lambda: real_monotonic() + 301, raising=False
    )
    resp = client.post(
        DEFAULT_LOGIN_PATH, data={"password": _PW}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert f"{_AUTH_COOKIE}=" in resp.headers.get("set-cookie", "")


def test_login_success_resets_the_failure_counter():
    client = TestClient(_full_app(_settings()))
    for _ in range(9):
        assert (
            client.post(DEFAULT_LOGIN_PATH, data={"password": "nope"}).status_code
            == 401
        )
    assert client.post(DEFAULT_LOGIN_PATH, data={"password": _PW}).status_code == 200
    # Drop the session cookie: otherwise the login handler short-circuits to
    # "already signed in" and never reaches the throttle.
    client.cookies.clear()
    # The counter is cleared, so 9 more failures still do not lock the client out.
    for _ in range(9):
        assert (
            client.post(DEFAULT_LOGIN_PATH, data={"password": "nope"}).status_code
            == 401
        )


def test_login_throttle_is_per_client_ip():
    """One noisy IP must not lock out everyone else (below the global cap)."""
    app = _full_app(_settings())
    attacker = TestClient(app, client=("10.0.0.1", 1234))
    victim = TestClient(app, client=("10.0.0.2", 1234))
    for _ in range(10):
        attacker.post(DEFAULT_LOGIN_PATH, data={"password": "nope"})
    assert attacker.post(DEFAULT_LOGIN_PATH, data={"password": _PW}).status_code == 429
    assert victim.post(DEFAULT_LOGIN_PATH, data={"password": _PW}).status_code == 200


def test_login_throttle_ignores_spoofed_forwarding_headers():
    """X-Forwarded-For is attacker-controlled: with no trusted proxy configured
    a fresh value must not reset the bucket, or the limit is trivially bypassed."""
    client = TestClient(_full_app(_settings()), client=("198.51.100.7", 1234))
    for i in range(10):
        resp = client.post(
            DEFAULT_LOGIN_PATH,
            data={"password": "nope"},
            headers={"X-Forwarded-For": f"203.0.113.{i}"},
        )
        assert resp.status_code == 401
    resp = client.post(
        DEFAULT_LOGIN_PATH,
        data={"password": _PW},
        headers={"X-Forwarded-For": "203.0.113.99"},
        follow_redirects=False,
    )
    assert resp.status_code == 429


# --- client IP resolution (trusted proxies) ------------------------------
#
# Uvicorn's own proxy_headers layer resolves scope["client"] when the peer is
# in forwarded_allow_ips (server._forwarded_allow_ips keeps the two lists in
# step). These tests drive the app layer directly, which is what runs when the
# peer was not trusted there — and is idempotent when it was.


def test_client_ip_direct_connection_uses_peer():
    settings = _settings(trusted_proxies=("10.0.0.1",))
    conn = _conn("203.0.113.5", {"x-forwarded-for": "192.0.2.9"})
    # The peer is a plain client, not the proxy -> its header is ignored.
    assert web_ui._client_ip(conn, settings) == "203.0.113.5"


def test_client_ip_trusted_proxy_uses_forwarded_header():
    settings = _settings(trusted_proxies=("10.0.0.1",))
    conn = _conn("10.0.0.1", {"x-forwarded-for": "203.0.113.5"})
    assert web_ui._client_ip(conn, settings) == "203.0.113.5"


def test_client_ip_trusted_proxy_cidr():
    settings = _settings(trusted_proxies=("10.0.0.0/24",))
    conn = _conn("10.0.0.77", {"x-forwarded-for": "203.0.113.5"})
    assert web_ui._client_ip(conn, settings) == "203.0.113.5"


def test_client_ip_untrusted_peer_cannot_spoof():
    """The core property: without trust, the header is inert."""
    settings = _settings(trusted_proxies=("10.0.0.1",))
    for spoof in ("203.0.113.5", "10.0.0.1", "127.0.0.1"):
        conn = _conn("198.51.100.7", {"x-forwarded-for": spoof})
        assert web_ui._client_ip(conn, settings) == "198.51.100.7"


def test_client_ip_default_trusts_no_proxy():
    settings = _settings()  # no trusted_proxies configured
    conn = _conn("10.0.0.1", {"x-forwarded-for": "203.0.113.5"})
    assert web_ui._client_ip(conn, settings) == "10.0.0.1"


def test_client_ip_takes_rightmost_untrusted_hop():
    """A client may prepend fake hops; only what the trusted chain appended counts."""
    settings = _settings(trusted_proxies=("10.0.0.1", "10.0.0.2"))
    conn = _conn(
        "10.0.0.1",
        # Client-supplied junk, then the real client, then trusted hops.
        {"x-forwarded-for": "1.2.3.4, 203.0.113.5, 10.0.0.2"},
    )
    assert web_ui._client_ip(conn, settings) == "203.0.113.5"


def test_client_ip_falls_back_to_peer_without_header():
    settings = _settings(trusted_proxies=("10.0.0.1",))
    assert web_ui._client_ip(_conn("10.0.0.1", {}), settings) == "10.0.0.1"
    # Every hop trusted (nothing untrusted to pick): the peer, not a guess.
    conn = _conn("10.0.0.1", {"x-forwarded-for": "10.0.0.1"})
    assert web_ui._client_ip(conn, settings) == "10.0.0.1"


def test_throttle_separates_clients_behind_one_trusted_proxy():
    """Two users behind the same proxy must get independent buckets."""
    app = _full_app(_settings(trusted_proxies=("10.0.0.1",)))
    proxy = TestClient(app, client=("10.0.0.1", 1234))
    noisy = {"X-Forwarded-For": "203.0.113.5"}
    quiet = {"X-Forwarded-For": "203.0.113.6"}
    for _ in range(10):
        assert (
            proxy.post(
                DEFAULT_LOGIN_PATH, data={"password": "nope"}, headers=noisy
            ).status_code
            == 401
        )
    assert (
        proxy.post(
            DEFAULT_LOGIN_PATH, data={"password": _PW}, headers=noisy
        ).status_code
        == 429
    )
    assert (
        proxy.post(
            DEFAULT_LOGIN_PATH, data={"password": _PW}, headers=quiet
        ).status_code
        == 200
    )


def test_throttle_behind_proxy_still_blocks_a_spoofing_client():
    """A client behind a trusted proxy cannot escape by prepending fake hops:
    the proxy appends the real address to the right of whatever it sent."""
    app = _full_app(_settings(trusted_proxies=("10.0.0.1",)))
    proxy = TestClient(app, client=("10.0.0.1", 1234))
    for i in range(10):
        resp = proxy.post(
            DEFAULT_LOGIN_PATH,
            data={"password": "nope"},
            headers={"X-Forwarded-For": f"192.0.2.{i}, 203.0.113.5"},
        )
        assert resp.status_code == 401
    resp = proxy.post(
        DEFAULT_LOGIN_PATH,
        data={"password": _PW},
        headers={"X-Forwarded-For": "192.0.2.99, 203.0.113.5"},
        follow_redirects=False,
    )
    assert resp.status_code == 429


def test_login_failure_response_is_generic():
    """No user enumeration: a missing password and a wrong one look identical."""
    client = TestClient(_full_app(_settings()))
    empty = client.post(DEFAULT_LOGIN_PATH, data={"password": ""})
    wrong = client.post(DEFAULT_LOGIN_PATH, data={"password": "definitely-not-it"})
    assert empty.status_code == wrong.status_code == 401
    assert empty.text == wrong.text
    assert _PW not in wrong.text


def test_legacy_login_path_is_404_not_a_redirect():
    """The old /login must not survive as an alias or redirect (scanner noise)."""
    client = TestClient(_full_app(_settings()))
    for method in ("get", "post"):
        resp = getattr(client, method)("/login", follow_redirects=False)
        assert resp.status_code == 404, method
        assert "location" not in {k.lower() for k in resp.headers}


def test_unknown_path_is_404_not_a_login_redirect():
    client = TestClient(_full_app(_settings()))
    resp = client.get("/wp-login.php", follow_redirects=False)
    assert resp.status_code == 404
    assert DEFAULT_LOGIN_PATH not in resp.text


def test_login_path_is_configurable():
    client = TestClient(_full_app(_settings(login_path="/way-in")))

    assert client.get("/way-in").status_code == 200
    # The default path is not also served.
    assert client.get(DEFAULT_LOGIN_PATH, follow_redirects=False).status_code == 404
    # Redirects and the rendered form both point at the configured path.
    assert client.get("/", follow_redirects=False).headers["location"] == "/way-in"
    assert 'action="/way-in"' in client.get("/way-in").text
    resp = client.post("/way-in", data={"password": _PW}, follow_redirects=False)
    assert resp.status_code == 303


def test_dashboard_reports_configured_login_path():
    client = TestClient(_full_app(_settings(login_path="/way-in")))
    body = client.get("/", headers=_basic("admin", _PW)).text
    assert "LOGIN_PATH = '/way-in'" in body
    assert "__LOGIN_PATH__" not in body


def test_login_redirects_when_already_authenticated():
    settings = _settings()
    token = _make_ui_token(_team(settings), settings)
    client = TestClient(_full_app(settings))
    client.cookies.set(_AUTH_COOKIE, token)
    resp = client.get(DEFAULT_LOGIN_PATH, follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "/"


def test_logout_clears_cookie():
    client = TestClient(_full_app(_settings()))
    client.post(DEFAULT_LOGIN_PATH, data={"password": _PW})
    resp = client.post("/logout", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == DEFAULT_LOGIN_PATH
    set_cookie = resp.headers.get("set-cookie", "").lower()
    assert _AUTH_COOKIE in set_cookie
    assert "max-age=0" in set_cookie or "expires=" in set_cookie


def test_api_unauthenticated_is_401_without_basic_challenge():
    client = TestClient(_full_app(_settings()))
    resp = client.get("/api/license", follow_redirects=False)
    assert resp.status_code == 401
    assert "www-authenticate" not in {k.lower() for k in resp.headers}


def test_api_basic_auth_still_works(tmp_path):
    client = TestClient(_full_app(_settings(etc_dir=str(tmp_path / "conf"))))
    resp = client.get("/api/license", headers=_basic("admin", _PW))
    assert resp.status_code == 200
    assert resp.json()["license"]["type"] == TYPE_UNLICENSED


def test_api_license_activate_uses_settings_etc_dir(tmp_path, monkeypatch):
    calls = {}

    def install_license_from_text(key_text: str, etc_dir: str | None = None):
        calls["key_text"] = key_text
        calls["etc_dir"] = etc_dir
        return LicenseInfo(
            type=TYPE_INDIVIDUAL,
            name="Ada",
            email="ada@example.com",
        )

    monkeypatch.setattr(web_ui, "install_license_from_text", install_license_from_text)
    settings = _settings(etc_dir=str(tmp_path / "conf"))
    client = TestClient(_full_app(settings))

    resp = client.post(
        "/api/license/activate",
        headers=_basic("admin", _PW),
        json={"key": " fake-key "},
    )

    assert resp.status_code == 200
    assert resp.json()["license"]["name"] == "Ada"
    assert calls == {"key_text": "fake-key", "etc_dir": settings.etc_dir}


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
    assert resp.headers["location"] == DEFAULT_LOGIN_PATH


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


# --- production endpoints ---------------------------------------------------


def test_healthz_is_public():
    from unittest.mock import patch

    client = TestClient(_full_app(_settings()))
    with patch(
        "oduflow.health.collect_health", return_value={"ok": True, "checks": {}}
    ):
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_healthz_degraded_is_503():
    from unittest.mock import patch

    client = TestClient(_full_app(_settings()))
    with patch(
        "oduflow.health.collect_health", return_value={"ok": False, "checks": {}}
    ):
        resp = client.get("/healthz")
    assert resp.status_code == 503


def test_github_webhook_is_public_but_verifies_hmac():
    # No UI session: the route is reachable, but a bad signature is 401.
    client = TestClient(_full_app(_settings(prod_enabled=True)))
    resp = client.post(
        "/api/webhooks/github",
        content=b"{}",
        headers={
            "x-github-event": "push",
            "x-hub-signature-256": "sha256=deadbeef",
        },
    )
    assert resp.status_code == 401


def test_productions_api_requires_auth():
    client = TestClient(_full_app(_settings()))
    assert client.get("/api/productions").status_code == 401
    assert client.post("/api/productions/x/stop").status_code == 401
