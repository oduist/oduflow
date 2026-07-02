"""Unit tests for the short-lived Odoo.sh import tokens."""

import os

import pytest

from oduflow import import_tokens
from oduflow.errors import NotFoundError, PrerequisiteNotMetError
from oduflow.settings import Settings, TeamSettings


def _team(tmp_path):
    return TeamSettings(team_id="1", data_dir=str(tmp_path))


def _settings(team):
    return Settings(teams={team.team_id: team})


def test_create_and_load_roundtrip(tmp_path):
    team = _team(tmp_path)
    settings = _settings(team)
    rec = import_tokens.create_token(team, "zipfit")
    assert rec["template_name"] == "zipfit"
    team2, rec2 = import_tokens.load_token(settings, rec["token"])
    assert team2.team_id == "1"
    assert rec2["token"] == rec["token"]
    assert rec2["template_name"] == "zipfit"


def test_malformed_and_unknown_tokens_raise_not_found(tmp_path):
    settings = _settings(_team(tmp_path))
    with pytest.raises(NotFoundError):
        import_tokens.load_token(settings, "")
    with pytest.raises(NotFoundError):
        import_tokens.load_token(settings, "has spaces!")
    # Well-formed shape but never issued.
    with pytest.raises(NotFoundError):
        import_tokens.load_token(settings, "a" * 24)


def test_expired_token_is_rejected_and_removed(tmp_path):
    team = _team(tmp_path)
    settings = _settings(team)
    rec = import_tokens.create_token(team, "tpl", ttl_seconds=1, now=1000.0)
    with pytest.raises(PrerequisiteNotMetError):
        import_tokens.load_token(settings, rec["token"], now=1002.0)
    # The expired token file is deleted, so a second look is a plain NotFound.
    with pytest.raises(NotFoundError):
        import_tokens.load_token(settings, rec["token"], now=1002.0)


def test_invalidate_deletes_token(tmp_path):
    team = _team(tmp_path)
    settings = _settings(team)
    rec = import_tokens.create_token(team, "tpl")
    import_tokens.invalidate(team, rec["token"])
    with pytest.raises(NotFoundError):
        import_tokens.load_token(settings, rec["token"])


def test_create_reaps_expired_tokens(tmp_path):
    team = _team(tmp_path)
    old = import_tokens.create_token(team, "old", ttl_seconds=1, now=1000.0)
    # A later mint triggers cleanup of the now-expired token file.
    import_tokens.create_token(team, "new", now=5000.0)
    assert not os.path.exists(import_tokens._token_path(team, old["token"]))


def test_invalid_template_name_rejected(tmp_path):
    team = _team(tmp_path)
    with pytest.raises(ValueError):
        import_tokens.create_token(team, "../escape")
