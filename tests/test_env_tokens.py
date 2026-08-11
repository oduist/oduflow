"""Unit tests for per-environment access token resolution."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from oduflow import env_tokens
from oduflow.settings import Settings, TeamSettings


def _settings() -> Settings:
    return Settings(
        teams={
            "1": TeamSettings(
                team_id="1",
                auth_token="team-tok-1",
                port_range_start=50000,
                port_range_end=50100,
            ),
            "2": TeamSettings(
                team_id="2",
                auth_token="team-tok-2",
                port_range_start=50100,
                port_range_end=50200,
            ),
        }
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    env_tokens.invalidate_cache()
    yield
    env_tokens.invalidate_cache()


def test_generate_token_unique():
    a = env_tokens.generate_token()
    b = env_tokens.generate_token()
    assert a != b
    assert len(a) >= 20


def test_resolve_team_token():
    s = _settings()
    assert env_tokens.resolve_token(s, "team-tok-1") == ("1", None)
    assert env_tokens.resolve_token(s, "team-tok-2") == ("2", None)


def test_resolve_env_token(monkeypatch):
    s = _settings()
    monkeypatch.setattr(
        env_tokens,
        "_scan_env_tokens",
        lambda settings: {"env-secret": ("2", "feature/x")},
    )
    assert env_tokens.resolve_token(s, "env-secret") == ("2", "feature/x")


def test_resolve_unknown(monkeypatch):
    s = _settings()
    monkeypatch.setattr(env_tokens, "_scan_env_tokens", lambda settings: {})
    assert env_tokens.resolve_token(s, "nope") is None
    assert env_tokens.resolve_token(s, "") is None


def test_team_token_wins_over_scan(monkeypatch):
    s = _settings()
    # Even if a scan would also report this token, the team match short-circuits
    # before any Docker scan happens.
    called = {"n": 0}

    def fake_scan(settings):
        called["n"] += 1
        return {}

    monkeypatch.setattr(env_tokens, "_scan_env_tokens", fake_scan)
    assert env_tokens.resolve_token(s, "team-tok-1") == ("1", None)
    assert called["n"] == 0


def test_env_token_cached(monkeypatch):
    s = _settings()
    calls = {"n": 0}

    def fake_scan(settings):
        calls["n"] += 1
        return {"env-secret": ("1", "main")}

    monkeypatch.setattr(env_tokens, "_scan_env_tokens", fake_scan)
    assert env_tokens.resolve_token(s, "env-secret") == ("1", "main")
    assert env_tokens.resolve_token(s, "env-secret") == ("1", "main")
    assert calls["n"] == 1  # second lookup served from the cache


def test_concurrent_unknown_tokens_share_one_scan(monkeypatch):
    s = _settings()
    scan_started = threading.Event()
    release_scan = threading.Event()
    calls = {"n": 0}

    def fake_scan(settings):
        calls["n"] += 1
        scan_started.set()
        assert release_scan.wait(timeout=2)
        return {"env-secret": ("1", "main")}

    monkeypatch.setattr(env_tokens, "_scan_env_tokens", fake_scan)
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(env_tokens.resolve_token, s, "env-secret") for _ in range(8)
        ]
        assert scan_started.wait(timeout=2)
        release_scan.set()
        results = [future.result(timeout=2) for future in futures]

    assert results == [("1", "main")] * 8
    assert calls["n"] == 1
