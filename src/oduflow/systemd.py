"""Generate and manage a systemd service unit for Oduflow."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

UNIT_DIR = Path("/etc/systemd/system")
ETC_DIR = Path("/etc/oduflow")

UNIT_TEMPLATE = """\
[Unit]
Description=Oduflow MCP Server (instance {instance_id})
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=simple
ExecStart={oduflow_bin} run-instance --instance {instance_id}
WorkingDirectory=/
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier={syslog_id}

[Install]
WantedBy=multi-user.target
"""


def _service_name(instance_id: str) -> str:
    """Return systemd service name based on instance ID."""
    if instance_id == "1":
        return "oduflow.service"
    return f"oduflow-{instance_id}.service"


def env_file_path(instance_id: str) -> Path:
    """Return the canonical .env path for an instance."""
    return ETC_DIR / f"instance_{instance_id}.env"


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


def install(instance_id: str) -> None:
    """Generate, install, and enable a systemd service for Oduflow."""
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

    env_file = env_file_path(instance_id)
    if not env_file.exists():
        print(f"Warning: {env_file} does not exist.")
        print("Run 'oduflow init-instance' first, or create it manually.")

    service = _service_name(instance_id)
    syslog_id = service.removesuffix(".service")

    unit_content = UNIT_TEMPLATE.format(
        instance_id=instance_id,
        oduflow_bin=oduflow_bin,
        syslog_id=syslog_id,
    )

    unit_path = UNIT_DIR / service
    unit_path.write_text(unit_content)
    print(f"Unit file written to {unit_path}")

    _systemctl("daemon-reload")
    _systemctl("enable", service)

    print(f"\nService '{service}' enabled.")
    print(f"  Start:   systemctl start {service}")
    print(f"  Status:  systemctl status {service}")
    print(f"  Logs:    journalctl -u {syslog_id} -f")


def uninstall(instance_id: str) -> None:
    """Stop, disable, and remove the Oduflow systemd service."""
    if os.geteuid() != 0:
        print("Error: systemd-uninstall must be run as root.")
        sys.exit(1)

    service = _service_name(instance_id)
    unit_path = UNIT_DIR / service

    if not unit_path.exists():
        print(f"Service file {unit_path} does not exist. Nothing to remove.")
        return

    subprocess.run(["systemctl", "stop", service], check=False)
    subprocess.run(["systemctl", "disable", service], check=False)
    unit_path.unlink()
    _systemctl("daemon-reload")

    print(f"Service '{service}' stopped, disabled, and removed.")
