"""Per-environment share secrets (the credential inside a /env/<name> link)."""

from __future__ import annotations

import json
import os
import stat

import pytest

from oduflow import env_share
from oduflow.settings import TeamSettings


def _team(tmp_path) -> TeamSettings:
    return TeamSettings(team_id="1", data_dir=str(tmp_path))


def test_not_shared_by_default(tmp_path):
    team = _team(tmp_path)
    assert env_share.get(team, "feature/x") is None
    assert not env_share.verify(team, "feature/x", "anything")


def test_create_is_idempotent_and_verifies(tmp_path):
    team = _team(tmp_path)
    secret = env_share.create_or_get(team, "feature/x")
    assert secret
    assert env_share.create_or_get(team, "feature/x") == secret
    assert env_share.verify(team, "feature/x", secret)
    assert not env_share.verify(team, "feature/x", secret + "x")
    assert not env_share.verify(team, "feature/x", "")
    # Sharing one environment shares only that one.
    assert not env_share.verify(team, "other", secret)


def test_rotate_replaces_the_secret(tmp_path):
    team = _team(tmp_path)
    old = env_share.create_or_get(team, "feature/x")
    new = env_share.rotate(team, "feature/x")
    assert new != old
    assert env_share.verify(team, "feature/x", new)
    assert not env_share.verify(team, "feature/x", old)


def test_revoke_removes_the_share(tmp_path):
    team = _team(tmp_path)
    secret = env_share.create_or_get(team, "feature/x")
    assert env_share.revoke(team, "feature/x") is True
    assert env_share.get(team, "feature/x") is None
    assert not env_share.verify(team, "feature/x", secret)
    # Revoking again is a no-op, not an error.
    assert env_share.revoke(team, "feature/x") is False


def test_rename_carries_the_share_over(tmp_path):
    team = _team(tmp_path)
    secret = env_share.create_or_get(team, "old")
    env_share.rename(team, "old", "new")
    assert env_share.verify(team, "new", secret)
    assert env_share.get(team, "old") is None


def test_shares_of_different_environments_are_independent(tmp_path):
    team = _team(tmp_path)
    a = env_share.create_or_get(team, "a")
    b = env_share.create_or_get(team, "b")
    assert a != b
    env_share.revoke(team, "a")
    assert env_share.verify(team, "b", b)


def test_registry_file_is_not_world_readable(tmp_path):
    team = _team(tmp_path)
    env_share.create_or_get(team, "feature/x")
    mode = stat.S_IMODE(os.stat(env_share.shares_path(team)).st_mode)
    assert mode == 0o600
    with open(env_share.shares_path(team)) as f:
        assert "feature/x" in json.load(f)


def test_malformed_registry_fails_closed(tmp_path):
    team = _team(tmp_path)
    with open(env_share.shares_path(team), "w") as f:
        f.write("null")
    assert env_share.get(team, "feature/x") is None
    assert not env_share.verify(team, "feature/x", "anything")
    # And can still be written over.
    secret = env_share.create_or_get(team, "feature/x")
    assert env_share.verify(team, "feature/x", secret)


def test_sharing_requires_a_data_dir():
    team = TeamSettings(team_id="1", data_dir="")
    with pytest.raises(ValueError):
        env_share.create_or_get(team, "feature/x")
    assert env_share.get(team, "feature/x") is None
