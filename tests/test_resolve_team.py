"""Team resolution: strict in HTTP mode, permissive fallback in stdio."""

from types import SimpleNamespace

import pytest

import oduflow.server as server
from oduflow import settings as settings_module
from oduflow.errors import NotFoundError
from oduflow.settings import Settings, TeamSettings


def _settings(n_teams: int = 2, allow_insecure_http: bool = False) -> Settings:
    teams = {
        str(i): TeamSettings(
            team_id=str(i),
            hostname=f"team{i}.example.com",
            auth_token=f"token-{i}",
            port_range_start=50000 + i * 100,
            port_range_end=50100 + i * 100,
        )
        for i in range(1, n_teams + 1)
    }
    return Settings(teams=teams, allow_insecure_http=allow_insecure_http)


@pytest.fixture
def inject(monkeypatch):
    def _inject(settings: Settings, transport: str) -> None:
        monkeypatch.setattr(server, "_settings", settings)
        monkeypatch.setattr(settings_module, "TRANSPORT", transport)

    yield _inject
    server._settings = None


def test_stdio_falls_back_to_default_team(inject):
    inject(_settings(n_teams=2), "stdio")
    assert server._resolve_team(None).team_id == "1"


def test_http_refuses_unresolved_request(inject):
    inject(_settings(n_teams=2), "http")
    with pytest.raises(NotFoundError, match="Cannot resolve a team"):
        server._resolve_team(None)


def test_http_refuses_even_with_single_team(inject):
    # A hosted single-client server must not serve requests that carry no
    # resolvable identity either.
    inject(_settings(n_teams=1), "http")
    with pytest.raises(NotFoundError, match="Cannot resolve a team"):
        server._resolve_team(None)


def test_http_insecure_optout_keeps_fallback(inject):
    inject(_settings(n_teams=1, allow_insecure_http=True), "http")
    assert server._resolve_team(None).team_id == "1"


def test_http_token_resolution_still_works(inject):
    inject(_settings(n_teams=2), "http")
    ctx = SimpleNamespace(client_id="2")
    assert server._resolve_team(ctx).team_id == "2"
