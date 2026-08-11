"""Streaming dump/extract paths in system_ops.

Dumps used to be written three times at full size (pg_dump into the container's
writable layer, get_archive into a host temp tar, then the final file). These
tests pin the streaming replacements: the payload must land only at its
destination, stderr must never reach the payload bytes, and a non-zero exit must
not leave a truncated dump behind looking like a success.
"""

import io
import os
import tarfile
from unittest.mock import MagicMock, patch

import pytest

from oduflow.docker_ops import system_ops
from oduflow.errors import ExternalCommandError
from oduflow.settings import Settings, TeamSettings

TEST_SETTINGS = Settings()
TEST_TEAM = TeamSettings(team_id="1", data_dir="/tmp/oduflow-test-team")


def _tar_blob(entries: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _FakeArchiveContainer:
    """Container whose get_archive hands back many small chunks."""

    def __init__(self, blob: bytes, chunk_size: int = 7) -> None:
        self._blob = blob
        self._chunk_size = chunk_size
        self.archive_calls = 0

    def get_archive(self, path: str):
        self.archive_calls += 1
        chunks = (
            self._blob[i : i + self._chunk_size]
            for i in range(0, len(self._blob), self._chunk_size)
        )
        return chunks, {}


class _FakeExecApi:
    def __init__(self, frames, exit_codes=(0,)) -> None:
        self.frames = frames
        self.exit_codes = list(exit_codes)
        self.cmd = None
        self.create_kwargs: dict = {}
        self.start_kwargs: dict = {}

    def exec_create(self, container_id, cmd, **kwargs):
        self.cmd = cmd
        self.create_kwargs = kwargs
        return {"Id": "exec-1"}

    def exec_start(self, exec_id, **kwargs):
        self.start_kwargs = kwargs
        return iter(self.frames)

    def exec_inspect(self, exec_id):
        code = (
            self.exit_codes.pop(0) if len(self.exit_codes) > 1 else self.exit_codes[0]
        )
        return {"ExitCode": code}


def _fake_exec_client(frames, exit_codes=(0,)):
    client = MagicMock()
    api = _FakeExecApi(frames, exit_codes)
    client.api = api
    container = MagicMock()
    container.id = "db-container"
    return client, api, container


# --------------------------------------------------------------------------
# _ChunkReader
# --------------------------------------------------------------------------


def test_chunk_reader_reassembles_across_chunk_boundaries():
    payload = bytes(range(256)) * 4
    reader = io.BufferedReader(
        system_ops._ChunkReader(payload[i : i + 5] for i in range(0, len(payload), 5))
    )
    assert reader.read() == payload


def test_chunk_reader_reports_eof_on_exhausted_iterator():
    reader = system_ops._ChunkReader(iter([]))
    assert reader.readinto(bytearray(16)) == 0


# --------------------------------------------------------------------------
# _copy_file_from_container / _extract_archive_from_container
# --------------------------------------------------------------------------


def test_copy_file_from_container_streams_payload(tmp_path):
    payload = b"PGDMP" + os.urandom(5000)
    container = _FakeArchiveContainer(_tar_blob([("dump.pgdump", payload)]))
    dest = tmp_path / "dump.pgdump"

    system_ops._copy_file_from_container(container, "/tmp/dump.pgdump", str(dest))

    assert dest.read_bytes() == payload
    assert container.archive_calls == 1


def test_copy_file_from_container_raises_when_archive_has_no_file(tmp_path):
    container = _FakeArchiveContainer(_tar_blob([]))

    with pytest.raises(ExternalCommandError):
        system_ops._copy_file_from_container(
            container, "/tmp/missing", str(tmp_path / "out")
        )


def test_extract_archive_streams_and_strips_prefix(tmp_path):
    prefix = "Odoo/filestore/build_db/"
    blob = _tar_blob(
        [
            (prefix + "aa/hash1", b"one"),
            (prefix + "bb/hash2", b"two"),
        ]
    )
    container = _FakeArchiveContainer(blob)

    extracted = system_ops._extract_archive_from_container(
        container, "/var/lib/odoo", str(tmp_path), prefix
    )

    assert extracted == 2
    assert (tmp_path / "aa/hash1").read_bytes() == b"one"
    assert (tmp_path / "bb/hash2").read_bytes() == b"two"


def test_extract_archive_still_rejects_traversal_members(tmp_path):
    # The traversal guard must survive the switch to sequential ("r|") reading.
    prefix = "Odoo/filestore/build_db/"
    dest = tmp_path / "filestore"
    dest.mkdir()
    blob = _tar_blob(
        [
            (prefix + "safe", b"ok"),
            (prefix + "../../../escaped", b"pwned"),
        ]
    )
    container = _FakeArchiveContainer(blob)

    extracted = system_ops._extract_archive_from_container(
        container, "/var/lib/odoo", str(dest), prefix
    )

    assert extracted == 1
    assert (dest / "safe").read_bytes() == b"ok"
    assert not (tmp_path / "escaped").exists()


# --------------------------------------------------------------------------
# _stream_exec_to_file
# --------------------------------------------------------------------------


def test_stream_exec_writes_stdout_and_drops_stderr(tmp_path):
    dest = tmp_path / "dump.pgdump"
    frames = [
        (b"PGDMP", None),
        (None, b"pg_dump: warning: something noisy\n"),
        (b"\x01\x02\x03", None),
    ]
    client, api, container = _fake_exec_client(frames)

    written = system_ops._stream_exec_to_file(
        client, container, ["pg_dump", "db"], str(dest), tool="pg_dump"
    )

    assert dest.read_bytes() == b"PGDMP\x01\x02\x03"
    assert written == 8
    # A TTY would stop the daemon multiplexing and corrupt the binary payload.
    assert api.create_kwargs["tty"] is False
    assert api.start_kwargs["demux"] is True
    assert api.start_kwargs["stream"] is True


def test_stream_exec_raises_and_leaves_no_partial_file(tmp_path):
    dest = tmp_path / "dump.pgdump"
    frames = [(b"trunc", None), (None, b"pg_dump: error: connection lost\n")]
    client, api, container = _fake_exec_client(frames, exit_codes=(1,))

    with pytest.raises(ExternalCommandError, match="connection lost"):
        system_ops._stream_exec_to_file(
            client, container, ["pg_dump", "db"], str(dest), tool="pg_dump"
        )

    assert not dest.exists()
    assert list(tmp_path.iterdir()) == []


def test_stream_exec_tolerates_lagging_exit_code(tmp_path):
    dest = tmp_path / "dump.pgdump"
    client, api, container = _fake_exec_client([(b"data", None)], exit_codes=(None, 0))

    system_ops._stream_exec_to_file(
        client, container, ["pg_dump", "db"], str(dest), tool="pg_dump"
    )

    assert dest.read_bytes() == b"data"


def test_stream_exec_fails_when_exit_code_never_arrives(tmp_path):
    dest = tmp_path / "dump.pgdump"
    client, api, container = _fake_exec_client([(b"data", None)], exit_codes=(None,))

    with patch.object(system_ops, "_EXEC_EXIT_POLL_SECONDS", 0.0):
        with pytest.raises(ExternalCommandError):
            system_ops._stream_exec_to_file(
                client, container, ["pg_dump", "db"], str(dest), tool="pg_dump"
            )

    assert not dest.exists()


# --------------------------------------------------------------------------
# parallel restore
# --------------------------------------------------------------------------


def test_pg_restore_jobs_is_bounded():
    for cpus in (None, 1, 2, 8, 64):
        with patch.object(system_ops.os, "cpu_count", return_value=cpus):
            assert 1 <= system_ops._pg_restore_jobs() <= 4


def _reload_template_restore_cmds(tmp_path, *, is_text_dump: bool, jobs: int = 4):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path))
    tpl_dir = team.get_template_dir("mytpl")
    os.makedirs(tpl_dir, exist_ok=True)
    dump = os.path.join(tpl_dir, "dump.pgdump")
    with open(dump, "wb") as fh:
        fh.write(b"PGDMP" if not is_text_dump else b"-- sql")

    db_container = MagicMock()
    db_container.exec_run.return_value = (0, b"")
    client = MagicMock()
    client.containers.get.return_value = db_container

    with (
        patch.object(system_ops, "get_client", return_value=client),
        patch.object(system_ops, "_wait_pg_ready"),
        patch.object(system_ops, "_exec_sql", return_value="5"),
        patch.object(system_ops, "_db_exists", return_value=False),
        patch.object(system_ops, "_is_text_dump", return_value=is_text_dump),
        patch.object(
            system_ops, "ensure_team_tablespace", return_value="oduflow_team_1"
        ),
        patch.object(system_ops, "_copy_file_to_container"),
        patch.object(system_ops, "_update_template_sizes"),
        patch.object(system_ops, "_pg_restore_jobs", return_value=jobs),
    ):
        system_ops.reload_template(TEST_SETTINGS, team, "mytpl")

    return [call.args[0] for call in db_container.exec_run.call_args_list]


def test_custom_dump_restore_runs_parallel(tmp_path):
    cmds = _reload_template_restore_cmds(tmp_path, is_text_dump=False)
    restore = next(cmd for cmd in cmds if cmd[0] == "pg_restore")

    assert "-j" in restore
    assert restore[restore.index("-j") + 1] == "4"
    # The archive path must stay last: -j is inserted before it, not after.
    assert restore[-1].startswith("/tmp/")


def test_single_cpu_restore_omits_jobs_flag(tmp_path):
    cmds = _reload_template_restore_cmds(tmp_path, is_text_dump=False, jobs=1)
    restore = next(cmd for cmd in cmds if cmd[0] == "pg_restore")

    assert "-j" not in restore


def test_plain_sql_restore_never_uses_jobs(tmp_path):
    # psql has no -j; only seekable custom-format archives can restore in parallel.
    cmds = _reload_template_restore_cmds(tmp_path, is_text_dump=True)
    restore = next(cmd for cmd in cmds if cmd[0] == "psql")

    assert "-j" not in restore
