"""Unit tests for the self-hosted OAuth Authorization Server."""

from __future__ import annotations

import asyncio
import json

import pytest

from oduflow.oauth_provider import OduflowOAuthProvider
from oduflow.settings import Settings, TeamSettings


def _settings(**overrides):
    teams = overrides.pop(
        "teams",
        {
            "1": TeamSettings(
                team_id="1",
                auth_token="tok-a",
                port_range_start=50000,
                port_range_end=50100,
            ),
            "2": TeamSettings(
                team_id="2",
                auth_token="tok-b",
                port_range_start=50100,
                port_range_end=50200,
            ),
        },
    )
    return Settings(
        oauth_base_url=overrides.pop("oauth_base_url", "https://oduflow.example.com"),
        teams=teams,
        **overrides,
    )


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _host_settings(*hostnames):
    """Host-relative Settings (no oauth_base_url) with a team per hostname."""
    return Settings(
        oauth_base_url="",
        teams={
            str(i): TeamSettings(
                team_id=str(i),
                auth_token=f"tok-{i}",
                hostname=h,
                port_range_start=50000 + i * 100,
                port_range_end=50100 + i * 100,
            )
            for i, h in enumerate(hostnames, start=1)
        },
    )


class TestOduflowOAuthProvider:
    def test_host_relative_without_oauth_base_url(self):
        # No oauth_base_url (the traefik norm): the issuer is derived per-request
        # from the incoming Host, so construction is allowed (host-relative mode)
        # and uses a team hostname as the placeholder base_url.
        s = Settings(
            routing_mode="traefik",
            acme_email="admin@example.com",
            oauth_base_url="",
            teams={
                "1": TeamSettings(
                    team_id="1",
                    auth_token="tok",
                    hostname="team1.example.com",
                    port_range_start=50000,
                    port_range_end=50100,
                )
            },
        )
        provider = OduflowOAuthProvider(s)
        assert provider._host_relative is True

    def test_preregistered_clients(self):
        provider = OduflowOAuthProvider(_settings())
        # client_id is the public, non-secret team identifier; the secret is the
        # team's auth_token.
        client_a = _run(provider.get_client("team_1"))
        assert client_a is not None
        assert client_a.client_id == "team_1"
        assert client_a.client_secret == "tok-a"

        client_b = _run(provider.get_client("team_2"))
        assert client_b is not None
        assert client_b.client_id == "team_2"
        assert client_b.client_secret == "tok-b"

        # The secret must NOT double as a client_id — otherwise it would leak
        # into the /authorize URL.
        assert _run(provider.get_client("tok-a")) is None
        assert _run(provider.get_client("unknown")) is None

    def test_register_client_disabled(self):
        from mcp.shared.auth import OAuthClientInformationFull
        from pydantic import AnyUrl

        provider = OduflowOAuthProvider(_settings())
        new_client = OAuthClientInformationFull(
            client_id="new",
            client_secret="new",
            redirect_uris=[AnyUrl("https://example.com/cb")],
        )
        with pytest.raises(ValueError, match="Dynamic client registration"):
            _run(provider.register_client(new_client))

    def test_verify_token(self):
        provider = OduflowOAuthProvider(_settings())
        access = _run(provider.verify_token("tok-a"))
        assert access is not None
        assert access.token == "tok-a"
        assert access.client_id == "1"
        assert access.expires_at is None

        access_b = _run(provider.verify_token("tok-b"))
        assert access_b is not None
        assert access_b.client_id == "2"

        assert _run(provider.verify_token("nope")) is None

    def test_exchange_authorization_code_returns_auth_token(self):
        from mcp.server.auth.provider import AuthorizationCode
        from pydantic import AnyUrl

        provider = OduflowOAuthProvider(_settings())
        client = _run(provider.get_client("team_1"))
        assert client is not None
        code = AuthorizationCode(
            code="dummy",
            client_id="team_1",
            redirect_uri=AnyUrl("https://claude.ai/cb"),
            redirect_uri_provided_explicitly=True,
            scopes=["mcp"],
            expires_at=9999999999,
            code_challenge="abc",
        )
        token = _run(provider.exchange_authorization_code(client, code))
        # The issued access/refresh token is the SECRET auth_token, not the
        # public client_id.
        assert token.access_token == "tok-a"
        assert token.refresh_token == "tok-a"
        assert token.token_type == "Bearer"

    def test_bearer_invariant_and_refresh(self):
        provider = OduflowOAuthProvider(_settings())
        # The auth_token (the issued access token) still resolves to the team id
        # via the preseeded, secret-keyed access token — the Bearer path is
        # unchanged by the client_id/secret split.
        access = _run(provider.load_access_token("tok-a"))
        assert access is not None
        assert access.client_id == "1"
        # The public client_id is NOT a valid Bearer/access token on its own.
        assert _run(provider.load_access_token("team_1")) is None
        # Refresh token equals the secret (auth_token), not the client_id.
        client = _run(provider.get_client("team_1"))
        assert client is not None
        rt = _run(provider.load_refresh_token(client, "tok-a"))
        assert rt is not None
        assert rt.client_id == "team_1"
        assert _run(provider.load_refresh_token(client, "team_1")) is None

    def test_flexible_redirect_uri(self):
        from pydantic import AnyUrl

        provider = OduflowOAuthProvider(_settings())
        client = _run(provider.get_client("team_1"))
        assert client is not None
        # Legitimate MCP callbacks are accepted: https (claude.ai) and loopback
        # http (IDEs).
        assert (
            str(client.validate_redirect_uri(AnyUrl("https://claude.ai/some/cb")))
            == "https://claude.ai/some/cb"
        )
        assert client.validate_redirect_uri(AnyUrl("http://127.0.0.1:8976/cb"))

    def test_redirect_uri_rejects_dangerous_and_cleartext(self):
        from pydantic import AnyUrl
        from mcp.shared.auth import InvalidRedirectUriError

        provider = OduflowOAuthProvider(_settings())
        client = _run(provider.get_client("team_1"))
        # Cleartext http to a non-loopback host would leak the auth code.
        with pytest.raises(InvalidRedirectUriError):
            client.validate_redirect_uri(AnyUrl("http://evil.example/cb"))

    def test_env_token_is_bearer_only(self, monkeypatch):
        from oduflow import oauth_provider as op

        mapping = {"env-secret": ("2", "feature/x"), "tok-a": ("1", None)}
        monkeypatch.setattr(
            op.env_tokens,
            "resolve_token",
            lambda settings, token: mapping.get(token),
        )
        provider = OduflowOAuthProvider(_settings())

        # A per-env token must not be accepted as an OAuth client_id because it
        # would put the secret in the /authorize URL. Scoped endpoints are
        # Bearer-only.
        assert _run(provider.get_client("env-secret")) is None

        # Its access token carries the team_id and an env-binding scope.
        access = _run(provider.verify_token("env-secret"))
        assert access is not None
        assert access.client_id == "2"
        assert access.scopes == ["oduflow_env:feature/x"]

        # Team tokens still resolve via the preseeded path (no env scope).
        team_access = _run(provider.verify_token("tok-a"))
        assert team_access is not None
        assert team_access.client_id == "1"
        assert team_access.scopes == []

    def test_well_known_route_exposed(self):
        provider = OduflowOAuthProvider(_settings())
        routes = provider.get_routes("/mcp")
        paths = [getattr(r, "path", "") for r in routes]
        assert "/.well-known/oauth-authorization-server" in paths
        assert "/authorize" in paths
        assert "/token" in paths

    def test_metadata_via_test_client(self):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        provider = OduflowOAuthProvider(_settings())
        app = Starlette(routes=provider.get_routes("/mcp"))
        client = TestClient(app)
        resp = client.get("/.well-known/oauth-authorization-server")
        assert resp.status_code == 200
        meta = json.loads(resp.text)
        assert meta["issuer"].rstrip("/") == "https://oduflow.example.com"
        assert "authorization_endpoint" in meta
        assert "token_endpoint" in meta
        # DCR is disabled — endpoint must not be advertised.
        assert "registration_endpoint" not in meta

    def test_metadata_host_relative(self):
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        # No fixed issuer → issuer/endpoints derived per-request from the Host.
        provider = OduflowOAuthProvider(
            _host_settings("zipfit.oduflow.dev", "other.example.com")
        )
        assert provider._host_relative is True
        app = Starlette(routes=provider.get_routes("/mcp"))
        client = TestClient(app)

        headers = {"host": "zipfit.oduflow.dev", "x-forwarded-proto": "https"}
        meta = json.loads(
            client.get("/.well-known/oauth-authorization-server", headers=headers).text
        )
        assert meta["issuer"].rstrip("/") == "https://zipfit.oduflow.dev"
        assert meta["authorization_endpoint"] == "https://zipfit.oduflow.dev/authorize"
        assert meta["token_endpoint"] == "https://zipfit.oduflow.dev/token"

        prm = json.loads(
            client.get(
                "/.well-known/oauth-protected-resource/mcp", headers=headers
            ).text
        )
        assert prm["resource"].rstrip("/") == "https://zipfit.oduflow.dev/mcp"
        assert (
            prm["authorization_servers"][0].rstrip("/") == "https://zipfit.oduflow.dev"
        )

        # A different Host yields a different issuer — OAuth is per-team-host.
        other = json.loads(
            client.get(
                "/.well-known/oauth-authorization-server",
                headers={"host": "other.example.com", "x-forwarded-proto": "https"},
            ).text
        )
        assert other["issuer"].rstrip("/") == "https://other.example.com"

    def test_auth_challenge_rewrites_resource_metadata_host(self):
        # The 401 WWW-Authenticate resource_metadata origin must follow the
        # request host, or fastmcp's static URL would point team B's client at
        # team A's hostname to discover OAuth.
        from oduflow.oauth_provider import HostRelativeAuthChallenge

        static = (
            'Bearer error="invalid_token", error_description="Authentication '
            'required", resource_metadata="https://team-a.example.com/'
            '.well-known/oauth-protected-resource/mcp"'
        )

        async def inner(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"www-authenticate", static.encode())],
                }
            )
            await send({"type": "http.response.body", "body": b""})

        sent: list = []

        async def send(msg):
            sent.append(msg)

        async def receive():
            return {"type": "http.request"}

        scope = {
            "type": "http",
            "scheme": "http",  # Traefik terminates TLS; app sees http
            "headers": [
                (b"host", b"team-b.example.com"),
                (b"x-forwarded-proto", b"https"),
            ],
        }
        app = HostRelativeAuthChallenge(inner, _host_settings("team-b.example.com"))
        _run(app(scope, receive, send))

        start = next(m for m in sent if m["type"] == "http.response.start")
        hdr = dict(start["headers"])[b"www-authenticate"].decode()
        assert (
            'resource_metadata="https://team-b.example.com/.well-known/'
            'oauth-protected-resource/mcp"' in hdr
        )
        # The error parts are preserved untouched.
        assert 'error="invalid_token"' in hdr

    def test_metadata_rejects_unknown_host(self):
        # A forged/absent host must not be reflected as the issuer: the discovery
        # endpoints reject it with 400 rather than advertise an attacker origin
        # or blow up on an invalid https:// URL.
        from starlette.applications import Starlette
        from starlette.testclient import TestClient

        provider = OduflowOAuthProvider(_host_settings("zipfit.oduflow.dev"))
        client = TestClient(Starlette(routes=provider.get_routes("/mcp")))

        for path in (
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-protected-resource/mcp",
        ):
            r = client.get(
                path, headers={"host": "evil.com", "x-forwarded-proto": "https"}
            )
            assert r.status_code == 400, path

    def test_auth_challenge_skips_unknown_host(self):
        # A forged host must not be injected into the 401 challenge; the static
        # (placeholder team) resource_metadata is left untouched.
        from oduflow.oauth_provider import HostRelativeAuthChallenge

        static = (
            'Bearer error="invalid_token", resource_metadata='
            '"https://team-a.example.com/.well-known/oauth-protected-resource/mcp"'
        )

        async def inner(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"www-authenticate", static.encode())],
                }
            )
            await send({"type": "http.response.body", "body": b""})

        sent: list = []

        async def send(msg):
            sent.append(msg)

        async def receive():
            return {"type": "http.request"}

        scope = {
            "type": "http",
            "scheme": "http",
            "headers": [(b"host", b"evil.com"), (b"x-forwarded-proto", b"https")],
        }
        app = HostRelativeAuthChallenge(inner, _host_settings("team-b.example.com"))
        _run(app(scope, receive, send))

        start = next(m for m in sent if m["type"] == "http.response.start")
        hdr = dict(start["headers"])[b"www-authenticate"].decode()
        assert "team-a.example.com" in hdr  # unchanged
        assert "evil.com" not in hdr
