"""Unit tests for backup_ops helpers that need no Docker/S3."""

from __future__ import annotations

import pytest

from oduflow import backup_ops
from oduflow.errors import ExternalCommandError


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
