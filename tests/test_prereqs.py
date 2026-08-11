"""Unit tests for the host-binary startup preflight (``oduflow.prereqs``)."""

from types import SimpleNamespace
from unittest.mock import patch

from oduflow import prereqs


def _completed(returncode: int) -> SimpleNamespace:
    """A stand-in for ``subprocess.CompletedProcess``."""
    return SimpleNamespace(returncode=returncode, stderr=b"boom")


def _which(*present: str):
    """Return a ``shutil.which`` fake where only ``present`` binaries resolve."""

    def _lookup(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in present else None

    return _lookup


class TestEnsureFuseOverlayfs:
    def test_non_linux_is_noop(self):
        with (
            patch.object(prereqs.sys, "platform", "darwin"),
            patch.object(prereqs.subprocess, "run") as run,
            patch.object(prereqs.shutil, "which") as which,
        ):
            prereqs.ensure_fuse_overlayfs()
        run.assert_not_called()
        which.assert_not_called()

    def test_already_installed_is_noop(self):
        with (
            patch.object(prereqs.sys, "platform", "linux"),
            patch.object(prereqs.shutil, "which", _which("fuse-overlayfs")),
            patch.object(prereqs.subprocess, "run") as run,
        ):
            prereqs.ensure_fuse_overlayfs()
        run.assert_not_called()

    def test_missing_but_not_root_warns_without_installing(self, caplog):
        with (
            patch.object(prereqs.sys, "platform", "linux"),
            patch.object(prereqs.shutil, "which", _which("apt-get")),
            patch.object(prereqs.os, "geteuid", return_value=1000),
            patch.object(prereqs.subprocess, "run") as run,
        ):
            with caplog.at_level("WARNING"):
                prereqs.ensure_fuse_overlayfs()
        run.assert_not_called()
        assert "not running as root" in caplog.text

    def test_missing_without_apt_get_warns(self, caplog):
        with (
            patch.object(prereqs.sys, "platform", "linux"),
            patch.object(prereqs.shutil, "which", _which()),  # nothing resolves
            patch.object(prereqs.os, "geteuid", return_value=0),
            patch.object(prereqs.subprocess, "run") as run,
        ):
            with caplog.at_level("WARNING"):
                prereqs.ensure_fuse_overlayfs()
        run.assert_not_called()
        assert "apt-get was not found" in caplog.text

    def test_installs_via_apt_get_when_root(self):
        with (
            patch.object(prereqs.sys, "platform", "linux"),
            patch.object(prereqs.shutil, "which", _which("apt-get")),
            patch.object(prereqs.os, "geteuid", return_value=0),
            patch.object(prereqs.subprocess, "run", return_value=_completed(0)) as run,
        ):
            prereqs.ensure_fuse_overlayfs()
        assert run.call_count == 1
        cmd = run.call_args.kwargs["args"]
        assert cmd == ["apt-get", "install", "-y", "fuse-overlayfs"]

    def test_retries_after_apt_update_on_first_failure(self):
        # First install fails, apt-get update succeeds, second install succeeds.
        with (
            patch.object(prereqs.sys, "platform", "linux"),
            patch.object(prereqs.shutil, "which", _which("apt-get")),
            patch.object(prereqs.os, "geteuid", return_value=0),
            patch.object(
                prereqs.subprocess,
                "run",
                side_effect=[_completed(1), _completed(0), _completed(0)],
            ) as run,
        ):
            prereqs.ensure_fuse_overlayfs()
        commands = [call.kwargs["args"] for call in run.call_args_list]
        assert commands == [
            ["apt-get", "install", "-y", "fuse-overlayfs"],
            ["apt-get", "update"],
            ["apt-get", "install", "-y", "fuse-overlayfs"],
        ]

    def test_install_failure_never_raises(self, caplog):
        # Every apt command fails; the function must swallow it and warn.
        with (
            patch.object(prereqs.sys, "platform", "linux"),
            patch.object(prereqs.shutil, "which", _which("apt-get")),
            patch.object(prereqs.os, "geteuid", return_value=0),
            patch.object(prereqs.subprocess, "run", return_value=_completed(100)),
        ):
            with caplog.at_level("WARNING"):
                prereqs.ensure_fuse_overlayfs()  # must not raise
        assert "Could not auto-install fuse-overlayfs" in caplog.text

    def test_os_error_is_swallowed(self):
        with (
            patch.object(prereqs.sys, "platform", "linux"),
            patch.object(prereqs.shutil, "which", _which("apt-get")),
            patch.object(prereqs.os, "geteuid", return_value=0),
            patch.object(prereqs.subprocess, "run", side_effect=OSError("nope")),
        ):
            prereqs.ensure_fuse_overlayfs()  # must not raise

    def test_timeout_is_swallowed(self, caplog):
        # A hung apt-get (held lock / stalled mirror) must not wedge startup.
        timeout = prereqs.subprocess.TimeoutExpired(cmd="apt-get", timeout=1)
        with (
            patch.object(prereqs.sys, "platform", "linux"),
            patch.object(prereqs.shutil, "which", _which("apt-get")),
            patch.object(prereqs.os, "geteuid", return_value=0),
            patch.object(prereqs.subprocess, "run", side_effect=timeout),
        ):
            with caplog.at_level("WARNING"):
                prereqs.ensure_fuse_overlayfs()  # must not raise
        assert "timed out" in caplog.text


class TestEnsureRsync:
    def test_already_installed_is_noop(self):
        with (
            patch.object(prereqs.sys, "platform", "linux"),
            patch.object(prereqs.shutil, "which", _which("rsync")),
            patch.object(prereqs.subprocess, "run") as run,
        ):
            prereqs.ensure_rsync()
        run.assert_not_called()

    def test_installs_via_apt_get_when_root(self):
        with (
            patch.object(prereqs.sys, "platform", "linux"),
            patch.object(prereqs.shutil, "which", _which("apt-get")),
            patch.object(prereqs.os, "geteuid", return_value=0),
            patch.object(prereqs.subprocess, "run", return_value=_completed(0)) as run,
        ):
            prereqs.ensure_rsync()
        assert run.call_args.kwargs["args"] == ["apt-get", "install", "-y", "rsync"]

    def test_missing_on_non_linux_warns_instead_of_passing_over(self, caplog):
        # Unlike fuse-overlayfs, rsync matters on macOS too: without it every
        # publish re-copies the whole filestore.
        with (
            patch.object(prereqs.sys, "platform", "darwin"),
            patch.object(prereqs.shutil, "which", _which()),
            patch.object(prereqs.subprocess, "run") as run,
        ):
            with caplog.at_level("WARNING"):
                prereqs.ensure_rsync()
        run.assert_not_called()
        assert "rsync is missing" in caplog.text

    def test_present_on_non_linux_is_silent(self, caplog):
        with (
            patch.object(prereqs.sys, "platform", "darwin"),
            patch.object(prereqs.shutil, "which", _which("rsync")),
            patch.object(prereqs.subprocess, "run") as run,
        ):
            with caplog.at_level("WARNING"):
                prereqs.ensure_rsync()
        run.assert_not_called()
        assert caplog.text == ""

    def test_missing_but_not_root_warns_without_installing(self, caplog):
        with (
            patch.object(prereqs.sys, "platform", "linux"),
            patch.object(prereqs.shutil, "which", _which("apt-get")),
            patch.object(prereqs.os, "geteuid", return_value=1000),
            patch.object(prereqs.subprocess, "run") as run,
        ):
            with caplog.at_level("WARNING"):
                prereqs.ensure_rsync()
        run.assert_not_called()
        assert "sudo apt install rsync" in caplog.text

    def test_install_failure_never_raises(self, caplog):
        with (
            patch.object(prereqs.sys, "platform", "linux"),
            patch.object(prereqs.shutil, "which", _which("apt-get")),
            patch.object(prereqs.os, "geteuid", return_value=0),
            patch.object(prereqs.subprocess, "run", return_value=_completed(100)),
        ):
            with caplog.at_level("WARNING"):
                prereqs.ensure_rsync()  # must not raise
        assert "Could not auto-install rsync" in caplog.text
