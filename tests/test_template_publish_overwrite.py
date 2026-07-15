"""Guard-path tests for system_ops.publish_env_as_template overwrite protection.

Publishing an environment over an EXISTING template used to silently clobber it
(its DB + filestore, remounting other envs). publish_env_as_template now refuses
unless the caller explicitly opts in with overwrite=True, so the dashboard
"New Template" action (which never sets overwrite) can only create fresh
templates.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from oduflow.docker_ops import system_ops
from oduflow.errors import ConflictError
from oduflow.naming import get_db_name, get_template_db_name
from oduflow.settings import Settings, TeamSettings

ENV = "feature-x"
TPL = "prod"


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def team(tmp_path):
    return TeamSettings(team_id="1", data_dir=str(tmp_path))


def _db_exists_map(existing):
    """Return a _db_exists side_effect that reports only `existing` db names."""

    def _fn(client, settings, db_name):
        return db_name in existing

    return _fn


def test_refuses_overwrite_when_template_dir_exists(settings, team):
    # Source env DB exists, template DB does not, but the template dir is on disk.
    env_db = get_db_name(ENV, team.team_id)
    os.makedirs(team.get_template_dir(TPL), exist_ok=True)

    with (
        patch("oduflow.docker_ops.system_ops.get_client", return_value=MagicMock()),
        patch(
            "oduflow.docker_ops.system_ops._db_exists",
            side_effect=_db_exists_map({env_db}),
        ),
    ):
        with pytest.raises(ConflictError, match="already exists"):
            system_ops.publish_env_as_template(settings, team, ENV, TPL)


def test_refuses_overwrite_when_template_db_exists(settings, team):
    # No template dir on disk, but the template DB is loaded in PostgreSQL.
    env_db = get_db_name(ENV, team.team_id)
    tpl_db = get_template_db_name(TPL, team.team_id)

    with (
        patch("oduflow.docker_ops.system_ops.get_client", return_value=MagicMock()),
        patch(
            "oduflow.docker_ops.system_ops._db_exists",
            side_effect=_db_exists_map({env_db, tpl_db}),
        ),
    ):
        with pytest.raises(ConflictError, match="already exists"):
            system_ops.publish_env_as_template(settings, team, ENV, TPL)


def test_overwrite_true_bypasses_guard(settings, team):
    # overwrite=True skips the ConflictError guard; execution then reaches the
    # quota check (our sentinel), proving the guard did not fire on an existing
    # template.
    env_db = get_db_name(ENV, team.team_id)
    os.makedirs(team.get_template_dir(TPL), exist_ok=True)
    sentinel = RuntimeError("reached quota check")

    with (
        patch("oduflow.docker_ops.system_ops.get_client", return_value=MagicMock()),
        patch(
            "oduflow.docker_ops.system_ops._db_exists",
            side_effect=_db_exists_map({env_db}),
        ),
        patch("oduflow.docker_ops.system_ops.check_db_quota", side_effect=sentinel),
    ):
        with pytest.raises(RuntimeError, match="reached quota check"):
            system_ops.publish_env_as_template(settings, team, ENV, TPL, overwrite=True)
