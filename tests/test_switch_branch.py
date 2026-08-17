"""Reusing an environment for another branch (``switch_environment_branch``).

The environment is the reusable unit: switching the branch must keep the
database, filestore and routing while changing only the code. What these tests
pin down is the part that is easy to get subtly wrong — the *order* of the
mutations, and the two refusals that keep a reused environment honest (a branch
that was never pushed, a database whose installed modules the target branch does
not carry).
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from oduflow import git_ops
from oduflow.docker_ops import env_ops
from oduflow.errors import (
    ConflictError,
    ExternalCommandError,
    NotFoundError,
    PrerequisiteNotMetError,
)
from oduflow.settings import Settings, TeamSettings


def _team(tmp_path) -> TeamSettings:
    return TeamSettings(team_id="1", data_dir=str(tmp_path))


def _settings(tmp_path) -> Settings:
    return Settings(teams={"1": _team(tmp_path)})


def _container(labels: dict[str, str]) -> MagicMock:
    container = MagicMock()
    container.labels = labels
    return container


def _labels(**overrides: str) -> dict[str, str]:
    labels = {
        "oduflow.branch": "dev1",
        "oduflow.git_branch": "feature/old",
        "oduflow.repo": "https://github.com/acme/addons.git",
        "oduflow.git_user": "ada",
    }
    labels.update(overrides)
    return labels


# --- git plumbing ---------------------------------------------------------


class TestFetchBranch:
    def test_an_unpushed_branch_is_reported_as_missing_not_as_a_git_failure(
        self, tmp_path
    ):
        error = subprocess.CalledProcessError(128, ["git", "fetch"])
        error.stderr = "fatal: couldn't find remote ref refs/heads/feature/new"

        with patch("subprocess.run", side_effect=error):
            with pytest.raises(NotFoundError) as excinfo:
                git_ops.fetch_branch(str(tmp_path), "feature/new")

        assert "does not exist on origin" in str(excinfo.value)
        assert "git push -u origin feature/new" in str(excinfo.value)

    def test_any_other_fetch_failure_stays_an_external_command_error(self, tmp_path):
        error = subprocess.CalledProcessError(128, ["git", "fetch"])
        error.stderr = "fatal: Authentication failed"

        with patch("subprocess.run", side_effect=error):
            with pytest.raises(ExternalCommandError):
                git_ops.fetch_branch(str(tmp_path), "feature/new")

    def test_credentials_are_never_echoed_back_in_the_error(self, tmp_path):
        error = subprocess.CalledProcessError(128, ["git", "fetch"])
        error.stderr = "fatal: https://ada:ghp_secret@github.com/acme/addons.git denied"

        with patch("subprocess.run", side_effect=error):
            with pytest.raises(ExternalCommandError) as excinfo:
                git_ops.fetch_branch(str(tmp_path), "feature/new")

        assert "ghp_secret" not in str(excinfo.value)

    def test_returns_the_fetched_remote_tip(self, tmp_path):
        with (
            patch("subprocess.run") as run,
            patch.object(git_ops, "rev_parse", return_value="cafe123") as rev_parse,
        ):
            assert git_ops.fetch_branch(str(tmp_path), "feature/new") == "cafe123"

        assert run.call_count == 1
        assert rev_parse.call_args[0][1] == "refs/remotes/origin/feature/new"


class TestCheckoutBranch:
    def test_moves_the_clone_onto_the_branch_and_returns_the_tree_diff(self, tmp_path):
        heads = iter(["old111", "new222"])

        with (
            patch("subprocess.run") as run,
            patch.object(
                git_ops, "rev_parse", side_effect=lambda *_a, **_k: next(heads)
            ),
            patch.object(git_ops, "diff_names", return_value=["sale_x/models/a.py"]),
        ):
            old, new, files = git_ops.checkout_branch(str(tmp_path), "feature/new")

        assert (old, new, files) == ("old111", "new222", ["sale_x/models/a.py"])
        argv = run.call_args[0][0]
        # --force keeps the managed clone's policy identical to pull_repo's
        # reset --hard, and -B pins the local branch on the fetched remote tip.
        assert argv[3:] == [
            "checkout",
            "--force",
            "-B",
            "feature/new",
            "refs/remotes/origin/feature/new",
        ]

    def test_an_identical_tip_reports_no_changed_files(self, tmp_path):
        with (
            patch("subprocess.run"),
            patch.object(git_ops, "rev_parse", return_value="same"),
            patch.object(git_ops, "diff_names") as diff_names,
        ):
            old, new, files = git_ops.checkout_branch(str(tmp_path), "feature/new")

        assert (old, new, files) == ("same", "same", [])
        diff_names.assert_not_called()


class TestTreeModules:
    def test_reads_module_names_from_manifests_anywhere_in_the_tree(self, tmp_path):
        listing = (
            "README.md\n"
            "sale_x/__manifest__.py\n"
            "sale_x/models/sale.py\n"
            "addons/deep/crm_y/__manifest__.py\n"
            "__manifest__.py\n"
        )
        with patch("subprocess.run", return_value=MagicMock(stdout=listing)):
            modules = git_ops.tree_modules(str(tmp_path), "origin/main")

        # The repository root is never an addons directory, so a root manifest
        # is not a module the addons path could load.
        assert modules == {"sale_x", "crm_y"}


# --- switch_environment_branch -------------------------------------------


@pytest.fixture
def switch_env(tmp_path):
    """Patch everything a switch touches; yield the mocks it is asserted on."""
    client = MagicMock()
    client.containers.get.return_value = _container(_labels())

    with (
        patch.object(env_ops, "get_client", return_value=client),
        patch.object(git_ops, "is_git_repository", return_value=True),
        patch.object(git_ops, "fetch_branch", return_value="tip999") as fetch,
        patch.object(
            git_ops,
            "checkout_branch",
            return_value=("old111", "new222", ["sale_x/a.py"]),
        ) as checkout,
        patch.object(env_ops, "update_environment") as update,
        patch.object(
            env_ops,
            "pull_environment",
            return_value={"action": "restart", "message": "Restarted."},
        ) as pull,
        patch.object(env_ops, "_agent_add_env") as agent,
        patch.object(env_ops, "_dropped_module_warnings", return_value=[]) as preflight,
    ):
        yield {
            "client": client,
            "fetch": fetch,
            "checkout": checkout,
            "update": update,
            "pull": pull,
            "agent": agent,
            "preflight": preflight,
        }


def _switch(tmp_path, **kwargs):
    return env_ops.switch_environment_branch(
        _settings(tmp_path), _team(tmp_path), "dev1", "feature/new", **kwargs
    )


class TestSwitchEnvironmentBranch:
    def test_the_label_is_flipped_before_the_checkout(self, tmp_path, switch_env):
        calls: list[str] = []
        switch_env["update"].side_effect = lambda *a, **k: calls.append("label")
        switch_env["checkout"].side_effect = lambda *a, **k: (
            calls.append("checkout") or ("old111", "new222", ["sale_x/a.py"])
        )

        _switch(tmp_path)

        # Reversed, a failed label flip would leave a switched tree that the
        # next pull silently resets back to the old branch.
        assert calls == ["label", "checkout"]

    def test_only_the_branch_label_changes_and_no_image_is_pulled(
        self, tmp_path, switch_env
    ):
        _switch(tmp_path)

        kwargs = switch_env["update"].call_args.kwargs
        assert kwargs["label_overrides"] == {"oduflow.git_branch": "feature/new"}
        assert kwargs["pull_image"] is False
        # The checkout is still on the old branch at that point, so a changed
        # requirements.txt belongs to the diff, not to this recreate.
        assert kwargs["install_dependencies"] is False

    def test_the_diff_is_handed_to_the_normal_apply_path(self, tmp_path, switch_env):
        _switch(tmp_path, upgrade=["sale_x"], strict=True)

        kwargs = switch_env["pull"].call_args.kwargs
        assert kwargs["presynced"] == ("old111", ["sale_x/a.py"])
        assert kwargs["upgrade"] == ["sale_x"]
        assert kwargs["strict"] is True

    def test_the_result_records_both_branches_and_the_switch(
        self, tmp_path, switch_env
    ):
        result = _switch(tmp_path)

        assert result["branch"] == "feature/new"
        assert result["previous_branch"] == "feature/old"
        assert result["branch_switched"] is True
        assert result["old_head"] == "old111"
        assert result["new_head"] == "new222"
        assert result["message"].startswith(
            "Switched 'dev1' from 'feature/old' to 'feature/new'."
        )

    def test_two_branches_with_the_same_tree_do_not_report_being_up_to_date(
        self, tmp_path, switch_env
    ):
        switch_env["checkout"].return_value = ("same", "same", [])
        switch_env["pull"].return_value = {
            "action": "none",
            "message": "Already up to date.",
        }

        result = _switch(tmp_path)

        # "Already up to date" is pull vocabulary; after a switch it reads as a
        # contradiction of the switch that just happened.
        assert result["message"] == (
            "Switched 'dev1' from 'feature/old' to 'feature/new'. "
            "No code difference to apply."
        )

    def test_the_agents_own_checkout_follows_the_switch(self, tmp_path, switch_env):
        _switch(tmp_path)

        args = switch_env["agent"].call_args[0]
        assert args[3:] == (
            "dev1",
            "https://github.com/acme/addons.git",
            "feature/new",
            "ada",
        )

    def test_extra_addons_branches_switch_with_the_main_repo(
        self, tmp_path, switch_env
    ):
        _switch(tmp_path, extra_addons={"enterprise": "19.0"})

        overrides = switch_env["update"].call_args.kwargs["label_overrides"]
        assert overrides["oduflow.git_branch"] == "feature/new"
        assert overrides["oduflow.extra_addons"] == '{"enterprise": "19.0"}'

    def test_switching_to_the_branch_it_is_already_on_pulls_instead(
        self, tmp_path, switch_env
    ):
        switch_env["client"].containers.get.return_value = _container(
            _labels(**{"oduflow.git_branch": "feature/new"})
        )

        result = _switch(tmp_path)

        assert result["branch_switched"] is False
        switch_env["update"].assert_not_called()
        switch_env["checkout"].assert_not_called()
        switch_env["pull"].assert_called_once()
        assert "already on branch 'feature/new'" in result["message"]

    def test_a_live_mounted_environment_is_refused(self, tmp_path, switch_env):
        switch_env["client"].containers.get.return_value = _container(
            _labels(**{"oduflow.local_path": "/home/ada/addons"})
        )

        with pytest.raises(PrerequisiteNotMetError) as excinfo:
            _switch(tmp_path)

        assert "/home/ada/addons" in str(excinfo.value)
        switch_env["update"].assert_not_called()

    def test_a_production_is_refused_by_name_before_docker_is_touched(
        self, tmp_path, switch_env
    ):
        with pytest.raises(ConflictError) as excinfo:
            env_ops.switch_environment_branch(
                _settings(tmp_path), _team(tmp_path), "prod-erp", "feature/new"
            )

        assert "update_production" in str(excinfo.value)
        switch_env["client"].containers.get.assert_not_called()

    def test_a_production_container_is_refused_by_label_too(self, tmp_path, switch_env):
        switch_env["client"].containers.get.return_value = _container(
            _labels(**{"oduflow.prod": "true"})
        )

        with pytest.raises(ConflictError):
            _switch(tmp_path)

        switch_env["update"].assert_not_called()

    def test_an_unpushed_branch_leaves_the_environment_untouched(
        self, tmp_path, switch_env
    ):
        switch_env["fetch"].side_effect = NotFoundError("no such branch on origin")

        with pytest.raises(NotFoundError):
            _switch(tmp_path)

        switch_env["update"].assert_not_called()
        switch_env["checkout"].assert_not_called()

    def test_a_failed_checkout_says_the_label_already_moved(self, tmp_path, switch_env):
        switch_env["checkout"].side_effect = ExternalCommandError(
            "git checkout", 1, "index locked"
        )

        with pytest.raises(ExternalCommandError) as excinfo:
            _switch(tmp_path)

        message = str(excinfo.value)
        assert "labelled with branch 'feature/new'" in message
        assert "still on 'feature/old'" in message
        switch_env["pull"].assert_not_called()

    def test_a_missing_environment_is_a_not_found_error(self, tmp_path, switch_env):
        import docker

        switch_env["client"].containers.get.side_effect = docker.errors.NotFound("nope")

        with pytest.raises(NotFoundError):
            _switch(tmp_path)


class TestDroppedModulePreflight:
    def test_a_dropped_installed_module_is_reported_but_still_switches(
        self, tmp_path, switch_env
    ):
        switch_env["preflight"].return_value = ["sale_x is installed but absent"]

        result = _switch(tmp_path)

        assert result["branch_switched"] is True
        assert result["warnings"] == ["sale_x is installed but absent"]
        switch_env["update"].assert_called_once()

    def test_strict_refuses_and_mutates_nothing(self, tmp_path, switch_env):
        switch_env["preflight"].return_value = ["sale_x is installed but absent"]

        result = _switch(tmp_path, strict=True)

        assert result["action"] == "blocked"
        assert result["branch"] == "feature/old"
        assert result["requested_branch"] == "feature/new"
        switch_env["update"].assert_not_called()
        switch_env["checkout"].assert_not_called()
        switch_env["pull"].assert_not_called()

    def test_warnings_from_the_switch_and_from_the_guardrail_are_both_kept(
        self, tmp_path, switch_env
    ):
        switch_env["preflight"].return_value = ["module gone"]
        switch_env["pull"].return_value = {
            "action": "restart",
            "message": "Restarted.",
            "warnings": ["a data file changed but nothing was upgraded"],
        }

        result = _switch(tmp_path)

        assert result["warnings"] == [
            "module gone",
            "a data file changed but nothing was upgraded",
        ]

    def test_only_modules_installed_in_the_database_are_warned_about(self, tmp_path):
        trees = {"HEAD": {"sale_x", "crm_y"}, "tip999": {"crm_y"}}

        with (
            patch.object(
                git_ops, "tree_modules", side_effect=lambda _p, ref: trees[ref]
            ),
            patch(
                "oduflow.docker_ops.odoo_ops._get_module_states",
                return_value={"sale_x": "installed"},
            ) as states,
        ):
            warnings = env_ops._dropped_module_warnings(
                _settings(tmp_path), _team(tmp_path), "dev1", "/repo", "tip999"
            )

        assert states.call_args[0][3] == ("sale_x",)
        assert len(warnings) == 1
        assert "sale_x" in warnings[0]

    def test_an_uninstalled_module_is_not_worth_a_warning(self, tmp_path):
        trees = {"HEAD": {"sale_x"}, "tip999": set()}

        with (
            patch.object(
                git_ops, "tree_modules", side_effect=lambda _p, ref: trees[ref]
            ),
            patch(
                "oduflow.docker_ops.odoo_ops._get_module_states",
                return_value={"sale_x": "uninstalled"},
            ),
        ):
            assert (
                env_ops._dropped_module_warnings(
                    _settings(tmp_path), _team(tmp_path), "dev1", "/repo", "tip999"
                )
                == []
            )

    def test_an_unreachable_database_does_not_block_the_switch(self, tmp_path):
        trees = {"HEAD": {"sale_x"}, "tip999": set()}

        with (
            patch.object(
                git_ops, "tree_modules", side_effect=lambda _p, ref: trees[ref]
            ),
            patch(
                "oduflow.docker_ops.odoo_ops._get_module_states",
                side_effect=RuntimeError("postgres is down"),
            ),
        ):
            assert (
                env_ops._dropped_module_warnings(
                    _settings(tmp_path), _team(tmp_path), "dev1", "/repo", "tip999"
                )
                == []
            )

    def test_module_names_git_would_allow_but_sql_should_not_are_dropped(
        self, tmp_path
    ):
        trees = {"HEAD": {"sale_x", "we'ird-name"}, "tip999": set()}

        with (
            patch.object(
                git_ops, "tree_modules", side_effect=lambda _p, ref: trees[ref]
            ),
            patch(
                "oduflow.docker_ops.odoo_ops._get_module_states", return_value={}
            ) as states,
        ):
            env_ops._dropped_module_warnings(
                _settings(tmp_path), _team(tmp_path), "dev1", "/repo", "tip999"
            )

        assert states.call_args[0][3] == ("sale_x",)
