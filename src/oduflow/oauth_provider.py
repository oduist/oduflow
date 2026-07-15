"""Self-hosted OAuth 2.1 Authorization Server for Oduflow.

Each team is preregistered as an OAuth client whose ``client_id`` is a public,
non-secret identifier (``team_<id>``, e.g. ``team_1``) and whose
``client_secret`` is the team's ``auth_token``. Keeping the secret out of the
``client_id`` matters because ``client_id`` travels in the ``/authorize`` query
string (logs, browser history, Referer); the secret is sent only in the POST
``/token`` body. After a successful Authorization Code + PKCE flow the issued
``access_token`` is the team ``auth_token``, so the existing Bearer-token path in
``_resolve_team()`` keeps working unchanged.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from mcp.server.auth.json_response import PydanticJSONResponse
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    RefreshToken,
)
from mcp.server.auth.routes import build_metadata, cors_middleware
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import (
    InvalidRedirectUriError,
    OAuthClientInformationFull,
    OAuthToken,
    ProtectedResourceMetadata,
)
from pydantic import AnyHttpUrl, AnyUrl
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from oduflow import env_tokens
from oduflow.scoped_access import ENV_SCOPE_PREFIX
from oduflow.settings import Settings

logger = logging.getLogger("oduflow")


# Schemes that are never a legitimate OAuth callback and could turn the
# authorization redirect into an XSS / data-exfil primitive.
_DANGEROUS_REDIRECT_SCHEMES = {"javascript", "data", "vbscript", "file"}
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}

_RESOURCE_METADATA_RE = re.compile(r'resource_metadata="([^"]+)"')


def public_client_id(team_id: str) -> str:
    """The non-secret OAuth ``client_id`` for a team, safe to appear in the
    ``/authorize`` URL. The team's ``auth_token`` is the ``client_secret``."""
    return f"team_{team_id}"


def _forwarded_scheme_host(
    raw_headers: list[tuple[bytes, bytes]], fallback_scheme: str | None = None
) -> tuple[str, str]:
    """The ``(scheme, host)`` the client reached us on, from ASGI headers.

    Behind Traefik the app is reached over http with the team's public host in
    the Host header and ``X-Forwarded-Proto: https``; reflect the forwarded
    proto/host (first hop) so advertised OAuth URLs are reachable.
    """
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in raw_headers}
    fwd_host = headers.get("x-forwarded-host", "").split(",")[0].strip()
    host = fwd_host or headers.get("host", "")
    fwd_proto = headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    scheme = fwd_proto or fallback_scheme or "https"
    return scheme, host


def _unknown_host_response() -> JSONResponse:
    """400 for a discovery request whose host is not a registered team — never
    advertise an issuer derived from a forged or absent Host header."""
    return JSONResponse(
        {"error": "invalid_request", "error_description": "Unrecognized host."},
        status_code=400,
    )


def _rewrite_resource_metadata_origin(header_value: bytes, origin: str) -> bytes:
    """Rewrite the scheme+host of a ``WWW-Authenticate`` ``resource_metadata``
    URL to ``origin``, preserving its path."""
    text = header_value.decode("latin-1")
    m = _RESOURCE_METADATA_RE.search(text)
    if not m:
        return header_value
    old = m.group(1)
    new = f"{origin}{urlparse(old).path}"
    return text.replace(
        f'resource_metadata="{old}"', f'resource_metadata="{new}"'
    ).encode("latin-1")


class HostRelativeAuthChallenge:
    """ASGI wrapper that makes the 401 auth challenge host-relative.

    In host-relative OAuth mode the issuer is per-team-host, but fastmcp bakes a
    single static ``resource_metadata`` URL — built from the placeholder
    base_url — into RequireAuthMiddleware's ``WWW-Authenticate`` challenge. An
    unauthenticated MCP client (e.g. Claude.ai) begins OAuth discovery from that
    header, so without this rewrite team B's client would start OAuth on team A's
    hostname. We rewrite the URL's scheme+host to the host the request actually
    arrived on, keeping the path; the discovery it points to is already
    host-relative (see OduflowOAuthProvider.get_routes). The rewrite only fires
    for a host that maps to a registered team, so a forged X-Forwarded-Host can't
    inject an attacker origin into the challenge.
    """

    def __init__(self, app: Any, settings: Settings) -> None:
        self.app = app
        self._settings = settings

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        scheme, host = _forwarded_scheme_host(
            scope.get("headers", []), scope.get("scheme")
        )
        # Only rewrite for a host that belongs to a registered team; a forged or
        # unexpected host is left with fastmcp's own (placeholder team) URL.
        if self._settings.get_team_by_hostname(host) is None:
            await self.app(scope, receive, send)
            return
        origin = f"{scheme}://{host}"

        async def send_wrapper(message: Any) -> None:
            if message.get("type") == "http.response.start" and message.get("headers"):
                message = {
                    **message,
                    "headers": [
                        (k, _rewrite_resource_metadata_origin(v, origin))
                        if k.lower() == b"www-authenticate"
                        else (k, v)
                        for k, v in message["headers"]
                    ],
                }
            await send(message)

        await self.app(scope, receive, send_wrapper)


class _FlexibleClient(OAuthClientInformationFull):
    """Client info that accepts the varied callback URLs MCP clients use.

    Preregistered team clients have ``client_id = team_<id>`` (non-secret) and
    ``client_secret = auth_token``. The secret-bearing /token exchange proves
    identity, so we let MCP clients (claude.ai over https, IDEs over loopback or
    a custom scheme) bring their own callback — but we still reject callbacks that
    are never legitimate: dangerous schemes (javascript:, data:, …) and cleartext
    http:// to a non-loopback host (which would leak the authorization code in the
    clear).
    """

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        if redirect_uri is None:
            if self.redirect_uris:
                return self.redirect_uris[0]
            raise InvalidRedirectUriError("redirect_uri must be specified")

        scheme = (redirect_uri.scheme or "").lower()
        if not scheme or scheme in _DANGEROUS_REDIRECT_SCHEMES:
            raise InvalidRedirectUriError(
                f"redirect_uri scheme '{scheme}' is not allowed."
            )
        if scheme == "http" and (redirect_uri.host or "") not in _LOOPBACK_HOSTS:
            raise InvalidRedirectUriError(
                "Plaintext http:// redirect_uri is only allowed for loopback "
                "addresses; use https for remote callbacks."
            )
        return redirect_uri


class OduflowOAuthProvider(InMemoryOAuthProvider):
    """OAuth Authorization Server backed by ``team.auth_token`` values."""

    def __init__(self, settings: Settings) -> None:
        # Host-relative issuer: with no explicit oauth_base_url (the norm in
        # traefik mode) the issuer is derived per-request from the incoming Host,
        # so OAuth runs on each team's own TLS-terminated hostname. The
        # placeholder base_url below is a real team hostname that satisfies the
        # SDK's issuer validation for the path-only /authorize + /token routes;
        # it is kept out of every client-facing output by two overrides —
        # get_routes() rebuilds the discovery metadata per-request, and
        # HostRelativeAuthChallenge rewrites the 401 WWW-Authenticate
        # resource_metadata origin to the request host.
        self._host_relative = not settings.oauth_base_url
        if settings.oauth_base_url:
            base_url = settings.oauth_base_url
        else:
            placeholder_host = next(
                (t.hostname for t in settings.teams.values() if t.hostname),
                "localhost",
            )
            base_url = f"https://{placeholder_host}"
        super().__init__(base_url=base_url)

        self._settings = settings
        self._token_to_team: dict[str, str] = {}
        placeholder_redirect = AnyUrl("https://claude.ai/")

        for team in settings.teams.values():
            token = team.auth_token
            if not token:
                continue
            # Public, non-secret client_id (e.g. "team_1"): it travels in the
            # /authorize query string, so it must NOT be the secret. The secret
            # lives only in client_secret (POST /token body) and is what the
            # issued access_token equals.
            client_id = public_client_id(team.team_id)
            client = _FlexibleClient(
                client_id=client_id,
                client_secret=token,
                redirect_uris=[placeholder_redirect],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="client_secret_post",
                scope=None,
            )
            self.clients[client_id] = client
            self._token_to_team[client_id] = team.team_id
            # Preseed the access token — keyed by the SECRET auth_token, not the
            # public client_id — so direct Bearer-token calls also work (curl/CLI
            # clients that skip the OAuth dance). The public client_id is not a
            # valid Bearer token on its own.
            self.access_tokens[token] = AccessToken(
                token=token,
                client_id=team.team_id,
                scopes=[],
                expires_at=None,
            )

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        """OAuth routes, with discovery metadata made per-request when there is
        no fixed issuer.

        With no explicit ``oauth_base_url`` the SDK would bake the placeholder
        issuer into static ``/.well-known/...`` documents. We instead swap those
        two discovery endpoints for handlers that advertise the issuer at the
        host the client actually reached us on, so each team's OAuth flow runs on
        its own hostname. ``/authorize`` and ``/token`` are path-only and need no
        change.
        """
        routes = super().get_routes(mcp_path)
        if not self._host_relative:
            return routes

        patched: list[Route] = []
        for route in routes:
            if not isinstance(route, Route):
                patched.append(route)
            elif route.path.startswith("/.well-known/oauth-authorization-server"):
                patched.append(
                    Route(
                        route.path,
                        endpoint=cors_middleware(
                            self._authorization_server_metadata, ["GET", "OPTIONS"]
                        ),
                        methods=["GET", "OPTIONS"],
                    )
                )
            elif route.path.startswith("/.well-known/oauth-protected-resource"):
                patched.append(
                    Route(
                        route.path,
                        endpoint=cors_middleware(
                            self._protected_resource_metadata, ["GET", "OPTIONS"]
                        ),
                        methods=["GET", "OPTIONS"],
                    )
                )
            else:
                patched.append(route)
        return patched

    def _validated_origin(self, request: Request) -> str | None:
        """External origin (scheme://host) the client reached us on, but only for
        a host that belongs to a registered team; otherwise ``None`` so the caller
        rejects the request. This keeps a forged ``X-Forwarded-Host`` from
        advertising an attacker-controlled issuer, and turns a missing Host into a
        clean 400 rather than an invalid ``https://`` URL."""
        scheme, host = _forwarded_scheme_host(
            request.scope["headers"], request.scope.get("scheme")
        )
        if self._settings.get_team_by_hostname(host) is None:
            return None
        return f"{scheme}://{host}"

    async def _authorization_server_metadata(self, request: Request) -> Response:
        origin = self._validated_origin(request)
        if origin is None:
            return _unknown_host_response()
        issuer = AnyHttpUrl(origin)
        # Default the options exactly as the SDK's create_auth_routes does —
        # build_metadata dereferences them, and both are None on this provider.
        metadata = build_metadata(
            issuer,
            self.service_documentation_url,
            self.client_registration_options or ClientRegistrationOptions(),
            self.revocation_options or RevocationOptions(),
        )
        # No-store: the document is host-specific, so a shared cache must not
        # replay one team's issuer for another host.
        return PydanticJSONResponse(
            content=metadata, headers={"Cache-Control": "no-store"}
        )

    async def _protected_resource_metadata(self, request: Request) -> Response:
        origin = self._validated_origin(request)
        if origin is None:
            return _unknown_host_response()
        resource_path = (
            urlparse(str(self._resource_url)).path if self._resource_url else "/mcp"
        )
        supported_scopes = (
            self.client_registration_options.valid_scopes
            if (
                self.client_registration_options
                and self.client_registration_options.valid_scopes
            )
            else self.required_scopes
        )
        metadata = ProtectedResourceMetadata(
            resource=AnyHttpUrl(f"{origin}{resource_path}"),
            authorization_servers=[AnyHttpUrl(origin)],
            scopes_supported=supported_scopes,
        )
        return PydanticJSONResponse(
            content=metadata, headers={"Cache-Control": "no-store"}
        )

    async def _env_identity(self, token: str | None) -> tuple[str, str] | None:
        """Return ``(team_id, env_name)`` for a valid per-environment token.

        Team tokens are handled by the preseeded clients/access tokens, so they
        return ``None`` here.
        """
        if not token:
            return None
        resolved = await env_tokens.resolve_token_async(self._settings, token)
        if resolved is None:
            return None
        team_id, env_name = resolved
        if env_name is None:
            return None
        return (team_id, env_name)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await super().get_client(client_id)

    async def load_access_token(  # type: ignore[override]
        self, token: str
    ) -> AccessToken | None:
        existing = await super().load_access_token(token)
        if existing is not None:
            return existing
        identity = await self._env_identity(token)
        if identity is None:
            return None
        team_id, env_name = identity
        return AccessToken(
            token=token,
            client_id=team_id,
            scopes=[f"{ENV_SCOPE_PREFIX}{env_name}"],
            expires_at=None,
        )

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        raise ValueError(
            "Dynamic client registration is disabled. Use the preregistered "
            "client: client_id = team_<id> (e.g. team_1), client_secret = that "
            "team's auth_token from oduflow.toml."
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        # Single-use code.
        self.auth_codes.pop(authorization_code.code, None)

        cid = client.client_id
        if cid is None or cid not in self._token_to_team:
            from mcp.server.auth.provider import TokenError

            raise TokenError("invalid_client", "Unknown client.")

        # The issued access token is the client SECRET (the team auth_token, or
        # never the public client_id, which would not authenticate as a Bearer
        # token downstream.
        token = client.client_secret or cid
        scope = (
            " ".join(authorization_code.scopes) if authorization_code.scopes else None
        )
        return OAuthToken(
            access_token=token,
            token_type="Bearer",
            expires_in=None,
            refresh_token=token,
            scope=scope,
        )

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> RefreshToken | None:
        cid = client.client_id
        # The refresh token we issue equals the client secret (team auth_token),
        # not the public client_id.
        if (
            cid is not None
            and refresh_token == client.client_secret
            and cid in self._token_to_team
        ):
            return RefreshToken(
                token=refresh_token,
                client_id=cid,
                scopes=[],
                expires_at=None,
            )
        return None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        token = refresh_token.token
        return OAuthToken(
            access_token=token,
            token_type="Bearer",
            expires_in=None,
            refresh_token=token,
            scope=" ".join(scopes) if scopes else None,
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # Tokens are tied to oduflow.toml; runtime revocation would just
        # break the next request. Operator should rotate auth_token instead.
        return None
