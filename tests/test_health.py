from unittest.mock import MagicMock, patch

import docker
import pytest

from oduflow import health
from oduflow.settings import BackupSettings, Settings, TeamSettings


@pytest.fixture(autouse=True)
def _clear_cache():
    health._cache["result"] = None
    health._cache["at"] = 0.0
    yield
    health._cache["result"] = None


@pytest.fixture
def settings(tmp_path):
    team_dir = tmp_path / "team_1"
    team_dir.mkdir()
    return Settings(
        base_data_dir=str(tmp_path),
        routing_mode="traefik",
        acme_email="a@b.co",
        teams={"1": TeamSettings(team_id="1", data_dir=str(team_dir))},
    )


def _client(running: dict[str, bool]):
    """Docker client mock: containers in `running` exist; exec is healthy."""
    client = MagicMock()

    def _get(name):
        if name not in running:
            raise docker.errors.NotFound("nf")
        container = MagicMock()
        container.status = "running" if running[name] else "exited"
        container.exec_run.return_value = (0, b"")
        return container

    client.containers.get.side_effect = _get
    return client


class TestCollectHealth:
    def test_all_ok(self, settings):
        client = _client(
            {"oduflow-db": True, "oduflow-prod-db": True, "oduflow-traefik": True}
        )
        with patch("oduflow.docker_ops.client.get_client", return_value=client):
            result = health.collect_health(settings, force=True)
        assert result["ok"] is True
        assert result["checks"]["dev_pg"]["status"] == "ok"
        assert result["checks"]["prod_pg"]["status"] == "ok"
        assert result["checks"]["traefik"]["status"] == "ok"
        # No [backup] section -> S3 check is off, not an error.
        assert result["checks"]["s3"]["status"] == "off"

    def test_missing_prod_pg_is_off_not_error(self, settings):
        client = _client({"oduflow-db": True, "oduflow-traefik": True})
        with patch("oduflow.docker_ops.client.get_client", return_value=client):
            result = health.collect_health(settings, force=True)
        assert result["checks"]["prod_pg"]["status"] == "off"
        assert result["ok"] is True

    def test_stopped_dev_pg_degrades(self, settings):
        client = _client({"oduflow-db": False, "oduflow-traefik": True})
        with patch("oduflow.docker_ops.client.get_client", return_value=client):
            result = health.collect_health(settings, force=True)
        assert result["checks"]["dev_pg"]["status"] == "error"
        assert result["ok"] is False

    def test_unhealthy_production_degrades(self, settings):
        from oduflow import production_registry

        production_registry.create_production(
            settings.teams["1"], "erp", {"unhealthy": True}
        )
        client = _client(
            {"oduflow-db": True, "oduflow-prod-db": True, "oduflow-traefik": True}
        )
        with patch("oduflow.docker_ops.client.get_client", return_value=client):
            result = health.collect_health(settings, force=True)
        assert result["ok"] is False
        assert result["checks"]["productions"]["unhealthy"] == ["1/erp"]

    def test_s3_error_degrades(self, settings, tmp_path):
        settings = Settings(
            base_data_dir=settings.base_data_dir,
            routing_mode="traefik",
            acme_email="a@b.co",
            backup=BackupSettings(bucket="b", access_key="a", secret_key="s"),
            teams=settings.teams,
        )
        client = _client(
            {"oduflow-db": True, "oduflow-prod-db": True, "oduflow-traefik": True}
        )
        with (
            patch("oduflow.docker_ops.client.get_client", return_value=client),
            patch(
                "oduflow.s3_client.check_s3",
                return_value={"ok": False, "error": "denied"},
            ),
        ):
            result = health.collect_health(settings, force=True)
        assert result["checks"]["s3"]["status"] == "error"
        assert result["ok"] is False

    def test_disk_warn_does_not_degrade(self, settings):
        client = _client(
            {"oduflow-db": True, "oduflow-prod-db": True, "oduflow-traefik": True}
        )
        fake_usage = MagicMock(total=100, used=90, free=10)
        with (
            patch("oduflow.docker_ops.client.get_client", return_value=client),
            patch("shutil.disk_usage", return_value=fake_usage),
        ):
            result = health.collect_health(settings, force=True)
        assert result["checks"]["disk"]["status"] == "warn"
        assert result["ok"] is True

    def test_cache(self, settings):
        client = _client(
            {"oduflow-db": True, "oduflow-prod-db": True, "oduflow-traefik": True}
        )
        with patch(
            "oduflow.docker_ops.client.get_client", return_value=client
        ) as getter:
            health.collect_health(settings, force=True)
            first_calls = getter.call_count
            health.collect_health(settings)  # cached
            assert getter.call_count == first_calls
