from unittest.mock import MagicMock, patch

import docker
import pytest

from oduflow import production_registry
from oduflow.docker_ops import system_ops
from oduflow.settings import Settings, TeamSettings


@pytest.fixture
def settings(tmp_path):
    team_dir = tmp_path / "team_1"
    team_dir.mkdir()
    return Settings(
        base_data_dir=str(tmp_path),
        etc_dir=str(tmp_path / "etc"),
        teams={"1": TeamSettings(team_id="1", data_dir=str(team_dir))},
    )


def _client_without_prod():
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("nf")
    client.volumes.get.side_effect = docker.errors.NotFound("nf")
    return client


class TestLazyProvisioning:
    def test_noop_without_productions(self, settings):
        client = _client_without_prod()
        assert system_ops.ensure_prod_infra(client, settings) is False
        client.containers.run.assert_not_called()
        client.volumes.create.assert_not_called()

    def test_provisions_when_registry_has_production(self, settings):
        production_registry.create_production(settings.teams["1"], "erp", {})
        client = _client_without_prod()
        with (
            patch("oduflow.walg.ensure_walg"),
            patch.object(system_ops, "_wait_pg_ready"),
            patch.object(system_ops, "ensure_team_network"),
            patch("oduflow.walg.apply_archive_command") as apply_cmd,
        ):
            assert system_ops.ensure_prod_infra(client, settings) is True
        client.volumes.create.assert_called_once()
        client.containers.run.assert_called_once()
        # No [backup] configured -> archive command stays disabled.
        assert apply_cmd.call_args[1]["enabled"] is False

    def test_force_provisions_without_productions(self, settings):
        client = _client_without_prod()
        with (
            patch("oduflow.walg.ensure_walg"),
            patch.object(system_ops, "_wait_pg_ready"),
            patch.object(system_ops, "ensure_team_network"),
            patch("oduflow.walg.apply_archive_command"),
        ):
            assert system_ops.ensure_prod_infra(client, settings, force=True) is True
        client.containers.run.assert_called_once()

    def test_walg_download_failure_does_not_block(self, settings):
        client = _client_without_prod()
        with (
            patch("oduflow.walg.ensure_walg", side_effect=RuntimeError("offline")),
            patch.object(system_ops, "_wait_pg_ready"),
            patch.object(system_ops, "ensure_team_network"),
            patch("oduflow.walg.apply_archive_command") as apply_cmd,
        ):
            assert system_ops.ensure_prod_infra(client, settings, force=True) is True
        assert apply_cmd.call_args[1]["enabled"] is False


class TestProdPgContainer:
    def test_container_config(self, settings):
        client = _client_without_prod()
        with (
            patch("oduflow.walg.ensure_walg"),
            patch.object(system_ops, "_wait_pg_ready"),
            patch.object(system_ops, "ensure_team_network"),
            patch("oduflow.walg.apply_archive_command"),
        ):
            system_ops.ensure_prod_infra(client, settings, force=True)

        kwargs = client.containers.run.call_args[1]
        assert kwargs["name"] == "oduflow-prod-db"
        volumes = kwargs["volumes"]
        assert volumes[settings.prod_db_volume]["bind"] == ("/var/lib/postgresql/data")
        # wal-g bin + conf dirs are mounted read-only from day one so
        # backups can be enabled later without recreating the container.
        binds = {v["bind"]: v["mode"] for v in volumes.values()}
        assert binds["/opt/oduflow-bin"] == "ro"
        assert binds["/etc/walg"] == "ro"
        # No /tablespaces mount and no published ports.
        assert "/tablespaces" not in binds
        assert "ports" not in kwargs
        assert kwargs["labels"]["oduflow.prod"] == "true"

    def test_prod_image_override(self, settings, tmp_path):
        settings = Settings(
            base_data_dir=settings.base_data_dir,
            etc_dir=settings.etc_dir,
            prod_postgres_image="postgres:17",
            teams=settings.teams,
        )
        client = _client_without_prod()
        with (
            patch("oduflow.walg.ensure_walg"),
            patch.object(system_ops, "_wait_pg_ready"),
            patch.object(system_ops, "ensure_team_network"),
            patch("oduflow.walg.apply_archive_command"),
        ):
            system_ops.ensure_prod_infra(client, settings, force=True)
        assert client.containers.run.call_args[0][0] == "postgres:17"

    def test_existing_container_started_not_recreated(self, settings):
        client = MagicMock()
        stopped = MagicMock()
        stopped.status = "exited"
        client.containers.get.return_value = stopped
        with (
            patch("oduflow.walg.ensure_walg"),
            patch.object(system_ops, "_wait_pg_ready"),
            patch.object(system_ops, "ensure_team_network"),
            patch("oduflow.walg.apply_archive_command"),
        ):
            system_ops.ensure_prod_infra(client, settings)
        stopped.start.assert_called_once()
        client.containers.run.assert_not_called()


class TestProdPgConf:
    def test_generated_once_with_keep_marker(self, settings):
        path = system_ops._ensure_prod_pg_conf(settings)
        content = open(path).read()
        assert content.splitlines()[0] == "# KEEP"
        assert "archive_mode = on" in content
        # Existing file is never rewritten.
        with open(path, "w") as f:
            f.write("# KEEP\ncustom")
        system_ops._ensure_prod_pg_conf(settings)
        assert open(path).read() == "# KEEP\ncustom"
