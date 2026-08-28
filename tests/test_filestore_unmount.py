"""Unit tests for overlay filestore unmount order in env_ops.

The overlay unmount must not depend on the setuid ``fusermount`` helper: the
AppArmor ``fusermount3`` profile shipped on Ubuntu 24.04+ DENIES its ``umount``
operation, which previously forced Oduflow onto the lazy ``umount -l`` fallback
(it detaches even a busy mount, leaving a still-bound container with a broken
filestore). Oduflow runs as root, so a direct ``umount`` — not mediated by that
profile — must be tried first.
"""

from __future__ import annotations

import os
import subprocess
from unittest.mock import MagicMock

from oduflow.docker_ops import env_ops
from oduflow.naming import get_filestore_paths


def _team(workspaces_dir: str):
    team = MagicMock()
    team.workspaces_dir = workspaces_dir
    return team


def _mount_present(monkeypatch, merged: str) -> None:
    """Make the merged dir look like a live mountpoint.

    The directory has to exist: liveness is decided by stat'ing the mountpoint
    (a stale FUSE mount is the case where that stat fails), not by ``ismount``
    alone.
    """
    os.makedirs(merged, exist_ok=True)
    monkeypatch.setattr(env_ops.os.path, "ismount", lambda p: True)


class TestUnmountFilestore:
    def test_tries_clean_umount_first(self, tmp_path, monkeypatch):
        merged = get_filestore_paths("env1", str(tmp_path))["merged"]
        _mount_present(monkeypatch, merged)
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return MagicMock(returncode=0)  # first attempt succeeds

        monkeypatch.setattr(env_ops.subprocess, "run", fake_run)

        env_ops._unmount_filestore("env1", _team(str(tmp_path)))

        # Clean `umount` is first — and since it succeeds, nothing else runs
        # (in particular, the AppArmor-blocked fusermount helper is never used).
        assert calls == [["umount", merged]]

    def test_falls_back_through_helpers_to_lazy(self, tmp_path, monkeypatch):
        merged = get_filestore_paths("env1", str(tmp_path))["merged"]
        _mount_present(monkeypatch, merged)
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            # Clean umount and both fusermount helpers fail; only lazy succeeds.
            if cmd == ["umount", "-l", merged]:
                return MagicMock(returncode=0)
            raise subprocess.CalledProcessError(1, cmd)

        monkeypatch.setattr(env_ops.subprocess, "run", fake_run)

        env_ops._unmount_filestore("env1", _team(str(tmp_path)))

        assert calls == [
            ["umount", merged],
            ["fusermount3", "-u", merged],
            ["fusermount", "-u", merged],
            ["umount", "-l", merged],
        ]

    def test_noop_when_not_mounted(self, tmp_path, monkeypatch):
        merged = get_filestore_paths("env1", str(tmp_path))["merged"]
        os.makedirs(merged, exist_ok=True)
        monkeypatch.setattr(env_ops.os.path, "ismount", lambda p: False)
        called = []
        monkeypatch.setattr(env_ops.subprocess, "run", lambda *a, **k: called.append(a))

        env_ops._unmount_filestore("env1", _team(str(tmp_path)))

        assert called == []
