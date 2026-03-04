import pytest
import docker
from unittest.mock import MagicMock, patch

from oduflow.docker_ops import service_ops, system_ops
from oduflow.errors import ConflictError, NotFoundError, PrerequisiteNotMetError
from oduflow.settings import Settings, TeamSettings

TEST_TEAM = TeamSettings(
    team_id="1",
    data_dir="/tmp/flow-test",
    port_registry_path="/tmp/flow-test/ports.json",
    port_range_start=50000,
    port_range_end=50100,
)

TEST_SETTINGS = Settings(
    base_data_dir="/tmp/flow-test",
    db_user="odoo",
    db_password="odoo",
    teams={"1": TEST_TEAM},
)

TRAEFIK_TEAM = TeamSettings(
    team_id="1",
    hostname="example.com",
    data_dir="/tmp/flow-test",
    port_registry_path="/tmp/flow-test/ports.json",
    port_range_start=50000,
    port_range_end=50100,
)

TRAEFIK_SETTINGS = Settings(
    routing_mode="traefik",
    acme_email="admin@example.com",
    base_data_dir="/tmp/flow-test",
    db_user="odoo",
    db_password="odoo",
    teams={"1": TRAEFIK_TEAM},
)


@pytest.fixture
def mock_docker_client():
    with (
        patch("oduflow.docker_ops.service_ops.get_client") as svc_mock,
        patch("oduflow.docker_ops.system_ops.get_client") as sys_mock,
    ):
        client_instance = MagicMock()
        svc_mock.return_value = client_instance
        sys_mock.return_value = client_instance
        yield client_instance


class TestCreateService:
    def test_create_port_mode(self, mock_docker_client):
        # Network exists
        mock_docker_client.networks.get.return_value = MagicMock()
        # No existing container
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.run.return_value = MagicMock()

        result = service_ops.create_service(
            TEST_SETTINGS, TEST_TEAM, "redis", "redis:7", 6379
        )

        assert result["name"] == "redis"
        assert result["container_name"] == "oduflow-svc-redis"
        assert result["url"] == "http://localhost:6379"
        assert result["image"] == "redis:7"

        run_kwargs = mock_docker_client.containers.run.call_args
        assert run_kwargs[1]["name"] == "oduflow-svc-redis"
        assert run_kwargs[1]["image"] == "redis:7"
        assert run_kwargs[1]["network"] == "oduflow-net"
        assert run_kwargs[1]["ports"] == {"6379/tcp": 6379}
        assert run_kwargs[1]["labels"]["oduflow.managed"] == "true"
        assert run_kwargs[1]["labels"]["oduflow.service"] == "redis"

    def test_create_traefik_mode(self, mock_docker_client):
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.run.return_value = MagicMock()

        result = service_ops.create_service(
            TRAEFIK_SETTINGS,
            TRAEFIK_TEAM,
            "meilisearch",
            "getmeili/meilisearch:v1.6",
            7700,
        )

        assert result["url"] == "https://meilisearch.example.com"

        run_kwargs = mock_docker_client.containers.run.call_args
        labels = run_kwargs[1]["labels"]
        assert labels["traefik.enable"] == "true"
        assert (
            labels["traefik.http.routers.oduflow-svc-meilisearch.rule"]
            == "Host(`meilisearch.example.com`)"
        )
        assert (
            labels[
                "traefik.http.services.oduflow-svc-meilisearch.loadbalancer.server.port"
            ]
            == "7700"
        )
        # No port mapping in traefik mode
        assert "ports" not in run_kwargs[1]

    def test_create_traefik_custom_hostname(self, mock_docker_client):
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.run.return_value = MagicMock()

        result = service_ops.create_service(
            TRAEFIK_SETTINGS,
            TRAEFIK_TEAM,
            "redis",
            "redis:7",
            6379,
            hostname="my-redis.example.com",
        )

        assert result["url"] == "https://my-redis.example.com"
        labels = mock_docker_client.containers.run.call_args[1]["labels"]
        assert (
            labels["traefik.http.routers.oduflow-svc-redis.rule"]
            == "Host(`my-redis.example.com`)"
        )

    def test_create_with_env_vars(self, mock_docker_client):
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.run.return_value = MagicMock()

        env = {"MEILI_MASTER_KEY": "abc123", "MEILI_ENV": "production"}
        service_ops.create_service(
            TEST_SETTINGS,
            TEST_TEAM,
            "meili",
            "getmeili/meilisearch:v1.6",
            7700,
            env_vars=env,
        )

        run_kwargs = mock_docker_client.containers.run.call_args
        assert run_kwargs[1]["environment"] == env

    def test_create_without_env_vars(self, mock_docker_client):
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.run.return_value = MagicMock()

        service_ops.create_service(TEST_SETTINGS, TEST_TEAM, "redis", "redis:7", 6379)

        run_kwargs = mock_docker_client.containers.run.call_args
        assert "environment" not in run_kwargs[1]

    def test_create_conflict(self, mock_docker_client):
        mock_docker_client.networks.get.return_value = MagicMock()
        existing = MagicMock()
        existing.status = "running"
        mock_docker_client.containers.get.return_value = existing

        with pytest.raises(ConflictError, match="already exists"):
            service_ops.create_service(
                TEST_SETTINGS, TEST_TEAM, "redis", "redis:7", 6379
            )

        mock_docker_client.containers.run.assert_not_called()

    def test_create_network_missing(self, mock_docker_client):
        mock_docker_client.networks.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(PrerequisiteNotMetError, match="not initialized"):
            service_ops.create_service(
                TEST_SETTINGS, TEST_TEAM, "redis", "redis:7", 6379
            )

        mock_docker_client.containers.run.assert_not_called()


class TestDeleteService:
    def test_delete(self, mock_docker_client):
        container = MagicMock()
        mock_docker_client.containers.get.return_value = container

        result = service_ops.delete_service(TEST_SETTINGS, "redis")

        assert result["name"] == "redis"
        assert result["container_name"] == "oduflow-svc-redis"
        container.stop.assert_called_once()
        container.remove.assert_called_once_with(v=True)

    def test_delete_not_found(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(NotFoundError, match="Service 'redis' not found"):
            service_ops.delete_service(TEST_SETTINGS, "redis")


class TestListServices:
    def test_list_with_services_port_mode(self, mock_docker_client):
        container = MagicMock()
        container.labels = {
            "oduflow.managed": "true",
            "oduflow.service": "redis",
        }
        container.name = "oduflow-svc-redis"
        container.status = "running"
        container.image.tags = ["redis:7"]
        container.attrs = {
            "NetworkSettings": {
                "Ports": {"6379/tcp": [{"HostIp": "0.0.0.0", "HostPort": "6379"}]}
            },
            "Config": {"Env": ["REDIS_PASSWORD=secret", "PATH=/usr/bin", "HOME=/root"]},
        }
        mock_docker_client.containers.list.return_value = [container]

        result = service_ops.list_services(TEST_SETTINGS, TEST_TEAM)

        assert len(result) == 1
        svc = result[0]
        assert svc["name"] == "redis"
        assert svc["container_name"] == "oduflow-svc-redis"
        assert svc["image"] == "redis:7"
        assert svc["status"] == "running"
        assert svc["port"] == 6379
        assert svc["url"] == "http://localhost:6379"
        # Env vars: system vars filtered out, only custom remain
        assert svc["env_vars"] == {"REDIS_PASSWORD": "secret"}

    def test_list_with_services_traefik_mode(self, mock_docker_client):
        container = MagicMock()
        container.labels = {
            "oduflow.managed": "true",
            "oduflow.service": "meili",
            "traefik.enable": "true",
            "traefik.http.routers.oduflow-svc-meili.rule": "Host(`meili.example.com`)",
            "traefik.http.services.oduflow-svc-meili.loadbalancer.server.port": "7700",
        }
        container.name = "oduflow-svc-meili"
        container.status = "running"
        container.image.tags = ["getmeili/meilisearch:v1.6"]
        container.attrs = {"Config": {"Env": []}}
        mock_docker_client.containers.list.return_value = [container]

        result = service_ops.list_services(TRAEFIK_SETTINGS, TRAEFIK_TEAM)

        assert len(result) == 1
        svc = result[0]
        assert svc["url"] == "https://meili.example.com"
        assert svc["port"] == 7700

    def test_list_empty(self, mock_docker_client):
        mock_docker_client.containers.list.return_value = []

        result = service_ops.list_services(TEST_SETTINGS, TEST_TEAM)

        assert result == []

    def test_list_skips_non_service_containers(self, mock_docker_client):
        # Container with managed label but no service label (e.g. an odoo env)
        container = MagicMock()
        container.labels = {"oduflow.managed": "true", "oduflow.branch": "main"}
        mock_docker_client.containers.list.return_value = [container]

        result = service_ops.list_services(TEST_SETTINGS, TEST_TEAM)

        assert result == []

    def test_list_env_var_filtering(self, mock_docker_client):
        container = MagicMock()
        container.labels = {"oduflow.managed": "true", "oduflow.service": "meili"}
        container.name = "oduflow-svc-meili"
        container.status = "running"
        container.image.tags = ["getmeili/meilisearch:v1.6"]
        container.attrs = {
            "NetworkSettings": {"Ports": {}},
            "Config": {
                "Env": [
                    "MEILI_MASTER_KEY=abc",
                    "MEILI_ENV=production",
                    "PATH=/usr/local/bin:/usr/bin",
                    "HOME=/root",
                    "HOSTNAME=abc123",
                    "TERM=xterm",
                    "LANG=en_US.UTF-8",
                    "LC_ALL=en_US.UTF-8",
                ]
            },
        }
        mock_docker_client.containers.list.return_value = [container]

        result = service_ops.list_services(TEST_SETTINGS, TEST_TEAM)

        env = result[0]["env_vars"]
        assert env == {"MEILI_MASTER_KEY": "abc", "MEILI_ENV": "production"}
        # System vars must be excluded
        for sys_key in ("PATH", "HOME", "HOSTNAME", "TERM", "LANG", "LC_ALL"):
            assert sys_key not in env

    def test_list_image_fallback(self, mock_docker_client):
        """When image.tags is empty, fall back to Config.Image."""
        container = MagicMock()
        container.labels = {"oduflow.managed": "true", "oduflow.service": "redis"}
        container.name = "oduflow-svc-redis"
        container.status = "running"
        container.image.tags = []
        container.attrs = {
            "NetworkSettings": {"Ports": {}},
            "Config": {"Image": "redis:7-alpine", "Env": []},
        }
        mock_docker_client.containers.list.return_value = [container]

        result = service_ops.list_services(TEST_SETTINGS, TEST_TEAM)

        assert result[0]["image"] == "unknown"

    def test_list_port_no_mappings(self, mock_docker_client):
        """Port key exists but no host mappings."""
        container = MagicMock()
        container.labels = {"oduflow.managed": "true", "oduflow.service": "redis"}
        container.name = "oduflow-svc-redis"
        container.status = "exited"
        container.image.tags = ["redis:7"]
        container.attrs = {
            "NetworkSettings": {"Ports": {"6379/tcp": None}},
            "Config": {"Env": []},
        }
        mock_docker_client.containers.list.return_value = [container]

        result = service_ops.list_services(TEST_SETTINGS, TEST_TEAM)

        svc = result[0]
        assert svc["port"] == 6379
        assert svc["url"] is None


class TestUpdateService:
    def test_update_port_mode(self, mock_docker_client):
        """Update pulls the image, removes old container, and re-creates with same settings."""
        container = MagicMock()
        container.image.tags = ["redis:7"]
        container.labels = {"oduflow.managed": "true", "oduflow.service": "redis"}
        container.attrs = {
            "NetworkSettings": {
                "Ports": {"6379/tcp": [{"HostIp": "0.0.0.0", "HostPort": "6379"}]}
            },
            "Config": {"Env": ["REDIS_PASSWORD=secret", "PATH=/usr/bin", "HOME=/root"]},
        }

        # First get() returns the existing container, second get() (inside create_service)
        # raises NotFound (container was removed)
        mock_docker_client.containers.get.side_effect = [
            container,  # update_service lookup
            docker.errors.NotFound("nf"),  # create_service conflict check
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        result = service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "redis")

        assert result["name"] == "redis"
        assert result["image"] == "redis:7"
        assert result["url"] == "http://localhost:6379"

        # Verify pull was called
        mock_docker_client.images.pull.assert_called_once_with("redis:7")

        # Verify old container was stopped and removed
        container.stop.assert_called_once()
        container.remove.assert_called_once_with(v=True)

        # Verify re-creation with same env vars (only non-system)
        run_kwargs = mock_docker_client.containers.run.call_args
        assert run_kwargs[1]["environment"] == {"REDIS_PASSWORD": "secret"}

    def test_update_traefik_mode(self, mock_docker_client):
        container = MagicMock()
        container.image.tags = ["getmeili/meilisearch:v1.6"]
        container.labels = {
            "oduflow.managed": "true",
            "oduflow.service": "meili",
            "traefik.http.routers.oduflow-svc-meili.rule": "Host(`meili.example.com`)",
            "traefik.http.services.oduflow-svc-meili.loadbalancer.server.port": "7700",
        }
        container.attrs = {"Config": {"Env": ["MEILI_MASTER_KEY=abc"]}}

        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        result = service_ops.update_service(TRAEFIK_SETTINGS, TRAEFIK_TEAM, "meili")

        assert result["url"] == "https://meili.example.com"
        mock_docker_client.images.pull.assert_called_once_with(
            "getmeili/meilisearch:v1.6"
        )

        # Verify traefik labels are restored
        run_kwargs = mock_docker_client.containers.run.call_args
        labels = run_kwargs[1]["labels"]
        assert (
            labels["traefik.http.routers.oduflow-svc-meili.rule"]
            == "Host(`meili.example.com`)"
        )

    def test_update_no_env_vars(self, mock_docker_client):
        """When the container has no custom env vars, env_vars=None is passed to create."""
        container = MagicMock()
        container.image.tags = ["redis:7"]
        container.labels = {"oduflow.managed": "true", "oduflow.service": "redis"}
        container.attrs = {
            "NetworkSettings": {
                "Ports": {"6379/tcp": [{"HostIp": "0.0.0.0", "HostPort": "6379"}]}
            },
            "Config": {"Env": ["PATH=/usr/bin", "HOME=/root"]},
        }

        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "redis")

        run_kwargs = mock_docker_client.containers.run.call_args
        assert "environment" not in run_kwargs[1]

    def test_update_not_found(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(NotFoundError, match="Service 'redis' not found"):
            service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "redis")

    def test_update_image_fallback_to_config(self, mock_docker_client):
        """When image.tags is empty, fall back to Config.Image."""
        container = MagicMock()
        container.image.tags = []
        container.labels = {"oduflow.managed": "true", "oduflow.service": "redis"}
        container.attrs = {
            "NetworkSettings": {
                "Ports": {"6379/tcp": [{"HostIp": "0.0.0.0", "HostPort": "6379"}]}
            },
            "Config": {"Image": "redis:7-alpine", "Env": []},
        }

        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        result = service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "redis")

        assert result["image"] == "redis:7-alpine"
        mock_docker_client.images.pull.assert_called_once_with("redis:7-alpine")


class TestGetServiceLogs:
    def test_logs(self, mock_docker_client):
        container = MagicMock()
        container.logs.return_value = (
            b"2025-01-01T00:00:00Z log line 1\n2025-01-01T00:00:01Z log line 2"
        )
        mock_docker_client.containers.get.return_value = container

        output = service_ops.get_service_logs(TEST_SETTINGS, "redis", 50)

        assert "log line 1" in output
        assert "log line 2" in output
        container.logs.assert_called_with(tail=50, timestamps=True)

    def test_logs_default_lines(self, mock_docker_client):
        container = MagicMock()
        container.logs.return_value = b"line"
        mock_docker_client.containers.get.return_value = container

        service_ops.get_service_logs(TEST_SETTINGS, "redis")

        container.logs.assert_called_with(tail=100, timestamps=True)

    def test_logs_not_found(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(NotFoundError, match="Service 'redis' not found"):
            service_ops.get_service_logs(TEST_SETTINGS, "redis")


class TestDestroyBlockedByServices:
    def test_destroy_with_active_services(self, mock_docker_client):
        container = MagicMock()
        container.labels = {"oduflow.service": "redis", "oduflow.managed": "true"}
        container.name = "oduflow-svc-redis"
        mock_docker_client.containers.list.return_value = [container]

        with pytest.raises(ConflictError, match="Active environments/services exist"):
            system_ops.destroy_system(TEST_SETTINGS)
