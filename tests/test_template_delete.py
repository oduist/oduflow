"""Guard-path tests for system_ops.delete_template.

Deleting a template `rmtree`s its filestore, which is the overlay lower layer
for every environment created from it (see env_ops._mount_filestore). So
delete_template must refuse while any such environment exists — mirroring the
guard in rename_template — or it would break those envs' overlays.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from oduflow.docker_ops import system_ops
from oduflow.errors import ConflictError
from oduflow.settings import Settings, TeamSettings


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def team(tmp_path):
    return TeamSettings(team_id="1", data_dir=str(tmp_path))


def _env_container(settings, template, branch="main"):
    c = MagicMock()
    c.labels = {"oduflow.template": template, settings.branch_label: branch}
    c.name = f"oduflow-1-{branch}-odoo"
    return c


def _client(container_list):
    client = MagicMock()
    client.containers.list.return_value = container_list
    return client


def test_blocks_when_env_uses_template(settings, team):
    tpl_dir = team.get_template_dir("prod")
    os.makedirs(tpl_dir, exist_ok=True)
    client = _client([_env_container(settings, "prod", branch="feature-x")])

    with patch("oduflow.docker_ops.system_ops.get_client", return_value=client):
        with pytest.raises(ConflictError, match="used by environments: feature-x"):
            system_ops.delete_template(settings, team, "prod")

    # Nothing was deleted — the template dir (overlay lower layer) survives.
    assert os.path.isdir(tpl_dir)


def test_allows_when_no_dependents(settings, team):
    tpl_dir = team.get_template_dir("prod")
    os.makedirs(tpl_dir, exist_ok=True)
    client = _client([])

    with (
        patch("oduflow.docker_ops.system_ops.get_client", return_value=client),
        patch("oduflow.docker_ops.system_ops._db_exists", return_value=False),
    ):
        result = system_ops.delete_template(settings, team, "prod")

    assert result["status"] == "dropped"
    assert not os.path.isdir(tpl_dir)


def test_ignores_envs_of_other_templates(settings, team):
    tpl_dir = team.get_template_dir("prod")
    os.makedirs(tpl_dir, exist_ok=True)
    # A live env exists, but it was built from a *different* template.
    client = _client([_env_container(settings, "staging")])

    with (
        patch("oduflow.docker_ops.system_ops.get_client", return_value=client),
        patch("oduflow.docker_ops.system_ops._db_exists", return_value=False),
    ):
        result = system_ops.delete_template(settings, team, "prod")

    assert result["status"] == "dropped"
    assert not os.path.isdir(tpl_dir)
