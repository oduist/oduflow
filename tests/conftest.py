"""Shared fixtures for integration tests."""

from __future__ import annotations

import os

import pytest

from oduflow.docker_ops import env_ops, system_ops
from oduflow.settings import Settings

_TEST_PREFIX = "oduflowtest-"
_TEST_NETWORK = "oduflowtest-net"
_TEST_DB_CONTAINER = "oduflowtest-db"
_TEST_DB_VOLUME = "oduflowtest-db-data"


def _test_settings(tmp_dir: str) -> Settings:
    return Settings(
        routing_mode="port",
        external_host="localhost",
        port_range_start=51000,
        port_range_end=51100,
        workspaces_dir=os.path.join(tmp_dir, "workspaces"),
        data_dir=tmp_dir,
        db_user="odoo",
        db_password="odoo",
        prefix=_TEST_PREFIX,
        shared_network=_TEST_NETWORK,
        shared_db_container=_TEST_DB_CONTAINER,
        shared_db_volume=_TEST_DB_VOLUME,
        port_registry_path=os.path.join(tmp_dir, "ports.json"),
    )


@pytest.fixture(scope="session")
def live_environment(tmp_path_factory):
    """Spin up a full system + main environment, tear down after all tests."""
    tmp_dir = str(tmp_path_factory.mktemp("oduflow"))
    settings = _test_settings(tmp_dir)

    template_dir = os.path.join(tmp_dir, "templates", "default")
    os.makedirs(template_dir, exist_ok=True)
    dump_file = os.path.join(template_dir, "dump.pgdump")

    real_dump = "/srv/oduflow_data/templates/default/dump.pgdump"
    alt_dump = "/srv/oduflow_data/dump/dump.pgdump"
    real_dump_sql = "/srv/oduflow_data/templates/default/dump.sql"
    alt_dump_sql = "/srv/oduflow_data/dump/dump.sql"
    if os.path.isfile(real_dump):
        import shutil
        shutil.copy2(real_dump, dump_file)
    elif os.path.isfile(alt_dump):
        import shutil
        shutil.copy2(alt_dump, dump_file)
    elif os.path.isfile(real_dump_sql):
        import shutil
        shutil.copy2(real_dump_sql, os.path.join(template_dir, "dump.sql"))
    elif os.path.isfile(alt_dump_sql):
        import shutil
        shutil.copy2(alt_dump_sql, os.path.join(template_dir, "dump.sql"))
    else:
        with open(dump_file, "w") as f:
            f.write("-- empty test dump\n")

    system_ops.init_system(settings)
    system_ops.reload_template(settings, template_name="default")
    env_ops.create_environment(
        settings,
        branch_name="main",
        repo_url="https://github.com/oduist/flow_test_addons.git",
        odoo_image="odoo:15.0",
    )

    yield settings

    try:
        env_ops.delete_environment(settings, "main")
    except Exception:
        pass
    try:
        system_ops.destroy_system(settings)
    except Exception:
        pass
