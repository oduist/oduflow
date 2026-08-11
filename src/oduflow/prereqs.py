"""Best-effort host prerequisite provisioning, run once at server start.

Two host binaries are handled here:

``fuse-overlayfs`` clones large template filestores copy-on-write instead of
duplicating them (see ``docker_ops/env_ops.py``). It is a Linux/FUSE binary — it
does not exist on macOS, where filestore overlays are skipped entirely — so its
absence off Linux is expected and silent.

``rsync`` copies only what changed. Publishing an environment as a template
snapshots its filestore by hardlinking every unchanged file from the baseline
(``docker_ops/system_ops.py``), and syncing a template from a local source uses
it directly (``sync.py``). It is wanted on every platform: without it a publish
degrades to re-copying the whole filestore, and a local template sync fails.

Both are bundled in the packaged Docker image (``oduist/oduflow``), so
auto-install only matters for bare-metal installs (``uv tool`` + systemd running
as root).

Every check is best-effort and idempotent: nothing here raises. When a binary
cannot be provided (non-Linux, not root, non-Debian, no network), the reason is
logged and the caller degrades — a plain filestore copy, or a clear
``PrerequisiteNotMetError`` at overlay mount time on Linux.

Called from ``server._ensure_initialized`` on every server start.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

logger = logging.getLogger("oduflow")

# Upper bound on any single apt-get invocation. Generous enough for a slow
# mirror, but bounded so a hung download or a held apt lock cannot wedge the
# server's startup path.
_APT_TIMEOUT_SECONDS = 300


def ensure_fuse_overlayfs() -> None:
    """Install ``fuse-overlayfs`` via apt-get on Linux if it is missing.

    No-op on non-Linux platforms, when the binary is already present, when not
    running as root, or when ``apt-get`` is unavailable. Never raises — a failure
    to install is logged and left for the mount-time check to surface.
    """
    if not sys.platform.startswith("linux"):
        return  # macOS/other: overlay is unsupported, copy mode is used instead
    _ensure_binary("fuse-overlayfs")


def ensure_rsync() -> None:
    """Install ``rsync`` via apt-get on Linux if it is missing.

    Unlike fuse-overlayfs this is wanted everywhere, so a missing binary is
    reported off Linux too rather than passed over: publishing a template
    silently degrades to copying the whole filestore without it, and syncing a
    template from a local source needs it outright.
    """
    if shutil.which("rsync"):
        return
    if not sys.platform.startswith("linux"):
        logger.warning(
            "rsync is missing; template publishes will copy the whole filestore "
            "instead of only what changed, and local template syncs will fail"
        )
        return
    _ensure_binary("rsync")


def _ensure_binary(binary: str) -> None:
    """Best-effort ``apt-get install`` of *binary* on a Debian/Ubuntu host."""
    if shutil.which(binary):
        return  # already present (Docker image ships it, or installed earlier)
    if os.geteuid() != 0:
        logger.warning(
            "%s is missing and Oduflow is not running as root; "
            "install it manually: sudo apt install %s",
            binary,
            binary,
        )
        return
    if shutil.which("apt-get") is None:
        logger.warning(
            "%s is missing and apt-get was not found; install it with your "
            "distribution's package manager (e.g. dnf install %s)",
            binary,
            binary,
        )
        return

    logger.info("%s is missing; installing via apt-get", binary)
    if _apt_install(binary):
        logger.info("%s installed successfully", binary)
        return

    # Package index may be stale/empty (e.g. fresh minimal image); refresh once
    # and retry a single time.
    logger.info("%s install failed; refreshing apt index and retrying", binary)
    _run_apt(["apt-get", "update"])
    if _apt_install(binary):
        logger.info("%s installed successfully after apt-get update", binary)
    else:
        logger.warning(
            "Could not auto-install %s; install it manually: sudo apt install %s",
            binary,
            binary,
        )


def _apt_install(package: str) -> bool:
    """Run ``apt-get install -y <package>``; return True on success."""
    return _run_apt(["apt-get", "install", "-y", package])


def _run_apt(cmd: list[str]) -> bool:
    """Run an apt command non-interactively; return True on exit code 0.

    Best-effort: any failure (non-zero exit, timeout, missing binary, OS error)
    is logged and reported as ``False`` rather than raised.

    A timeout is enforced so a held dpkg/apt lock (e.g. a concurrent
    apt-daily/unattended-upgrades run right after boot) or a stalled mirror
    cannot block server startup indefinitely, since this runs synchronously in
    ``_ensure_initialized``.
    """
    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
    try:
        result = subprocess.run(
            env=env, args=cmd, capture_output=True, timeout=_APT_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        logger.warning("'%s' timed out after %ds", " ".join(cmd), _APT_TIMEOUT_SECONDS)
        return False
    except OSError as exc:
        logger.warning("'%s' failed: %s", " ".join(cmd), exc)
        return False
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        logger.warning("'%s' exited %d: %s", " ".join(cmd), result.returncode, stderr)
        return False
    return True
