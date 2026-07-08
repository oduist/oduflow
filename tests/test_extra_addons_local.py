"""Unit tests for remote-less (local) extra-addons repos used by Odoo.sh import.

These exercise real git (no network, no Docker): create_local_repo seeds a bare
repo from files, fetch short-circuits on the .local marker, and a worktree can
be checked out from it.
"""

import os

import pytest

from oduflow.extra_addons import (
    create_local_repo,
    create_worktree,
    fetch_extra_repo,
    is_local_repo,
    list_extra_repos,
    pull_extra_worktree,
)
from oduflow.settings import TeamSettings


@pytest.fixture
def team(tmp_path):
    return TeamSettings(team_id="1", data_dir=str(tmp_path))


def _make_source(tmp_path):
    src = tmp_path / "src_enterprise"
    mod = src / "sale_enterprise"
    mod.mkdir(parents=True)
    (mod / "__manifest__.py").write_text("{'name': 'Sale Enterprise'}")
    # A stray .git pointer (as left by Odoo.sh worktrees) must be ignored.
    (src / ".git").write_text("gitdir: /somewhere/else")
    return str(src)


class TestCreateLocalRepo:
    def test_creates_bare_repo_with_branch_and_marker(self, team, tmp_path):
        src = _make_source(tmp_path)
        result = create_local_repo(team, "enterprise", src, "18.0")

        assert result["local"] is True
        assert result["branch"] == "18.0"
        assert result["repo_url"] == ""
        assert is_local_repo(team, "enterprise")
        assert os.path.exists(
            os.path.join(team.shared_repos_dir, "enterprise", ".local")
        )

    def test_requires_branch(self, team, tmp_path):
        src = _make_source(tmp_path)
        with pytest.raises(ValueError):
            create_local_repo(team, "enterprise", src, "")

    def test_rejects_bad_name(self, team, tmp_path):
        src = _make_source(tmp_path)
        with pytest.raises(ValueError):
            create_local_repo(team, "bad/name", src, "18.0")

    def test_listed_as_local(self, team, tmp_path):
        src = _make_source(tmp_path)
        create_local_repo(team, "enterprise", src, "18.0")
        repos = list_extra_repos(team)
        assert len(repos) == 1
        assert repos[0]["name"] == "enterprise"
        assert repos[0]["local"] is True
        assert repos[0]["repo_url"] == ""


class TestFetchShortCircuit:
    def test_fetch_is_noop_for_local(self, team, tmp_path):
        src = _make_source(tmp_path)
        create_local_repo(team, "enterprise", src, "18.0")
        summary = fetch_extra_repo(team, "enterprise")
        assert summary["local"] is True
        assert summary["up_to_date"] is True


class TestWorktreeFromLocal:
    def test_worktree_checks_out_files(self, team, tmp_path):
        src = _make_source(tmp_path)
        create_local_repo(team, "enterprise", src, "18.0")

        wt = tmp_path / "wt"
        create_worktree(team, "enterprise", "18.0", str(wt))

        assert (wt / "sale_enterprise" / "__manifest__.py").is_file()
        # The stray .git pointer from the source must not have been committed.
        assert (
            not (wt / ".git").is_file()
            or (wt / ".git").read_text() != "gitdir: /somewhere/else"
        )

    def test_pull_reports_no_changes(self, team, tmp_path):
        src = _make_source(tmp_path)
        create_local_repo(team, "enterprise", src, "18.0")
        wt = tmp_path / "wt"
        create_worktree(team, "enterprise", "18.0", str(wt))

        old_head, changed = pull_extra_worktree(team, "enterprise", "18.0", str(wt))
        assert changed == []
