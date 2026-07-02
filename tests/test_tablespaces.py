from unittest.mock import MagicMock, patch

from oduflow.docker_ops.system_ops import ensure_team_tablespace
from oduflow.migrations import _migrate_team_pg_tablespaces
from oduflow.naming import get_tablespace_name
from oduflow.settings import Settings, TeamSettings


def _team(tmp_path) -> TeamSettings:
    return TeamSettings(team_id="1", data_dir=str(tmp_path / "team_1"))


def _settings(tmp_path, teams=None) -> Settings:
    return Settings(
        base_data_dir=str(tmp_path),
        teams=teams or {"1": _team(tmp_path)},
    )


def test_tablespace_name():
    assert get_tablespace_name("1") == "oduflow_team_1"


class TestEnsureTeamTablespace:
    def test_creates_when_missing(self, tmp_path):
        pg = MagicMock()
        pg.exec_run.return_value = (0, b"")
        client = MagicMock()
        client.containers.get.return_value = pg
        executed = []

        def fake_sql(client_, settings_, sql, db="postgres"):
            executed.append(sql)
            return ""  # not found in pg_tablespace

        with patch("oduflow.docker_ops.system_ops._exec_sql", side_effect=fake_sql):
            name = ensure_team_tablespace(client, _settings(tmp_path), _team(tmp_path))

        assert name == "oduflow_team_1"
        # Host directory created inside the single mounted pg_tablespaces dir.
        assert (tmp_path / "pg_tablespaces" / "team_1").is_dir()
        # Ownership fixed inside the container, then the tablespace created.
        pg.exec_run.assert_any_call(
            ["chown", "postgres:postgres", "/tablespaces/team_1"], user="root"
        )
        assert any("CREATE TABLESPACE" in sql for sql in executed)

    def test_noop_when_exists(self, tmp_path):
        client = MagicMock()
        with patch("oduflow.docker_ops.system_ops._exec_sql", return_value="1"):
            name = ensure_team_tablespace(client, _settings(tmp_path), _team(tmp_path))

        assert name == "oduflow_team_1"
        client.containers.get.assert_not_called()


class TestTablespacesMigration:
    def _run(self, tmp_path, db_rows, has_mount=True):
        """Run the migration against a fake PG container; return executed SQL."""
        pg = MagicMock()
        pg.attrs = {"Mounts": [{"Destination": "/tablespaces"}] if has_mount else []}
        client = MagicMock()
        client.containers.get.return_value = pg
        executed = []

        def fake_sql(client_, settings_, sql, db="postgres"):
            executed.append(sql)
            if "FROM pg_database" in sql:
                return "\n".join(db_rows)
            if "FROM pg_tablespace" in sql:
                return "1"  # tablespace already exists
            return ""

        with (
            patch("oduflow.docker_ops.client.get_client", return_value=client),
            patch("oduflow.docker_ops.system_ops._exec_sql", side_effect=fake_sql),
            patch("oduflow.docker_ops.system_ops._wait_pg_ready"),
            patch("oduflow.docker_ops.system_ops._ensure_pg_container") as ensure_pg,
        ):
            _migrate_team_pg_tablespaces(_settings(tmp_path))
        return executed, pg, ensure_pg

    def test_moves_only_foreign_tablespace_dbs(self, tmp_path):
        executed, pg, ensure_pg = self._run(
            tmp_path,
            db_rows=[
                "postgres|",
                "oduflow_1_main|",  # to move
                "oduflow_template_1_prod|oduflow_team_1",  # already moved
                "oduflow_2_other|",  # another team: not configured here
            ],
        )

        moves = [sql for sql in executed if "SET TABLESPACE" in sql]
        assert moves == [
            'ALTER DATABASE "oduflow_1_main" SET TABLESPACE "oduflow_team_1";'
        ]
        # Connections blocked before the move and restored after.
        assert any("ALLOW_CONNECTIONS false" in sql for sql in executed)
        assert any("ALLOW_CONNECTIONS true" in sql for sql in executed)
        assert any("pg_terminate_backend" in sql for sql in executed)
        # Mount present: the PG container is not recreated.
        pg.stop.assert_not_called()
        ensure_pg.assert_not_called()

    def test_recreates_pg_container_when_mount_missing(self, tmp_path):
        executed, pg, ensure_pg = self._run(
            tmp_path, db_rows=["postgres|"], has_mount=False
        )

        pg.stop.assert_called_once()
        pg.remove.assert_called_once()
        ensure_pg.assert_called_once()
