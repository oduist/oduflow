"""The generated unit and its needrestart override.

An unattended-upgrades run once restarted Oduflow in the same batch as
containerd and left it wedged before the HTTP listener bound. The unit's
ordering plus a needrestart exclusion are what keep that from repeating.
"""

from __future__ import annotations

from unittest.mock import patch

from oduflow import systemd


def test_unit_is_ordered_after_the_container_runtime():
    unit = systemd.UNIT_TEMPLATE.format(oduflow_bin="/usr/local/bin/oduflow")
    assert "After=docker.service containerd.service network-online.target" in unit
    # Restart=always: the startup watchdog exits on purpose when Docker wedges.
    assert "Restart=always" in unit
    # No start-rate limit — a service parked in `failed` is a silent outage.
    assert "StartLimitIntervalSec=0" in unit


def test_install_writes_the_needrestart_override(tmp_path):
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    needrestart_dir = tmp_path / "needrestart"
    needrestart_dir.mkdir()  # needrestart is installed on this host

    with (
        patch.object(systemd, "UNIT_DIR", unit_dir),
        patch.object(systemd, "NEEDRESTART_DIR", needrestart_dir),
        patch.object(
            systemd, "NEEDRESTART_CONF", needrestart_dir / "conf.d" / "oduflow.conf"
        ),
        patch.object(systemd, "_find_oduflow_bin", return_value="/usr/bin/oduflow"),
        patch.object(systemd, "_systemctl"),
        patch("os.geteuid", return_value=0),
    ):
        systemd.install()

        conf = (needrestart_dir / "conf.d" / "oduflow.conf").read_text()

    assert r"$nrconf{override_rc}{qr(^oduflow\.service$)} = 0;" in conf
    assert (unit_dir / systemd.SERVICE_NAME).exists()


def test_install_skips_the_override_when_needrestart_is_absent(tmp_path):
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    absent = tmp_path / "needrestart"  # not created

    with (
        patch.object(systemd, "UNIT_DIR", unit_dir),
        patch.object(systemd, "NEEDRESTART_DIR", absent),
        patch.object(systemd, "NEEDRESTART_CONF", absent / "conf.d" / "oduflow.conf"),
        patch.object(systemd, "_find_oduflow_bin", return_value="/usr/bin/oduflow"),
        patch.object(systemd, "_systemctl"),
        patch("os.geteuid", return_value=0),
    ):
        systemd.install()

    assert not absent.exists()


def test_uninstall_removes_the_override(tmp_path):
    unit_dir = tmp_path / "systemd"
    unit_dir.mkdir()
    (unit_dir / systemd.SERVICE_NAME).write_text("unit")
    conf = tmp_path / "needrestart" / "conf.d" / "oduflow.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text(systemd.NEEDRESTART_SNIPPET)

    with (
        patch.object(systemd, "UNIT_DIR", unit_dir),
        patch.object(systemd, "NEEDRESTART_CONF", conf),
        patch.object(systemd, "_systemctl"),
        patch("subprocess.run"),
        patch("os.geteuid", return_value=0),
    ):
        systemd.uninstall()

    assert not conf.exists()
    assert not (unit_dir / systemd.SERVICE_NAME).exists()
