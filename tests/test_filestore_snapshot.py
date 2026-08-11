"""Filestore snapshotting when an environment is published as a template.

Publishing used to copy the environment's whole merged filestore — baseline
included — even though the baseline is almost always unchanged, and then walk
the entire result to fix ownership. The snapshot now hardlinks everything it
can from the baseline and only the transferred files get chowned. These tests
pin that, plus the fallbacks, which must never be silent.
"""

import os
import subprocess
from unittest.mock import MagicMock, patch

from oduflow.docker_ops import system_ops


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(content)


def _tree(root):
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            with open(full) as fh:
                out[os.path.relpath(full, root)] = fh.read()
    return out


def _fixture(tmp_path):
    """Baseline template filestore + an env merged view derived from it."""
    baseline = tmp_path / "baseline"
    merged = tmp_path / "merged"
    for root in (baseline, merged):
        _write(str(root / "aa" / "unchanged"), "same")
        _write(str(root / "bb" / "edited"), "same")
    _write(str(merged / "bb" / "edited"), "changed by the env")
    _write(str(merged / "cc" / "added"), "new in the env")
    return str(baseline), str(merged)


def test_snapshot_hardlinks_unchanged_files(tmp_path):
    baseline, merged = _fixture(tmp_path)
    dest = str(tmp_path / "snapshot")

    transferred = system_ops._snapshot_filestore(merged, dest, link_dests=[baseline])

    assert transferred is not None, "rsync path expected on a dev/CI host"
    assert _tree(dest) == _tree(merged)

    unchanged = os.stat(os.path.join(dest, "aa/unchanged"))
    assert unchanged.st_ino == os.stat(os.path.join(baseline, "aa/unchanged")).st_ino

    edited = os.stat(os.path.join(dest, "bb/edited"))
    assert edited.st_ino != os.stat(os.path.join(baseline, "bb/edited")).st_ino
    assert edited.st_nlink == 1


def test_snapshot_survives_removal_of_the_linked_baseline(tmp_path):
    # The baseline is rmtree'd right after the snapshot is taken; unlinking one
    # name must not take the shared content with it.
    baseline, merged = _fixture(tmp_path)
    dest = str(tmp_path / "snapshot")
    expected = _tree(merged)

    system_ops._snapshot_filestore(merged, dest, link_dests=[baseline])
    import shutil

    shutil.rmtree(baseline)

    assert _tree(dest) == expected


def test_snapshot_replaces_a_stale_destination(tmp_path):
    baseline, merged = _fixture(tmp_path)
    dest = str(tmp_path / "snapshot")
    _write(os.path.join(dest, "leftover"), "from a previous run")

    system_ops._snapshot_filestore(merged, dest, link_dests=[baseline])

    assert not os.path.exists(os.path.join(dest, "leftover"))
    assert _tree(dest) == _tree(merged)


def test_snapshot_skips_missing_link_dest(tmp_path):
    _baseline, merged = _fixture(tmp_path)
    dest = str(tmp_path / "snapshot")

    with patch.object(system_ops.subprocess, "run", wraps=subprocess.run) as run:
        system_ops._snapshot_filestore(
            merged, dest, link_dests=[str(tmp_path / "nope")]
        )

    cmd = run.call_args.args[0]
    assert not [arg for arg in cmd if arg.startswith("--link-dest")]
    assert _tree(dest) == _tree(merged)


def test_snapshot_falls_back_loudly_without_rsync(tmp_path, caplog):
    baseline, merged = _fixture(tmp_path)
    dest = str(tmp_path / "snapshot")

    with patch.object(system_ops.shutil, "which", return_value=None):
        transferred = system_ops._snapshot_filestore(
            merged, dest, link_dests=[baseline]
        )

    assert transferred is None
    assert _tree(dest) == _tree(merged)
    assert "full copy" in caplog.text


def test_snapshot_falls_back_loudly_when_rsync_fails(tmp_path, caplog):
    baseline, merged = _fixture(tmp_path)
    dest = str(tmp_path / "snapshot")
    failed = subprocess.CompletedProcess([], 23, b"", b"rsync: partial transfer")

    with patch.object(system_ops.subprocess, "run", return_value=failed):
        transferred = system_ops._snapshot_filestore(
            merged, dest, link_dests=[baseline]
        )

    assert transferred is None
    assert _tree(dest) == _tree(merged)
    assert "partial transfer" in caplog.text


# --------------------------------------------------------------------------
# _chown_filestore
# --------------------------------------------------------------------------


def test_chown_touches_only_transferred_paths(tmp_path):
    root = tmp_path / "filestore"
    _write(str(root / "aa" / "linked"), "x")
    _write(str(root / "bb" / "fresh"), "y")

    with patch.object(system_ops.os, "chown") as chown:
        system_ops._chown_filestore(
            str(root), ["bb/fresh"], 101, 102, MagicMock(), "odoo:19.0"
        )

    chowned = {call.args[0] for call in chown.call_args_list}
    assert chowned == {str(root), str(root / "bb" / "fresh")}


def test_chown_ignores_paths_escaping_the_root(tmp_path):
    root = tmp_path / "filestore"
    _write(str(root / "safe"), "x")
    _write(str(tmp_path / "outside"), "y")

    with patch.object(system_ops.os, "chown") as chown:
        system_ops._chown_filestore(
            str(root), ["../outside", "safe"], 101, 102, MagicMock(), "odoo:19.0"
        )

    chowned = {call.args[0] for call in chown.call_args_list}
    assert chowned == {str(root), str(root / "safe")}


def test_chown_walks_everything_after_a_full_copy(tmp_path):
    root = tmp_path / "filestore"
    _write(str(root / "a"), "x")

    with patch.object(system_ops, "chown_recursive") as recursive:
        system_ops._chown_filestore(str(root), None, 101, 102, MagicMock(), "odoo:19.0")

    recursive.assert_called_once()


def test_chown_falls_back_to_container_on_permission_error(tmp_path):
    root = tmp_path / "filestore"
    _write(str(root / "a"), "x")

    with (
        patch.object(system_ops.os, "chown", side_effect=PermissionError),
        patch.object(system_ops, "chown_recursive") as recursive,
    ):
        system_ops._chown_filestore(
            str(root), ["a"], 101, 102, MagicMock(), "odoo:19.0"
        )

    recursive.assert_called_once()


# --------------------------------------------------------------------------
# publish_env_as_template wiring
# --------------------------------------------------------------------------


def _publish(tmp_path, *, env_template: str, target_template: str):
    """Drive publish_env_as_template over a real on-disk filestore layout."""
    import contextlib

    from oduflow.docker_ops import env_ops
    from oduflow.naming import get_db_name, get_filestore_paths
    from oduflow.settings import Settings, TeamSettings

    settings = Settings()
    team = TeamSettings(team_id="1", data_dir=str(tmp_path))
    env_name = "feature-x"

    baseline = team.get_template_filestore_path(env_template)
    merged = get_filestore_paths(env_name, team.workspaces_dir)["merged"]
    for root in (baseline, merged):
        _write(os.path.join(root, "aa", "unchanged"), "same")
    _write(os.path.join(merged, "cc", "added"), "new in the env")

    container = MagicMock()
    container.status = "exited"
    container.image.tags = ["odoo:19.0"]
    container.labels = {"oduflow.template": env_template}
    client = MagicMock()
    client.containers.get.return_value = container

    @contextlib.contextmanager
    def _no_remount(*args, **kwargs):
        yield MagicMock(affected=[], failures=[])

    with (
        patch.object(system_ops, "get_client", return_value=client),
        patch.object(
            system_ops,
            "_db_exists",
            side_effect=lambda c, s, db: db == get_db_name(env_name, team.team_id),
        ),
        patch.object(system_ops, "check_db_quota"),
        patch.object(system_ops, "_wait_pg_ready"),
        patch.object(system_ops, "_stream_exec_to_file"),
        patch.object(system_ops, "reload_template"),
        patch.object(system_ops, "get_odoo_uid_gid", return_value="101:102"),
        patch.object(system_ops, "chown_recursive"),
        patch.object(system_ops.os, "chown"),
        patch.object(
            system_ops, "_update_template_sizes", side_effect=lambda *a: a[-1]
        ),
        patch.object(env_ops, "remount_template_overlays", _no_remount),
        patch.object(
            system_ops, "_snapshot_filestore", wraps=system_ops._snapshot_filestore
        ) as snapshot,
    ):
        system_ops.publish_env_as_template(
            settings, team, env_name, target_template, overwrite=True
        )

    return team, snapshot, baseline, merged


def test_publish_links_against_the_environments_own_baseline(tmp_path):
    team, snapshot, baseline, merged = _publish(
        tmp_path, env_template="prod", target_template="prod"
    )

    assert snapshot.call_args.kwargs["link_dests"] == [baseline]
    published = team.get_template_filestore_path("prod")
    assert _tree(published) == _tree(merged)


def test_publish_links_against_both_source_and_target_templates(tmp_path):
    # Publishing an env under a NEW template name: its own baseline holds the
    # matching files, the target template is where a re-baseline would match.
    team, snapshot, baseline, _merged = _publish(
        tmp_path, env_template="base", target_template="prod"
    )

    assert snapshot.call_args.kwargs["link_dests"] == [
        baseline,
        team.get_template_filestore_path("prod"),
    ]
