"""Unit tests for backup_ops helpers that need no Docker/S3."""

from __future__ import annotations

import hashlib
import os
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

from oduflow import backup_ops
from oduflow.docker_ops import production_ops
from oduflow.errors import ExternalCommandError, PrerequisiteNotMetError
from oduflow.settings import BackupSettings, Settings, TeamSettings


class _FakeApi:
    def __init__(self, frames, exit_code):
        self._frames = frames
        self._exit = exit_code

    def exec_create(self, container_id, cmd):
        return {"Id": "exec-1"}

    def exec_start(self, exec_id, stream, demux):
        return iter(self._frames)

    def exec_inspect(self, exec_id):
        return {"ExitCode": self._exit}


class _FakeContainer:
    def __init__(self, frames, exit_code):
        self.id = "cid"
        self.client = type("_C", (), {"api": _FakeApi(frames, exit_code)})()


class TestDumpStream:
    def test_yields_stdout_and_ignores_stderr_on_success(self):
        container = _FakeContainer(
            [(b"a", None), (None, b"NOTICE: ..."), (b"b", None)], exit_code=0
        )
        assert list(backup_ops._dump_stream(container, "odoo", "db")) == [b"a", b"b"]

    def test_raises_on_nonzero_exit_after_streaming(self):
        # pg_dump emits some output, then dies mid-dump (exit 1). The generator
        # must raise after the last frame so the snapshot fails before the
        # manifest is written (no truncated dump recorded as success).
        container = _FakeContainer([(b"partial", None)], exit_code=1)
        gen = backup_ops._dump_stream(container, "odoo", "db")
        assert next(gen) == b"partial"
        with pytest.raises(ExternalCommandError):
            next(gen)


class TestFilestoreRestore:
    def test_manifest_requires_explicit_revision(self):
        with pytest.raises(PrerequisiteNotMetError, match="no filestore revision"):
            backup_ops._filestore_revision_from_manifest({"filestore": {}})

    def test_revision_zero_is_valid(self):
        assert (
            backup_ops._filestore_revision_from_manifest({"filestore": {"revision": 0}})
            == 0
        )

    def test_empty_staged_filestore_replaces_old_files(self, tmp_path):
        live = tmp_path / "filestore"
        staged = tmp_path / "staged"
        old = tmp_path / "old"
        live.mkdir()
        staged.mkdir()
        (live / "attachment").write_text("old")

        had_previous = backup_ops._swap_restored_filestore(
            str(staged), str(live), str(old)
        )

        assert had_previous is True
        assert list(live.iterdir()) == []
        assert (old / "attachment").read_text() == "old"

    def test_failed_filestore_swap_restores_old_files(self, tmp_path, monkeypatch):
        live = tmp_path / "filestore"
        staged = tmp_path / "staged"
        old = tmp_path / "old"
        live.mkdir()
        staged.mkdir()
        (live / "attachment").write_text("old")
        (staged / "attachment").write_text("new")
        real_replace = os.replace

        def fail_staged(src, dst):
            if src == str(staged):
                raise OSError("swap failed")
            return real_replace(src, dst)

        monkeypatch.setattr(backup_ops.os, "replace", fail_staged)

        with pytest.raises(OSError, match="swap failed"):
            backup_ops._swap_restored_filestore(str(staged), str(live), str(old))

        assert (live / "attachment").read_text() == "old"
        assert (staged / "attachment").read_text() == "new"


def _restore_settings(tmp_path):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
    settings = Settings(
        base_data_dir=str(tmp_path),
        db_user="odoo",
        db_password="secret",
        backup=BackupSettings(bucket="bucket", access_key="key", secret_key="secret"),
        teams={"1": team},
    )
    return settings, team


def _manifest(revision=1):
    dump = b"database dump"
    return {
        "id": "snap",
        "db": {
            "bytes": len(dump),
            "key": "db/snap.pgdump",
            "sha256": hashlib.sha256(dump).hexdigest(),
        },
        "filestore": {"revision": revision},
    }


def _restore_patches(manifest, *, container=None):
    client = MagicMock()
    pg = MagicMock()
    pg.exec_run.return_value = (0, b"")
    client.containers.get.return_value = pg
    s3 = MagicMock()

    def download(_bucket, _key, destination):
        with open(destination, "wb") as f:
            f.write(b"database dump")

    s3.download_file.side_effect = download
    return (
        client,
        pg,
        s3,
        (
            patch.object(
                backup_ops.production_registry, "get_production", return_value={}
            ),
            patch.object(backup_ops, "_load_manifest", return_value=manifest),
            patch.object(backup_ops, "get_client", return_value=client),
            patch.object(production_ops, "_get_container", return_value=container),
            patch.object(backup_ops.s3_client, "make_client", return_value=s3),
            patch.object(backup_ops, "_copy_file_to_container"),
            patch.object(
                backup_ops,
                "load_credentials",
                return_value={"pg_user": "prod_user", "pg_password": "pw"},
            ),
            patch.object(backup_ops, "reassign_db_ownership"),
            patch.object(backup_ops, "drop_signaling_sequences"),
            patch.object(backup_ops, "filestore_storage", return_value=MagicMock()),
        ),
    )


def test_chunk_restore_failure_happens_before_live_database_swap(tmp_path):
    settings, team = _restore_settings(tmp_path)
    manifest = _manifest()
    _client, _pg, _s3, patches = _restore_patches(manifest)
    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        stack.enter_context(
            patch.object(
                backup_ops.chunkstore, "restore", side_effect=OSError("chunk failed")
            )
        )
        sql = stack.enter_context(patch.object(backup_ops, "_exec_sql"))
        with pytest.raises(OSError, match="chunk failed"):
            backup_ops.restore_production(settings, team, "erp", "snap")

    statements = [call.args[2] for call in sql.call_args_list]
    assert not any("ALTER DATABASE" in statement for statement in statements)


def test_filestore_preparation_failure_does_not_stop_production(tmp_path):
    settings, team = _restore_settings(tmp_path)
    container = MagicMock(status="running")
    manifest = _manifest()
    _client, _pg, _s3, patches = _restore_patches(manifest, container=container)

    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        stack.enter_context(
            patch.object(
                backup_ops.chunkstore, "restore", side_effect=OSError("chunk failed")
            )
        )
        with pytest.raises(OSError, match="chunk failed"):
            backup_ops.restore_production(settings, team, "erp", "snap")

    container.stop.assert_not_called()
    container.start.assert_not_called()


def test_filestore_swap_failure_rolls_database_back(tmp_path):
    settings, team = _restore_settings(tmp_path)
    manifest = _manifest()
    _client, _pg, _s3, patches = _restore_patches(manifest)
    db_name = production_ops.prod_db_name(team, "erp")

    with ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        stack.enter_context(patch.object(backup_ops.chunkstore, "restore"))
        stack.enter_context(
            patch.object(
                backup_ops,
                "_swap_restored_filestore",
                side_effect=OSError("filestore swap failed"),
            )
        )
        sql = stack.enter_context(patch.object(backup_ops, "_exec_sql"))
        with pytest.raises(OSError, match="filestore swap failed"):
            backup_ops.restore_production(settings, team, "erp", "snap")

    statements = [call.args[2] for call in sql.call_args_list]
    assert f'ALTER DATABASE "{db_name}" RENAME TO "{db_name}__old";' in statements
    assert f'ALTER DATABASE "{db_name}__restore" RENAME TO "{db_name}";' in statements
    assert f'ALTER DATABASE "{db_name}" RENAME TO "{db_name}__restore";' in statements
    assert f'ALTER DATABASE "{db_name}__old" RENAME TO "{db_name}";' in statements
