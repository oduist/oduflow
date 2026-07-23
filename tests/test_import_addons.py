"""Unit tests for the Odoo.sh addon ingest server-side contract (no Docker).

Covers extract_addon_dir (tar → staged addon dir) and _wire_imported_addons
(staged addon → local extra-addons repo + template mapping). The remote (clone)
branch needs network and is not exercised here.
"""

import json
import tarfile

import pytest

from oduflow.docker_ops import system_ops
from oduflow.extra_addons import is_local_repo
from oduflow.settings import TeamSettings


@pytest.fixture
def team(tmp_path):
    return TeamSettings(team_id="1", data_dir=str(tmp_path))


def _make_repo_dir(tmp_path, top="enterprise_src"):
    src = tmp_path / top
    mod = src / "sale_enterprise"
    mod.mkdir(parents=True)
    (mod / "__manifest__.py").write_text("{'name': 'Sale Enterprise'}")
    return src


def _tar_dir(src_dir, tar_path, arcname):
    """Mirror the script's `tar -C parent -cf - base` (top-level dir = base)."""
    with tarfile.open(tar_path, "w") as tf:
        tf.add(str(src_dir), arcname=arcname)


class TestExtractAddonDir:
    def test_single_top_dir_renamed_to_name(self, tmp_path):
        src = _make_repo_dir(tmp_path)
        tar_path = tmp_path / "a.tar"
        _tar_dir(src, tar_path, "enterprise_src")
        addons_dir = tmp_path / "staging" / "addons"
        system_ops.extract_addon_dir(str(tar_path), str(addons_dir), "enterprise")
        assert (
            addons_dir / "enterprise" / "sale_enterprise" / "__manifest__.py"
        ).is_file()
        assert not (addons_dir / "enterprise_src").exists()
        assert not (addons_dir / ".incoming_enterprise").exists()

    def test_bare_contents_multiple_top_dirs(self, tmp_path):
        # A tar whose top level is several module dirs (no single wrapper).
        base = tmp_path / "content"
        (base / "mod_a").mkdir(parents=True)
        (base / "mod_a" / "__manifest__.py").write_text("{}")
        (base / "mod_b").mkdir(parents=True)
        (base / "mod_b" / "__manifest__.py").write_text("{}")
        tar_path = tmp_path / "c.tar"
        with tarfile.open(tar_path, "w") as tf:
            tf.add(str(base / "mod_a"), arcname="mod_a")
            tf.add(str(base / "mod_b"), arcname="mod_b")
        addons_dir = tmp_path / "staging" / "addons"
        system_ops.extract_addon_dir(str(tar_path), str(addons_dir), "themes")
        assert (addons_dir / "themes" / "mod_a" / "__manifest__.py").is_file()
        assert (addons_dir / "themes" / "mod_b" / "__manifest__.py").is_file()


class TestWireImportedAddons:
    def test_no_manifest_returns_empty(self, team, tmp_path):
        staging = tmp_path / "staging"
        staging.mkdir()
        assert system_ops._wire_imported_addons(team, str(staging), "18.0") == {}

    def test_local_addon_becomes_local_repo(self, team, tmp_path):
        staging = tmp_path / "staging"
        addons = staging / "addons" / "enterprise"
        (addons / "sale_enterprise").mkdir(parents=True)
        (addons / "sale_enterprise" / "__manifest__.py").write_text("{}")
        (staging / "addons.json").write_text(
            json.dumps([{"name": "enterprise", "kind": "local", "branch": "18.0"}])
        )
        wired = system_ops._wire_imported_addons(team, str(staging), "18.0")
        assert wired == {"enterprise": "18.0"}
        assert is_local_repo(team, "enterprise")

    def test_branch_falls_back_to_major_version(self, team, tmp_path):
        staging = tmp_path / "staging"
        addons = staging / "addons" / "themes"
        addons.mkdir(parents=True)
        (addons / "readme.txt").write_text("x")
        (staging / "addons.json").write_text(
            json.dumps([{"name": "themes", "kind": "local", "branch": "HEAD"}])
        )
        wired = system_ops._wire_imported_addons(team, str(staging), "17.0")
        assert wired == {"themes": "17.0"}

    def test_existing_repo_is_referenced_not_recreated(self, team, tmp_path):
        source = _make_repo_dir(tmp_path, top="existing-enterprise")
        from oduflow.extra_addons import create_local_repo

        create_local_repo(team, "enterprise", str(source), "18.0")
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "addons.json").write_text(
            json.dumps([{"name": "enterprise", "kind": "local", "branch": "18.0"}])
        )
        wired = system_ops._wire_imported_addons(team, str(staging), "18.0")
        assert wired == {"enterprise": "18.0"}

    def test_existing_repo_without_requested_branch_fails(self, team, tmp_path):
        source = _make_repo_dir(tmp_path, top="existing-enterprise")
        from oduflow.errors import NotFoundError
        from oduflow.extra_addons import create_local_repo

        create_local_repo(team, "enterprise", str(source), "17.0")
        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "addons.json").write_text(
            json.dumps([{"name": "enterprise", "kind": "local", "branch": "18.0"}])
        )
        with pytest.raises(NotFoundError, match="Branch '18.0'"):
            system_ops._wire_imported_addons(team, str(staging), "18.0")

    def test_declared_addon_without_remote_or_files_fails(self, team, tmp_path):
        from oduflow.errors import PrerequisiteNotMetError

        staging = tmp_path / "staging"
        staging.mkdir()
        (staging / "addons.json").write_text(
            json.dumps([{"name": "enterprise", "kind": "local", "branch": "18.0"}])
        )
        with pytest.raises(PrerequisiteNotMetError, match="no uploaded files"):
            system_ops._wire_imported_addons(team, str(staging), "18.0")
