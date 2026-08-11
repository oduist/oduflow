"""Unit tests for Odoo native database neutralization in sanitizer.

Covers ``neutralize_environment``: the odoo-bin neutralize invocation inside the
serving container, success/failure logging, and graceful handling when the
container is missing. Neutralization must never abort provisioning.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import docker as _docker
from oduflow import sanitizer
from oduflow.naming import get_db_name, get_resource_name


def _settings():
    s = MagicMock()
    s.prefix = "oduflow"
    s.image_label = "oduflow.image"
    return s


def _team():
    t = MagicMock()
    t.team_id = "1"
    return t


def _neutralize_cmd(container) -> str | None:
    """Return the odoo neutralize command passed to exec_run, if any."""
    for call in container.exec_run.call_args_list:
        cmd = call.args[0] if call.args else call.kwargs.get("cmd", "")
        if isinstance(cmd, str) and "neutralize" in cmd:
            return cmd
    return None


@pytest.mark.parametrize(
    "image,expected",
    [
        ("odoo:15.0", 15),
        ("registry.example:5000/acme/odoo-enterprise:15.0-custom", 15),
        ("ghcr.io/acme/odoo-16", 16),
        ("ghcr.io/acme/platform:latest", None),
    ],
)
def test_detect_odoo_major_from_official_and_custom_images(image, expected):
    container = MagicMock()
    container.labels = {"oduflow.image": image}
    assert (
        sanitizer._detect_odoo_major_from_container(container, "oduflow.image")
        == expected
    )


class TestNeutralizeEnvironment:
    def test_odoo_15_skips_native_neutralize(self):
        container = MagicMock()
        container.labels = {"oduflow.image": "odoo:15.0"}
        client = MagicMock()
        client.containers.get.return_value = container

        logs = sanitizer.neutralize_environment(
            client, _settings(), _team(), "feature/foo"
        )

        container.exec_run.assert_not_called()
        assert any("Skipped" in line and "Odoo 15" in line for line in logs)
        assert not any("WARNING" in line for line in logs)

    def test_odoo_16_is_the_first_version_that_neutralizes(self):
        # 15 is the last release without the native CLI command; 16 must run it.
        # Guards the `major <= 15` boundary from both sides.
        container = MagicMock()
        container.labels = {"oduflow.image": "odoo:16.0"}
        container.exec_run.return_value = (0, b"Neutralization finished")
        client = MagicMock()
        client.containers.get.return_value = container

        logs = sanitizer.neutralize_environment(
            client, _settings(), _team(), "feature/foo"
        )

        assert _neutralize_cmd(container) is not None
        assert not any("Skipped" in line for line in logs)

    def test_odoo_14_is_also_skipped(self):
        container = MagicMock()
        container.labels = {"oduflow.image": "odoo:14.0"}
        client = MagicMock()
        client.containers.get.return_value = container

        logs = sanitizer.neutralize_environment(
            client, _settings(), _team(), "feature/foo"
        )

        container.exec_run.assert_not_called()
        assert any("Skipped" in line for line in logs)

    def test_unknown_version_still_attempts_neutralize(self):
        # No usable image label -> major is None -> do not skip. Neutralizing
        # and failing is safer than silently leaving a live database.
        container = MagicMock()
        container.labels = {}
        container.exec_run.return_value = (0, b"Neutralization finished")
        client = MagicMock()
        client.containers.get.return_value = container

        logs = sanitizer.neutralize_environment(
            client, _settings(), _team(), "feature/foo"
        )

        assert _neutralize_cmd(container) is not None
        assert not any("Skipped" in line for line in logs)

    def test_runs_neutralize_command_in_container(self):
        container = MagicMock()
        container.exec_run.return_value = (0, b"Neutralization finished")
        client = MagicMock()
        client.containers.get.return_value = container
        settings, team = _settings(), _team()

        logs = sanitizer.neutralize_environment(client, settings, team, "feature/foo")

        # Correct container looked up.
        client.containers.get.assert_called_once_with(
            get_resource_name("feature/foo", "odoo", settings.prefix, team.team_id)
        )
        # Command is `odoo neutralize -d <db>` (subcommand first) with the right DB.
        cmd = _neutralize_cmd(container)
        assert cmd is not None
        assert "odoo neutralize" in cmd
        assert f"-d {get_db_name('feature/foo', team.team_id)}" in cmd
        assert any("neutralized" in line.lower() for line in logs)
        assert not any("WARNING" in line for line in logs)

    def test_nonzero_exit_is_warned_not_raised(self):
        container = MagicMock()
        container.exec_run.return_value = (1, b"THE DATABASE IS NOT NEUTRALIZED!")
        client = MagicMock()
        client.containers.get.return_value = container

        logs = sanitizer.neutralize_environment(
            client, _settings(), _team(), "feature/foo"
        )

        assert any("WARNING" in line and "exit 1" in line for line in logs)

    def test_missing_container_is_skipped_gracefully(self):
        client = MagicMock()
        client.containers.get.side_effect = _docker.errors.NotFound("nope")

        logs = sanitizer.neutralize_environment(
            client, _settings(), _team(), "feature/foo"
        )

        assert any("WARNING" in line and "container not found" in line for line in logs)

    def test_exec_exception_never_aborts(self):
        container = MagicMock()
        container.exec_run.side_effect = RuntimeError("docker exploded")
        client = MagicMock()
        client.containers.get.return_value = container

        # Must not raise — provisioning should continue even if neutralize fails.
        logs = sanitizer.neutralize_environment(
            client, _settings(), _team(), "feature/foo"
        )

        assert any("WARNING" in line for line in logs)
