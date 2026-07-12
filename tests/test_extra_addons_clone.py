"""Unit tests for cloning remote extra-addons repos (clone_extra_repo).

These exercise real git (no network, no Docker): a local source repo with two
branches is cloned via a file:// URL, and we assert the clone is shallow yet
keeps every branch (--depth 1 --no-single-branch), so one bare repo still serves
worktrees for any Odoo version.
"""

import subprocess

import pytest

from oduflow.extra_addons import clone_extra_repo, create_worktree, list_extra_repos
from oduflow.settings import TeamSettings

_GIT_ID = [
    "-c",
    "user.name=Test",
    "-c",
    "user.email=test@example.com",
]


@pytest.fixture
def team(tmp_path):
    return TeamSettings(team_id="1", data_dir=str(tmp_path / "data"))


def _make_git_source(tmp_path):
    """Create a normal git repo with branches ``main`` and ``18.0``.

    Returns a ``file://`` URL — git only honours ``--depth`` for remote-style
    URLs; a plain local path is cloned in full (with a warning).
    """
    src = tmp_path / "source"
    mod = src / "sale_enterprise"
    mod.mkdir(parents=True)

    def _git(*args):
        subprocess.run(
            ["git", "-C", str(src), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    _git("init", "-b", "main")
    (mod / "__manifest__.py").write_text("{'name': 'Sale Enterprise'}")
    _git("add", "-A")
    _git(*_GIT_ID, "commit", "-m", "main commit")

    _git("checkout", "-b", "18.0")
    (mod / "views.xml").write_text("<odoo/>")
    _git("add", "-A")
    _git(*_GIT_ID, "commit", "-m", "18.0 commit")

    _git("checkout", "main")
    return f"file://{src}"


def _is_shallow(bare_path):
    out = subprocess.run(
        ["git", "-C", bare_path, "rev-parse", "--is-shallow-repository"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip() == "true"


class TestCloneExtraRepoShallow:
    def test_clone_is_shallow(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        result = clone_extra_repo(team, "enterprise", url)

        assert _is_shallow(result["path"])

    def test_clone_keeps_all_branches(self, team, tmp_path):
        url = _make_git_source(tmp_path)
        clone_extra_repo(team, "enterprise", url)

        repos = list_extra_repos(team)
        assert len(repos) == 1
        assert set(repos[0]["branches"]) == {"main", "18.0"}

    def test_worktree_from_non_default_branch(self, team, tmp_path):
        """A shallow, all-branches clone still checks out any branch's tip."""
        url = _make_git_source(tmp_path)
        clone_extra_repo(team, "enterprise", url)

        wt = tmp_path / "wt"
        create_worktree(team, "enterprise", "18.0", str(wt))

        # views.xml only exists on the 18.0 branch.
        assert (wt / "sale_enterprise" / "views.xml").is_file()
