"""Tests for the platform-aware filestore mount decision in ``_mount_filestore``.

On macOS (and any non-Linux host) ``fuse-overlayfs`` cannot run, so an overlay
template must transparently fall back to a plain copy. On Linux the historical
hard error is preserved when the binary is missing.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from oduflow.docker_ops import env_ops
from oduflow.errors import PrerequisiteNotMetError
from oduflow.settings import Settings, TeamSettings


def _overlay_template(tmp_path, name="default"):
    """Create a template whose metadata requests overlay mode."""
    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path),
        port_registry_path=str(tmp_path / "ports.json"),
    )
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})

    fs_dir = tmp_path / "templates" / name / "filestore"
    fs_dir.mkdir(parents=True)
    (fs_dir / "a.txt").write_text("hello")

    with open(team.get_template_metadata_path(name), "w") as f:
        json.dump({"odoo_image": "odoo:17.0", "use_overlay": True}, f)

    return team, settings


def test_non_linux_falls_back_to_copy(tmp_path):
    team, settings = _overlay_template(tmp_path)
    odoo_volumes: dict = {}

    with (
        patch.object(env_ops.sys, "platform", "darwin"),
        patch.object(env_ops, "get_odoo_uid_gid", return_value="0:0"),
        patch.object(env_ops, "chown_recursive"),
        patch.object(env_ops.subprocess, "run") as run,
    ):
        env_ops._mount_filestore(
            MagicMock(),
            settings,
            team,
            env_name="myenv",
            env_db="myenv_db",
            odoo_image="odoo:17.0",
            odoo_volumes=odoo_volumes,
            template_name="default",
        )

    # No fuse-overlayfs was invoked and the filestore was copied, not mounted.
    run.assert_not_called()
    merged = next(iter(odoo_volumes))
    assert odoo_volumes[merged]["bind"].endswith("/filestore/myenv_db")
    with open(f"{merged}/a.txt") as f:
        assert f.read() == "hello"


def test_linux_missing_binary_still_errors(tmp_path):
    team, settings = _overlay_template(tmp_path)

    with (
        patch.object(env_ops.sys, "platform", "linux"),
        patch.object(env_ops, "get_odoo_uid_gid", return_value="0:0"),
        patch.object(env_ops, "chown_recursive"),
        patch.object(env_ops.shutil, "which", return_value=None),
        patch.object(env_ops.subprocess, "run") as run,
    ):
        with pytest.raises(
            PrerequisiteNotMetError, match="fuse-overlayfs is not installed"
        ):
            env_ops._mount_filestore(
                MagicMock(),
                settings,
                team,
                env_name="myenv",
                env_db="myenv_db",
                odoo_image="odoo:17.0",
                odoo_volumes={},
                template_name="default",
            )
    run.assert_not_called()
