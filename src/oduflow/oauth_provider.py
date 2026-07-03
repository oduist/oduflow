"""Self-hosted OAuth 2.1 Authorization Server for Oduflow.

Each team's ``auth_token`` is preregistered as an OAuth client where
``client_id == client_secret == auth_token``. After a successful
Authorization Code + PKCE flow the issued ``access_token`` is the same
``auth_token``, so the existing Bearer-token path in ``_resolve_team()``
keeps working unchanged.
"""

from __future__ import annotations

import logging

from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    RefreshToken,
)
from mcp.shared.auth import (
    InvalidRedirectUriError,
    OAuthClientInformationFull,
    OAuthToken,
)
from pydantic import AnyUrl

from oduflow import env_tokens
from oduflow.scoped_access import ENV_SCOPE_PREFIX
from oduflow.settings import Settings

logger = logging.getLogger("oduflow")


# Schemes that are never a legitimate OAuth callback and could turn the
# authorization redirect into an XSS / data-exfil primitive.
_DANGEROUS_REDIRECT_SCHEMES = {"javascript", "data", "vbscript", "file"}
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}


class _FlexibleClient(OAuthClientInformationFull):
    """Client info that accepts the varied callback URLs MCP clients use.

    Preregistered clients have ``client_id == client_secret == auth_token``;
    the secret-bearing /token exchange already proves identity, so we let MCP
    clients (claude.ai over https, IDEs over loopback or a custom scheme) bring
    their own callback — but we still reject callbacks that are never legitimate:
    dangerous schemes (javascript:, data:, …) and cleartext http:// to a
    non-loopback host (which would leak the authorization code in the clear).
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
        if not settings.oauth_base_url:
            raise ValueError("oauth_base_url is required for self-hosted OAuth")
        super().__init__(base_url=settings.oauth_base_url)

        self._settings = settings
        self._token_to_team: dict[str, str] = {}
        placeholder_redirect = AnyUrl("https://claude.ai/")

        for team in settings.teams.values():
            token = team.auth_token
            if not token:
                continue
            client = _FlexibleClient(
                client_id=token,
                client_secret=token,
                redirect_uris=[placeholder_redirect],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="client_secret_post",
                scope=None,
            )
            self.clients[token] = client
            self._token_to_team[token] = team.team_id
            # Preseed the access token so direct Bearer-token calls also work
            # (curl/CLI clients that skip the OAuth dance entirely).
            self.access_tokens[token] = AccessToken(
                token=token,
                client_id=team.team_id,
                scopes=[],
                expires_at=None,
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
        client = await super().get_client(client_id)
        if client is not None:
            return client
        # A per-environment token acts as its own OAuth client.
        if await self._env_identity(client_id) is not None:
            return _FlexibleClient(
                client_id=client_id,
                client_secret=client_id,
                redirect_uris=[AnyUrl("https://claude.ai/")],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="client_secret_post",
                scope=None,
            )
        return None

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
            "Dynamic client registration is disabled. "
            "Set client_id = client_secret = team.<id>.auth_token from oduflow.toml."
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AuthorizationCode,
    ) -> OAuthToken:
        # Single-use code.
        self.auth_codes.pop(authorization_code.code, None)

        cid = client.client_id
        if cid is None or (
            cid not in self._token_to_team and await self._env_identity(cid) is None
        ):
            from mcp.server.auth.provider import TokenError

            raise TokenError("invalid_client", "Unknown client.")

        token = cid
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
        if (
            cid is not None
            and refresh_token == cid
            and (
                cid in self._token_to_team or await self._env_identity(cid) is not None
            )
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
