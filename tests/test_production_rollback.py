"""Update engine tests: auto code rollback on failed production deploys."""

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from oduflow import production_registry
from oduflow.docker_ops import production_ops
from oduflow.git_ops import rev_parse
from oduflow.settings import Settings, TeamSettings


@pytest.fixture
def team(tmp_path):
    data_dir = tmp_path / "team_1"
    data_dir.mkdir()
    return TeamSettings(team_id="1", data_dir=str(data_dir))


@pytest.fixture
def settings(team, tmp_path):
    return Settings(
        routing_mode="traefik",
        acme_email="a@b.co",
        base_data_dir=str(tmp_path),
        teams={"1": team},
    )


def _git(cwd, *args):
    subprocess.run(
        ["git", "-C", cwd, *args],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


@pytest.fixture
def prod_repo(team, tmp_path):
    """A production 'erp' with a two-commit git checkout in its workspace."""
    production_registry.create_production(
        team, "erp", {"domain": "erp.example.com", "branch": "main"}
    )
    repo_path = os.path.join(team.workspaces_dir, "prod-erp", "repo")
    os.makedirs(repo_path)
    subprocess.run(
        ["git", "init", "-q", "-b", "main", repo_path], check=True, capture_output=True
    )
    with open(os.path.join(repo_path, "a.py"), "w") as f:
        f.write("v1\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-qm", "c1")
    old_head = rev_parse(repo_path)
    with open(os.path.join(repo_path, "a.py"), "w") as f:
        f.write("v2\n")
    _git(repo_path, "add", ".")
    _git(repo_path, "commit", "-qm", "c2")
    new_head = rev_parse(repo_path)
    return repo_path, old_head, new_head


def _container():
    container = MagicMock()
    container.labels = {"oduflow.team": "1", "oduflow.prod": "true"}
    container.status = "running"
    return container


def _run_update(settings, team, pull_result, healthy_sequence, container):
    """Run update_production with pull/health mocked; return (result, health_mock)."""
    client = MagicMock()
    health = MagicMock(side_effect=healthy_sequence)
    with (
        patch.object(production_ops, "get_client", return_value=client),
        patch.object(production_ops, "_require_container", return_value=container),
        patch("oduflow.docker_ops.env_ops.pull_environment", return_value=pull_result),
        patch.object(production_ops, "wait_production_healthy", health),
        patch.object(production_ops, "reapply_prod_odoo_conf", return_value=True),
    ):
        result = production_ops.update_production(settings, team, "erp", trigger="test")
    return result, health


class TestUpdateSuccess:
    def test_successful_deploy_recorded(self, settings, team, prod_repo):
        repo_path, old_head, new_head = prod_repo
        container = _container()
        result, _ = _run_update(
            settings,
            team,
            {
                "action": "upgrade",
                "exit_code": 0,
                "changed_files": ["a.py"],
                "modules_upgraded": ["m"],
                "message": "ok",
            },
            [True],
            container,
        )
        assert result["action"] == "upgrade"
        assert result["commit"] == new_head
        deploys = production_ops.read_deploys(team, "erp")
        assert deploys[-1]["status"] == "success"
        record = production_registry.get_production(team, "erp")
        assert record["deploy_in_progress"] is False
        assert record["unhealthy"] is False

    def test_refresh_promoted_to_restart(self, settings, team, prod_repo):
        container = _container()
        result, _ = _run_update(
            settings,
            team,
            {
                "action": "refresh",
                "exit_code": 0,
                "changed_files": ["v.xml"],
                "message": "refresh",
            },
            [True],
            container,
        )
        assert result["action"] == "restart"
        container.restart.assert_called_once()

    def test_no_changes_is_not_a_deploy(self, settings, team, prod_repo):
        container = _container()
        before = len(production_ops.read_deploys(team, "erp", limit=0))
        result, health = _run_update(
            settings,
            team,
            {"action": "none", "message": "up to date"},
            [True],
            container,
        )
        assert result["action"] == "none"
        assert len(production_ops.read_deploys(team, "erp", limit=0)) == before
        health.assert_not_called()


class TestUpdateRollback:
    def test_failed_upgrade_rolls_code_back(self, settings, team, prod_repo):
        repo_path, old_head, new_head = prod_repo
        # Simulate: pull moved HEAD to new_head (it already is), upgrade failed.
        container = _container()
        result, _ = _run_update(
            settings,
            team,
            {
                "action": "upgrade",
                "exit_code": 1,
                "changed_files": ["a.py"],
                "output": "Traceback ...",
                "message": "failed",
            },
            [True],  # rollback health check succeeds
            container,
        )
        assert result["action"] == "rolled_back"
        # Code is back at... wait: from_commit was HEAD before pull. In this
        # test HEAD never moved (pull is mocked), so rollback resets to the
        # recorded old head == current head == new_head.
        assert rev_parse(repo_path) == new_head
        deploys = production_ops.read_deploys(team, "erp")
        assert deploys[-1]["status"] == "rolled_back"
        assert "NOT rolled back" in result["message"]  # DB warning present
        record = production_registry.get_production(team, "erp")
        assert record["unhealthy"] is False
        assert record["deploy_in_progress"] is False

    def test_health_failure_triggers_rollback(self, settings, team, prod_repo):
        container = _container()
        result, health = _run_update(
            settings,
            team,
            {
                "action": "restart",
                "exit_code": 0,
                "changed_files": ["a.py"],
                "message": "restarted",
            },
            [False, True],  # deploy verify fails, rollback verify succeeds
            container,
        )
        assert result["action"] == "rolled_back"
        assert health.call_count == 2

    def test_rollback_failure_marks_unhealthy(self, settings, team, prod_repo):
        container = _container()
        result, _ = _run_update(
            settings,
            team,
            {
                "action": "upgrade",
                "exit_code": 1,
                "changed_files": ["a.py"],
                "message": "failed",
            },
            [False, False],
            container,
        )
        assert result["action"] == "rollback_failed"
        deploys = production_ops.read_deploys(team, "erp")
        assert deploys[-1]["status"] == "rollback_failed"
        assert production_registry.get_production(team, "erp")["unhealthy"] is True

    def test_real_code_reset(self, settings, team, prod_repo):
        """End-to-end git behaviour: HEAD actually moves back on rollback."""
        repo_path, old_head, new_head = prod_repo
        container = _container()

        # Reset the checkout to old_head, then a fake pull that moves it to
        # new_head and fails — rollback must land back on old_head.
        from oduflow.git_ops import reset_hard

        reset_hard(repo_path, old_head)

        def fake_pull(settings_, team_, env_name, **kw):
            reset_hard(repo_path, new_head)
            return {
                "action": "upgrade",
                "exit_code": 1,
                "changed_files": ["a.py"],
                "message": "failed",
            }

        client = MagicMock()
        with (
            patch.object(production_ops, "get_client", return_value=client),
            patch.object(production_ops, "_require_container", return_value=container),
            patch("oduflow.docker_ops.env_ops.pull_environment", fake_pull),
            patch.object(production_ops, "wait_production_healthy", side_effect=[True]),
            patch.object(production_ops, "reapply_prod_odoo_conf", return_value=True),
        ):
            result = production_ops.update_production(settings, team, "erp")

        assert result["action"] == "rolled_back"
        assert rev_parse(repo_path) == old_head
        assert result["failed_commit"] == new_head


class TestManualRollback:
    def test_rollback_to_explicit_commit(self, settings, team, prod_repo):
        repo_path, old_head, new_head = prod_repo
        container = _container()
        client = MagicMock()
        with (
            patch.object(production_ops, "get_client", return_value=client),
            patch.object(production_ops, "_require_container", return_value=container),
            patch.object(production_ops, "wait_production_healthy", return_value=True),
            patch.object(production_ops, "reapply_prod_odoo_conf", return_value=True),
        ):
            result = production_ops.rollback_production(settings, team, "erp", old_head)
        assert result["healthy"] is True
        assert rev_parse(repo_path) == old_head
        deploys = production_ops.read_deploys(team, "erp")
        assert deploys[-1]["action"] == "rollback"

    def test_rollback_unknown_commit_raises(self, settings, team, prod_repo):
        from oduflow.errors import NotFoundError

        container = _container()
        client = MagicMock()
        with (
            patch.object(production_ops, "get_client", return_value=client),
            patch.object(production_ops, "_require_container", return_value=container),
        ):
            with pytest.raises(NotFoundError, match="not found"):
                production_ops.rollback_production(settings, team, "erp", "0" * 40)
