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

from oduflow.settings import Settings

logger = logging.getLogger("oduflow")


class _FlexibleClient(OAuthClientInformationFull):
    """Client info that accepts any redirect_uri at /authorize time.

    Preregistered clients have ``client_id == client_secret == auth_token``;
    the secret-bearing /token exchange already proves identity, so we let
    MCP clients (claude.ai, IDEs, etc.) bring whatever callback URL they use.
    """

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        if redirect_uri is not None:
            return redirect_uri
        if self.redirect_uris:
            return self.redirect_uris[0]
        raise InvalidRedirectUriError("redirect_uri must be specified")


class OduflowOAuthProvider(InMemoryOAuthProvider):
    """OAuth Authorization Server backed by ``team.auth_token`` values."""

    def __init__(self, settings: Settings) -> None:
        if not settings.oauth_base_url:
            raise ValueError("oauth_base_url is required for self-hosted OAuth")
        super().__init__(base_url=settings.oauth_base_url)

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

        if client.client_id is None or client.client_id not in self._token_to_team:
            from mcp.server.auth.provider import TokenError

            raise TokenError("invalid_client", "Unknown client.")

        token = client.client_id
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
        if (
            client.client_id is not None
            and refresh_token == client.client_id
            and refresh_token in self._token_to_team
        ):
            return RefreshToken(
                token=refresh_token,
                client_id=client.client_id,
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
