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
        client_a = _run(provider.get_client("tok-a"))
        assert client_a is not None
        assert client_a.client_id == "tok-a"
        assert client_a.client_secret == "tok-a"

        client_b = _run(provider.get_client("tok-b"))
        assert client_b is not None
        assert client_b.client_secret == "tok-b"

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
        client = _run(provider.get_client("tok-a"))
        assert client is not None
        code = AuthorizationCode(
            code="dummy",
            client_id="tok-a",
            redirect_uri=AnyUrl("https://claude.ai/cb"),
            redirect_uri_provided_explicitly=True,
            scopes=["mcp"],
            expires_at=9999999999,
            code_challenge="abc",
        )
        token = _run(provider.exchange_authorization_code(client, code))
        assert token.access_token == "tok-a"
        assert token.refresh_token == "tok-a"
        assert token.token_type == "Bearer"

    def test_flexible_redirect_uri(self):
        from pydantic import AnyUrl

        provider = OduflowOAuthProvider(_settings())
        client = _run(provider.get_client("tok-a"))
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
        client = _run(provider.get_client("tok-a"))
        # Cleartext http to a non-loopback host would leak the auth code.
        with pytest.raises(InvalidRedirectUriError):
            client.validate_redirect_uri(AnyUrl("http://evil.example/cb"))

    def test_env_token_acts_as_client(self, monkeypatch):
        from oduflow import oauth_provider as op

        mapping = {"env-secret": ("2", "feature/x"), "tok-a": ("1", None)}
        monkeypatch.setattr(
            op.env_tokens,
            "resolve_token",
            lambda settings, token: mapping.get(token),
        )
        provider = OduflowOAuthProvider(_settings())

        # A per-env token resolves as its own OAuth client.
        client = _run(provider.get_client("env-secret"))
        assert client is not None
        assert client.client_id == "env-secret"
        assert client.client_secret == "env-secret"

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
        provider = OduflowOAuthProvider(_settings(oauth_base_url=""))
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
        app = HostRelativeAuthChallenge(inner)
        _run(app(scope, receive, send))

        start = next(m for m in sent if m["type"] == "http.response.start")
        hdr = dict(start["headers"])[b"www-authenticate"].decode()
        assert (
            'resource_metadata="https://team-b.example.com/.well-known/'
            'oauth-protected-resource/mcp"' in hdr
        )
        # The error parts are preserved untouched.
        assert 'error="invalid_token"' in hdr
