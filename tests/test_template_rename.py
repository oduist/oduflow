"""Guard-path tests for system_ops.rename_template.

These cover the validation/collision/not-found branches that run BEFORE any
Docker call, so no Docker is needed. The DB-rename and dependent-env branches
require Docker and live under the integration marker elsewhere.
"""

import os

import pytest

from oduflow.docker_ops import system_ops
from oduflow.errors import ConflictError, NotFoundError
from oduflow.settings import Settings, TeamSettings


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def team(tmp_path):
    return TeamSettings(team_id="1", data_dir=str(tmp_path))


def _make_template(team, name):
    d = team.get_template_dir(name)
    os.makedirs(d, exist_ok=True)
    return d


def test_invalid_new_name_raises_value_error(settings, team):
    _make_template(team, "prod")
    with pytest.raises(ValueError):
        system_ops.rename_template(settings, team, "prod", "bad name")


def test_same_name_raises_conflict(settings, team):
    _make_template(team, "prod")
    with pytest.raises(ConflictError):
        system_ops.rename_template(settings, team, "prod", "prod")


def test_missing_source_raises_not_found(settings, team):
    with pytest.raises(NotFoundError):
        system_ops.rename_template(settings, team, "ghost", "prod")


def test_target_collision_raises_conflict(settings, team):
    _make_template(team, "prod")
    _make_template(team, "staging")
    with pytest.raises(ConflictError):
        system_ops.rename_template(settings, team, "prod", "staging")
