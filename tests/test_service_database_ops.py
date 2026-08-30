import os
import stat
from unittest.mock import patch

import pytest

from oduflow.docker_ops import service_database_ops
from oduflow.errors import ConflictError, PrerequisiteNotMetError
from oduflow.service_database_credentials import load
from oduflow.settings import Settings, TeamSettings


@pytest.fixture
def database_fixture(tmp_path):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
    settings = Settings(
        base_data_dir=str(tmp_path),
        db_user="admin",
        db_password="secret",
        shared_db_container="oduflow-db",
        teams={"1": team},
    )
    return settings, team


def test_create_persists_private_credentials_and_returns_connection(database_fixture):
    settings, team = database_fixture
    with (
        patch("oduflow.docker_ops.service_database_ops.get_client"),
        patch("oduflow.docker_ops.service_database_ops._wait_pg_ready"),
        patch("oduflow.docker_ops.service_database_ops.ensure_team_network"),
        patch(
            "oduflow.docker_ops.service_database_ops._catalog_exists",
            return_value=False,
        ),
        patch("oduflow.docker_ops.service_database_ops.check_db_quota"),
        patch(
            "oduflow.docker_ops.service_database_ops.ensure_team_tablespace",
            return_value="oduflow_team_1",
        ),
        patch("oduflow.docker_ops.service_database_ops._exec_sql") as exec_sql,
        patch(
            "oduflow.docker_ops.service_database_ops.generate_pg_password",
            return_value="generated-password",
        ),
    ):
        result = service_database_ops.create_database(settings, team, "events")

    assert result["database"] == "oduflow_service_1_events"
    assert result["username"] == "svc_1_events"
    assert result["password"] == "generated-password"
    assert result["url"].startswith("postgresql://svc_1_events:")
    record = load(team, "events")
    assert record["password"] == "generated-password"
    directory = os.path.join(team.data_dir, "service_databases")
    path = os.path.join(directory, "events.json")
    assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    sql = "\n".join(call.args[2] for call in exec_sql.call_args_list)
    assert "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION" in sql
    assert 'TABLESPACE "oduflow_team_1"' in sql
    assert 'REVOKE ALL ON DATABASE "oduflow_service_1_events" FROM PUBLIC' in sql


def test_create_refuses_unmanaged_catalog_drift(database_fixture):
    settings, team = database_fixture
    with (
        patch("oduflow.docker_ops.service_database_ops.get_client"),
        patch("oduflow.docker_ops.service_database_ops._wait_pg_ready"),
        patch("oduflow.docker_ops.service_database_ops.ensure_team_network"),
        patch(
            "oduflow.docker_ops.service_database_ops._catalog_exists", return_value=True
        ),
    ):
        with pytest.raises(ConflictError, match="without matching managed credentials"):
            service_database_ops.create_database(settings, team, "events")


def test_create_rolls_role_back_when_database_creation_fails(database_fixture):
    settings, team = database_fixture
    with (
        patch("oduflow.docker_ops.service_database_ops.get_client"),
        patch("oduflow.docker_ops.service_database_ops._wait_pg_ready"),
        patch("oduflow.docker_ops.service_database_ops.ensure_team_network"),
        patch(
            "oduflow.docker_ops.service_database_ops._catalog_exists",
            return_value=False,
        ),
        patch("oduflow.docker_ops.service_database_ops.check_db_quota"),
        patch(
            "oduflow.docker_ops.service_database_ops.ensure_team_tablespace",
            return_value="oduflow_team_1",
        ),
        patch(
            "oduflow.docker_ops.service_database_ops._exec_sql",
            side_effect=["", RuntimeError("create database failed")],
        ),
        patch("oduflow.docker_ops.service_database_ops._drop_pg_role") as drop_role,
    ):
        with pytest.raises(RuntimeError, match="create database failed"):
            service_database_ops.create_database(settings, team, "events")

    drop_role.assert_called_once()
    assert not os.path.exists(
        os.path.join(team.data_dir, "service_databases", "events.json")
    )


def test_get_masks_password_by_default_and_reports_live_state(database_fixture):
    settings, team = database_fixture
    record = {
        "name": "events",
        "database": "oduflow_service_1_events",
        "username": "svc_1_events",
        "password": "generated-password",
        "created_at": "2026-08-29T00:00:00+00:00",
    }
    from oduflow.service_database_credentials import save

    save(team, "events", record)
    with (
        patch("oduflow.docker_ops.service_database_ops.get_client"),
        patch("oduflow.docker_ops.service_database_ops._wait_pg_ready"),
        patch(
            "oduflow.docker_ops.service_database_ops._catalog_exists", return_value=True
        ),
        patch(
            "oduflow.docker_ops.service_database_ops._exec_sql",
            side_effect=[str(8 * 1024), "2"],
        ),
    ):
        result = service_database_ops.get_database(settings, team, "events")

    assert result["status"] == "ready"
    assert result["size_bytes"] == 8 * 1024
    assert result["connections"] == 2
    assert "password" not in result
    assert "url" not in result


def test_rotate_rolls_postgres_back_when_credentials_write_fails(database_fixture):
    settings, team = database_fixture
    record = {
        "name": "events",
        "database": "oduflow_service_1_events",
        "username": "svc_1_events",
        "password": "old-password",
        "created_at": "2026-08-29T00:00:00+00:00",
    }
    from oduflow.service_database_credentials import save

    save(team, "events", record)
    with (
        patch("oduflow.docker_ops.service_database_ops.get_client"),
        patch("oduflow.docker_ops.service_database_ops._wait_pg_ready"),
        patch(
            "oduflow.docker_ops.service_database_ops._catalog_exists", return_value=True
        ),
        patch(
            "oduflow.docker_ops.service_database_ops.generate_pg_password",
            return_value="new-password",
        ),
        patch(
            "oduflow.docker_ops.service_database_ops.save", side_effect=OSError("disk")
        ),
        patch("oduflow.docker_ops.service_database_ops._exec_sql") as exec_sql,
    ):
        with pytest.raises(OSError, match="disk"):
            service_database_ops.rotate_password(settings, team, "events")

    sql = [call.args[2] for call in exec_sql.call_args_list]
    assert "new-password" in sql[0]
    assert "old-password" in sql[1]


def test_drifted_database_cannot_rotate(database_fixture):
    settings, team = database_fixture
    record = {
        "name": "events",
        "database": "oduflow_service_1_events",
        "username": "svc_1_events",
        "password": "old-password",
        "created_at": "2026-08-29T00:00:00+00:00",
    }
    from oduflow.service_database_credentials import save

    save(team, "events", record)
    with (
        patch("oduflow.docker_ops.service_database_ops.get_client"),
        patch("oduflow.docker_ops.service_database_ops._wait_pg_ready"),
        patch(
            "oduflow.docker_ops.service_database_ops._catalog_exists",
            return_value=False,
        ),
    ):
        with pytest.raises(PrerequisiteNotMetError, match="drifted"):
            service_database_ops.rotate_password(settings, team, "events")
