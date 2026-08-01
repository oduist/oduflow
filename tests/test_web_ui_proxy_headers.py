"""Integration tests across Uvicorn's ProxyHeadersMiddleware and the login throttle.

The client IP that the throttle keys on is decided by two layers:

1. ``uvicorn.middleware.proxy_headers.ProxyHeadersMiddleware`` rewrites
   ``scope["client"]`` from ``X-Forwarded-For`` when the TCP peer is in the
   trust list Oduflow computes in ``server._forwarded_allow_ips``;
2. ``web_ui._client_ip`` resolves the same thing at the app layer for whatever
   the first layer left alone.

Unit tests cover each half. These tests wire the *real* Uvicorn middleware in
front of the *real* dashboard app, so a regression in either the trust list or
its composition with the app layer shows up as an actual spoofed login.

The key property: a request from localhost carrying a forged ``X-Forwarded-For``
must be throttled as localhost by default — Uvicorn's own default would have
trusted ``127.0.0.1`` (or ``FORWARDED_ALLOW_IPS``) and believed the header.
"""

from __future__ import annotations

import tempfile

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from oduflow.locking import LockManager
from oduflow.server import _forwarded_allow_ips
from oduflow.settings import DEFAULT_LOGIN_PATH, Settings, TeamSettings
from oduflow.web_ui import _AUTH_COOKIE, mount_web_ui

_PW = "s3cret"
_DATA_DIR = tempfile.mkdtemp(prefix="oduflow-proxyhdr-test-")
_TRAEFIK_SUBNET = "172.18.0.0/16"
_TRAEFIK_IP = "172.18.0.2"


def _settings(routing_mode: str = "port", trusted_proxies: tuple[str, ...] = ()):
    return Settings(
        routing_mode=routing_mode,
        acme_email="a@b.co",
        trusted_proxies=trusted_proxies,
        base_data_dir=_DATA_DIR,
        teams={
            "1": TeamSettings(team_id="1", hostname="dev.example.com", ui_password=_PW)
        },
    )


@pytest.fixture
def fake_docker_network(monkeypatch):
    """Traefik's Docker network, as ``_traefik_forwarded_allow_ips`` reads it."""

    class _Net:
        attrs = {"IPAM": {"Config": [{"Subnet": _TRAEFIK_SUBNET}]}}

        def reload(self):
            pass

    class _Client:
        networks = type("N", (), {"get": staticmethod(lambda name: _Net())})()

    monkeypatch.setattr("oduflow.docker_ops.client.get_client", lambda: _Client())


def _stack(settings: Settings, peer: str) -> TestClient:
    """The dashboard behind the real Uvicorn proxy-headers middleware."""
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=_forwarded_allow_ips(settings))
    return TestClient(wrapped, client=(peer, 44444))


def _exhaust(client: TestClient, headers: dict[str, str]) -> None:
    """Burn the 10-failure per-IP budget for whatever IP this resolves to."""
    for _ in range(10):
        resp = client.post(
            DEFAULT_LOGIN_PATH, data={"password": "nope"}, headers=headers
        )
        assert resp.status_code == 401


def _login(client: TestClient, headers: dict[str, str]):
    return client.post(
        DEFAULT_LOGIN_PATH,
        data={"password": _PW},
        headers=headers,
        follow_redirects=False,
    )


# --- default: nothing is trusted -----------------------------------------


def test_localhost_spoofed_xff_is_ignored_by_default():
    """A local process rotating X-Forwarded-For must not escape the throttle.

    This is exactly the case Uvicorn's default (``127.0.0.1``) would have let
    through, which is why _forwarded_allow_ips never returns None.
    """
    settings = _settings()
    assert _forwarded_allow_ips(settings) == []
    client = _stack(settings, "127.0.0.1")

    for i in range(10):
        resp = client.post(
            DEFAULT_LOGIN_PATH,
            data={"password": "nope"},
            headers={"X-Forwarded-For": f"203.0.113.{i}"},
        )
        assert resp.status_code == 401
    # A fresh forged address does not buy a fresh bucket.
    resp = _login(client, {"X-Forwarded-For": "203.0.113.99"})
    assert resp.status_code == 429
    assert _AUTH_COOKIE not in resp.headers.get("set-cookie", "")


def test_forwarded_allow_ips_env_var_is_not_inherited(monkeypatch):
    """FORWARDED_ALLOW_IPS must not silently widen trust behind our back."""
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    settings = _settings()
    assert _forwarded_allow_ips(settings) == []
    client = _stack(settings, "127.0.0.1")
    _exhaust(client, {"X-Forwarded-For": "203.0.113.5"})
    assert _login(client, {"X-Forwarded-For": "198.51.100.1"}).status_code == 429


def test_remote_peer_spoofed_xff_is_ignored_by_default():
    settings = _settings()
    client = _stack(settings, "198.51.100.7")
    _exhaust(client, {"X-Forwarded-For": "203.0.113.5"})
    assert _login(client, {"X-Forwarded-For": "203.0.113.99"}).status_code == 429


# --- loopback becomes effective only when configured ----------------------


def test_loopback_is_honoured_once_explicitly_configured():
    """Docker Desktop NATs container->host traffic to 127.0.0.1, so operators
    there opt in; then (and only then) the header decides the bucket."""
    settings = _settings(trusted_proxies=("127.0.0.1",))
    assert _forwarded_allow_ips(settings) == ["127.0.0.1"]
    client = _stack(settings, "127.0.0.1")

    _exhaust(client, {"X-Forwarded-For": "203.0.113.5"})
    # The exhausted bucket belongs to the forwarded address...
    assert _login(client, {"X-Forwarded-For": "203.0.113.5"}).status_code == 429
    # ...and a different real client behind the same proxy is unaffected.
    assert _login(client, {"X-Forwarded-For": "203.0.113.6"}).status_code == 303


def test_configured_loopback_does_not_extend_to_other_peers():
    """Trusting 127.0.0.1 must not trust the rest of the host's network."""
    settings = _settings(trusted_proxies=("127.0.0.1",))
    client = _stack(settings, "198.51.100.7")
    _exhaust(client, {"X-Forwarded-For": "203.0.113.5"})
    assert _login(client, {"X-Forwarded-For": "203.0.113.99"}).status_code == 429


# --- traefik mode ---------------------------------------------------------


def test_traefik_chain_resolves_to_the_real_client(fake_docker_network):
    settings = _settings("traefik")
    assert _forwarded_allow_ips(settings) == [_TRAEFIK_SUBNET]
    client = _stack(settings, _TRAEFIK_IP)

    _exhaust(client, {"X-Forwarded-For": "203.0.113.5"})
    assert _login(client, {"X-Forwarded-For": "203.0.113.5"}).status_code == 429
    # Independent clients behind Traefik keep independent buckets.
    assert _login(client, {"X-Forwarded-For": "203.0.113.6"}).status_code == 303


def test_traefik_chain_ignores_client_prepended_hops(fake_docker_network):
    """Only what the trusted chain appended counts; junk on the left is inert."""
    settings = _settings("traefik")
    client = _stack(settings, _TRAEFIK_IP)
    for i in range(10):
        resp = client.post(
            DEFAULT_LOGIN_PATH,
            data={"password": "nope"},
            headers={"X-Forwarded-For": f"192.0.2.{i}, 203.0.113.5"},
        )
        assert resp.status_code == 401
    resp = _login(client, {"X-Forwarded-For": "192.0.2.99, 203.0.113.5"})
    assert resp.status_code == 429


def test_traefik_mode_does_not_trust_loopback_implicitly(fake_docker_network):
    """A local process on a Traefik host is not a proxy: no implicit grant."""
    settings = _settings("traefik")
    assert "127.0.0.1" not in _forwarded_allow_ips(settings)
    client = _stack(settings, "127.0.0.1")
    _exhaust(client, {"X-Forwarded-For": "203.0.113.5"})
    assert _login(client, {"X-Forwarded-For": "203.0.113.99"}).status_code == 429


def test_traefik_mode_unions_configured_proxies(fake_docker_network):
    settings = _settings("traefik", trusted_proxies=("127.0.0.1",))
    assert _forwarded_allow_ips(settings) == ["127.0.0.1", _TRAEFIK_SUBNET]
    client = _stack(settings, "127.0.0.1")
    _exhaust(client, {"X-Forwarded-For": "203.0.113.5"})
    assert _login(client, {"X-Forwarded-For": "203.0.113.6"}).status_code == 303
