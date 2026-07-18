"""Generate and manage a systemd service unit for Oduflow."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

UNIT_DIR = Path("/etc/systemd/system")

SERVICE_NAME = "oduflow.service"

UNIT_TEMPLATE = """\
[Unit]
Description=Oduflow MCP Server
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=simple
ExecStart={oduflow_bin} --transport http
ExecReload=/bin/kill -HUP $MAINPID
WorkingDirectory=/
Restart=on-failure
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

    if not unit_path.exists():
        print(f"Service file {unit_path} does not exist. Nothing to remove.")
        return

    subprocess.run(["systemctl", "stop", SERVICE_NAME], check=False)
    subprocess.run(["systemctl", "disable", SERVICE_NAME], check=False)
    unit_path.unlink()
    _systemctl("daemon-reload")

    print(f"Service '{SERVICE_NAME}' stopped, disabled, and removed.")
