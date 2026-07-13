"""Best-effort host prerequisite provisioning, run once at server start.

The only prerequisite handled here is ``fuse-overlayfs``, used to clone large
template filestores copy-on-write instead of duplicating them (see
``docker_ops/env_ops.py``). It is a Linux/FUSE binary — it does not exist on
macOS, and the packaged Docker image (``oduist/oduflow``) already bundles it —
so auto-install only matters for bare-metal Linux installs (``uv tool`` +
systemd running as root).

Every check is best-effort and idempotent: nothing here raises. When the binary
cannot be provided (non-Linux, not root, non-Debian, no network), the reason is
logged and env creation falls back to a plain filestore copy on macOS or surfaces
a clear ``PrerequisiteNotMetError`` on Linux at mount time.

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
    if shutil.which("fuse-overlayfs"):
        return  # already present (Docker image ships it, or installed earlier)
    if os.geteuid() != 0:
        logger.warning(
            "fuse-overlayfs is missing and Oduflow is not running as root; "
            "install it manually: sudo apt install fuse-overlayfs"
        )
        return
    if shutil.which("apt-get") is None:
        logger.warning(
            "fuse-overlayfs is missing and apt-get was not found; install it "
            "with your distribution's package manager (e.g. dnf install "
            "fuse-overlayfs)"
        )
        return

    logger.info("fuse-overlayfs is missing; installing via apt-get")
    if _apt_install("fuse-overlayfs"):
        logger.info("fuse-overlayfs installed successfully")
        return

    # Package index may be stale/empty (e.g. fresh minimal image); refresh once
    # and retry a single time.
    logger.info("fuse-overlayfs install failed; refreshing apt index and retrying")
    _run_apt(["apt-get", "update"])
    if _apt_install("fuse-overlayfs"):
        logger.info("fuse-overlayfs installed successfully after apt-get update")
    else:
        logger.warning(
            "Could not auto-install fuse-overlayfs; install it manually: "
            "sudo apt install fuse-overlayfs"
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
