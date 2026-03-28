"""Shared fixtures for integration tests."""

from __future__ import annotations

import atexit
import os

import pytest

from oduflow.docker_ops import env_ops, system_ops
from oduflow.settings import Settings, TeamSettings

_TEST_PREFIX = "oduflowtest-"
_TEST_NETWORK = "oduflowtest-net"
_TEST_DB_CONTAINER = "oduflowtest-db"
_TEST_DB_VOLUME = "oduflowtest-db-data"


def _test_settings(tmp_dir: str) -> tuple[Settings, TeamSettings]:
    team = TeamSettings(
        team_id="1",
        data_dir=tmp_dir,
        port_registry_path=os.path.join(tmp_dir, "ports.json"),
        port_range_start=51000,
        port_range_end=51100,
    )
    settings = Settings(
        routing_mode="port",
        db_user="odoo",
        db_password="odoo",
        prefix=_TEST_PREFIX,
        shared_network=_TEST_NETWORK,
        shared_db_container=_TEST_DB_CONTAINER,
        shared_db_volume=_TEST_DB_VOLUME,
        base_data_dir=tmp_dir,
        teams={"1": team},
    )
    return settings, team


def _cleanup_test_resources(settings: Settings, team: TeamSettings) -> None:
    """Remove all test containers and system resources."""
    try:
        for env in env_ops.list_environments(settings, team):
            try:
                env_ops.delete_environment(settings, team, env["env_name"])
            except Exception:
                pass
    except Exception:
        pass
    try:
        system_ops.destroy_system(settings)
    except Exception:
        pass


@pytest.fixture(scope="session")
def live_environment(tmp_path_factory):
    """Spin up a full system + main environment, tear down after all tests."""
    tmp_dir = str(tmp_path_factory.mktemp("oduflow"))
    settings, team = _test_settings(tmp_dir)

    # Register atexit cleanup in case pytest is killed or crashes before
    # the yield finalizer runs.
    atexit.register(_cleanup_test_resources, settings, team)

    system_ops.init_system(settings)
    env_ops.create_environment(
        settings,
        team,
        branch="18.0",
        repo_url="https://github.com/oduist/oduflow_test.git",
        odoo_image="odoo:18.0",
    )

    yield settings, team

    _cleanup_test_resources(settings, team)
    atexit.unregister(_cleanup_test_resources)
