"""Generate and manage a systemd service unit for Oduflow."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

UNIT_DIR = Path("/etc/systemd/system")

SERVICE_NAME = "oduflow.service"

# needrestart (run by unattended-upgrades after a library upgrade) restarts
# every affected service in one batch. Oduflow drives the Docker daemon, so
# being restarted alongside containerd/docker means its startup races a daemon
# that is itself going down — observed to leave Oduflow blocked in a Docker call
# with no HTTP listener until someone restarted it by hand.
NEEDRESTART_DIR = Path("/etc/needrestart")
NEEDRESTART_CONF = NEEDRESTART_DIR / "conf.d" / "oduflow.conf"

NEEDRESTART_SNIPPET = """\
# Installed by `oduflow systemd-install`.
#
# Oduflow orchestrates Docker. Letting needrestart restart oduflow.service in
# the same batch as containerd/docker races the daemon restart and can wedge
# Oduflow's startup. Never restart it automatically: needrestart lists it for a
# manual `systemctl restart oduflow` instead.
$nrconf{override_rc}{qr(^oduflow\\.service$)} = 0;
"""

UNIT_TEMPLATE = """\
[Unit]
Description=Oduflow MCP Server
After=docker.service containerd.service network-online.target
Requires=docker.service
Wants=network-online.target
# Never give up on restarting: a host that upgrades Docker underneath Oduflow
# can produce several restarts in a row, and a service left in `failed` is a
# silent outage.
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart={oduflow_bin} --transport http
WorkingDirectory=/
# `always`, not `on-failure`: Oduflow's startup watchdog exits deliberately when
# a Docker call wedges, and that recovery must survive any exit reason.
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=oduflow

[Install]
WantedBy=multi-user.target
"""


def _find_oduflow_bin() -> str:
    """Locate the oduflow executable."""
    path = shutil.which("oduflow")
    if path:
        return path
    # Fallback: common uv tool install location for root
    root_bin = Path("/root/.local/bin/oduflow")
    if root_bin.exists():
        return str(root_bin)
    return ""


def _systemctl(*args: str) -> None:
    subprocess.run(["systemctl", *args], check=True)


def _install_needrestart_override() -> Path | None:
    """Exclude oduflow.service from needrestart's automatic restarts.

    Returns the written path, or None when needrestart is not installed (there
    is nothing to configure, and creating its config tree would be misleading).
    """
    if not NEEDRESTART_DIR.is_dir():
        return None
    NEEDRESTART_CONF.parent.mkdir(parents=True, exist_ok=True)
    NEEDRESTART_CONF.write_text(NEEDRESTART_SNIPPET)
    return NEEDRESTART_CONF


def install() -> None:
    """Generate, install, and enable the Oduflow systemd service."""
    if os.geteuid() != 0:
        print("Error: systemd-install must be run as root.")
        sys.exit(1)

    oduflow_bin = _find_oduflow_bin()
    if not oduflow_bin:
        print(
            "Error: 'oduflow' binary not found in PATH.\n"
            "Install it first:  uv tool install oduflow"
        )
        sys.exit(1)

    unit_content = UNIT_TEMPLATE.format(oduflow_bin=oduflow_bin)

    unit_path = UNIT_DIR / SERVICE_NAME
    unit_path.write_text(unit_content)
    print(f"Unit file written to {unit_path}")

    needrestart_path = _install_needrestart_override()
    if needrestart_path:
        print(
            f"needrestart override written to {needrestart_path} "
            "(Oduflow is excluded from automatic restarts, so a Docker upgrade "
            "cannot restart it in the same batch as containerd)"
        )

    _systemctl("daemon-reload")
    _systemctl("enable", SERVICE_NAME)

    print(f"\nService '{SERVICE_NAME}' enabled.")
    print(f"  Start:   systemctl start {SERVICE_NAME}")
    print(f"  Status:  systemctl status {SERVICE_NAME}")
    print("  Logs:    journalctl -u oduflow -f")


def uninstall() -> None:
    """Stop, disable, and remove the Oduflow systemd service."""
    if os.geteuid() != 0:
        print("Error: systemd-uninstall must be run as root.")
        sys.exit(1)

    unit_path = UNIT_DIR / SERVICE_NAME

    if NEEDRESTART_CONF.exists():
        NEEDRESTART_CONF.unlink()
        print(f"Removed needrestart override {NEEDRESTART_CONF}")

    if not unit_path.exists():
        print(f"Service file {unit_path} does not exist. Nothing to remove.")
        return

    subprocess.run(["systemctl", "stop", SERVICE_NAME], check=False)
    subprocess.run(["systemctl", "disable", SERVICE_NAME], check=False)
    unit_path.unlink()
    _systemctl("daemon-reload")

    print(f"Service '{SERVICE_NAME}' stopped, disabled, and removed.")
