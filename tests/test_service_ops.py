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

        mock_docker_client.images.pull.assert_called_once_with("redis:7")

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
    def _make_container(self, image_tags, labels, attrs):
        container = MagicMock()
        container.image.tags = image_tags
        container.labels = labels
        container.attrs = attrs
        return container

    def test_update_uses_preset(self, mock_docker_client):
        """When a preset exists, update_service reads options from it (not the container)."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )

        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {"REDIS_PASSWORD": "secret"},
            "volumes": [
                {"volume": "oduflow-traefik-acme", "mount_path": "/acme", "mode": "ro"}
            ],
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ), patch(
            "oduflow.docker_ops.service_ops.volume_ops.resolve_volume_binds",
            return_value={"vol1": {"bind": "/acme", "mode": "ro"}},
        ):
            result = service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "redis")

        assert result["name"] == "redis"
        assert result["image"] == "redis:7"

        run_kwargs = mock_docker_client.containers.run.call_args
        assert run_kwargs[1]["environment"] == {"REDIS_PASSWORD": "secret"}

    def test_update_preset_preserves_volumes(self, mock_docker_client):
        """Volumes from the preset (including external) are preserved through update."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )

        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        volumes = [
            {"volume": "oduflow-traefik-acme", "mount_path": "/acme", "mode": "ro"},
            {"volume": "data", "mount_path": "/data", "mode": "rw"},
        ]
        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
            "volumes": volumes,
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ), patch(
            "oduflow.docker_ops.service_ops.volume_ops.resolve_volume_binds",
            return_value={"vol1": {"bind": "/acme", "mode": "ro"}},
        ) as mock_resolve:
            service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "redis")
            mock_resolve.assert_called_once_with(TEST_TEAM, volumes)

    def test_update_port_mode_legacy_no_preset(self, mock_docker_client):
        """Legacy fallback: extract settings from container when no preset exists."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={
                "NetworkSettings": {
                    "Ports": {
                        "6379/tcp": [{"HostIp": "0.0.0.0", "HostPort": "6379"}]
                    }
                },
                "Config": {
                    "Env": ["REDIS_PASSWORD=secret", "PATH=/usr/bin", "HOME=/root"]
                },
                "Mounts": [],
            },
        )

        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            side_effect=NotFoundError("not found"),
        ):
            result = service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "redis")

        assert result["name"] == "redis"
        assert result["image"] == "redis:7"
        assert result["url"] == "http://localhost:6379"

        mock_docker_client.images.pull.assert_any_call("redis:7")
        container.stop.assert_called_once()
        container.remove.assert_called_once_with(v=True)

        run_kwargs = mock_docker_client.containers.run.call_args
        assert run_kwargs[1]["environment"] == {"REDIS_PASSWORD": "secret"}

    def test_update_traefik_mode(self, mock_docker_client):
        container = self._make_container(
            image_tags=["getmeili/meilisearch:v1.6"],
            labels={
                "oduflow.managed": "true",
                "oduflow.service": "meili",
                "traefik.http.routers.oduflow-svc-meili.rule": "Host(`meili.example.com`)",
                "traefik.http.services.oduflow-svc-meili.loadbalancer.server.port": "7700",
            },
            attrs={"Config": {"Env": ["MEILI_MASTER_KEY=abc"]}},
        )

        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        preset = {
            "name": "meili",
            "image": "getmeili/meilisearch:v1.6",
            "port": 7700,
            "hostname": "meili",
            "env_vars": {"MEILI_MASTER_KEY": "abc"},
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(
                TRAEFIK_SETTINGS, TRAEFIK_TEAM, "meili"
            )

        assert result["url"] == "https://meili.example.com"
        mock_docker_client.images.pull.assert_any_call("getmeili/meilisearch:v1.6")

        run_kwargs = mock_docker_client.containers.run.call_args
        labels = run_kwargs[1]["labels"]
        assert (
            labels["traefik.http.routers.oduflow-svc-meili.rule"]
            == "Host(`meili.example.com`)"
        )

    def test_update_no_env_vars(self, mock_docker_client):
        """When the preset has no custom env vars, env_vars=None is passed to create."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )

        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "redis")

        run_kwargs = mock_docker_client.containers.run.call_args
        assert "environment" not in run_kwargs[1]

    def test_update_image_unchanged_returns_url_port_mode(self, mock_docker_client):
        """When image digest is unchanged, return early with a valid URL (port mode)."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )
        container.image.id = "sha256:same"

        mock_docker_client.containers.get.return_value = container
        mock_docker_client.networks.get.return_value = MagicMock()
        pulled = MagicMock()
        pulled.id = "sha256:same"
        mock_docker_client.images.pull.return_value = pulled

        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "redis")

        assert result["image_updated"] is False
        assert result["url"] == "http://localhost:6379"
        assert result["name"] == "redis"
        assert result["image"] == "redis:7"
        container.stop.assert_not_called()
        container.remove.assert_not_called()
        mock_docker_client.containers.run.assert_not_called()

    def test_update_image_unchanged_returns_url_traefik(self, mock_docker_client):
        """When image digest is unchanged in traefik mode, URL is built from hostname."""
        container = self._make_container(
            image_tags=["getmeili/meilisearch:v1.6"],
            labels={"oduflow.managed": "true", "oduflow.service": "meili"},
            attrs={"Config": {"Env": []}},
        )
        container.image.id = "sha256:same"

        mock_docker_client.containers.get.return_value = container
        mock_docker_client.networks.get.return_value = MagicMock()
        pulled = MagicMock()
        pulled.id = "sha256:same"
        mock_docker_client.images.pull.return_value = pulled

        preset = {
            "name": "meili",
            "image": "getmeili/meilisearch:v1.6",
            "port": 7700,
            "hostname": "meili",
            "env_vars": {},
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(
                TRAEFIK_SETTINGS, TRAEFIK_TEAM, "meili"
            )

        assert result["image_updated"] is False
        assert result["url"] == "https://meili.example.com"
        container.stop.assert_not_called()
        container.remove.assert_not_called()
        mock_docker_client.containers.run.assert_not_called()

    def test_update_not_found(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(NotFoundError, match="Service 'redis' not found"):
            service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "redis")

    def test_update_image_fallback_to_config(self, mock_docker_client):
        """When image.tags is empty, fall back to Config.Image."""
        container = self._make_container(
            image_tags=[],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Image": "redis:7-alpine", "Env": []}},
        )

        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        preset = {
            "name": "redis",
            "image": "redis:7-alpine",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "redis")

        assert result["image"] == "redis:7-alpine"
        mock_docker_client.images.pull.assert_any_call("redis:7-alpine")

    def test_update_env_override_replaces_env_vars(self, mock_docker_client):
        """env_override fully replaces preset env_vars and forces recreation."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )
        # Force same digest so only config change triggers recreate
        pulled_image = MagicMock()
        pulled_image.id = container.image.id
        mock_docker_client.images.pull.return_value = pulled_image
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {"OLD_KEY": "old", "KEEP_ME": "1"},
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(
                TEST_SETTINGS,
                TEST_TEAM,
                "redis",
                env_override={"NEW_KEY": "new"},
            )

        assert result["config_updated"] is True
        assert result["image_updated"] is False
        container.stop.assert_called_once()
        container.remove.assert_called_once_with(v=True)

        run_kwargs = mock_docker_client.containers.run.call_args
        # Full replace: only NEW_KEY remains
        assert run_kwargs[1]["environment"] == {"NEW_KEY": "new"}

    def test_update_no_changes_no_recreate(self, mock_docker_client):
        """When image digest and all overrides match, no recreation happens."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )
        pulled_image = MagicMock()
        pulled_image.id = container.image.id
        mock_docker_client.images.pull.return_value = pulled_image
        mock_docker_client.containers.get.return_value = container

        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {"A": "1"},
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(TEST_SETTINGS, TEST_TEAM, "redis")

        assert result["image_updated"] is False
        assert result["config_updated"] is False
        assert result["url"] == "http://localhost:6379"
        container.stop.assert_not_called()
        container.remove.assert_not_called()

    def test_update_image_override(self, mock_docker_client):
        """image_override pulls a new image tag and recreates."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(
                TEST_SETTINGS, TEST_TEAM, "redis", image_override="redis:8"
            )

        assert result["image"] == "redis:8"
        assert result["config_updated"] is True
        mock_docker_client.images.pull.assert_any_call("redis:8")

    def test_update_port_override(self, mock_docker_client):
        """port_override changes port and recreates even if image digest unchanged."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )
        pulled_image = MagicMock()
        pulled_image.id = container.image.id
        mock_docker_client.images.pull.return_value = pulled_image
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(
                TEST_SETTINGS, TEST_TEAM, "redis", port_override=6380
            )

        assert result["config_updated"] is True
        assert result["image_updated"] is False
        run_kwargs = mock_docker_client.containers.run.call_args
        assert run_kwargs[1]["ports"] == {"6380/tcp": 6380}

    def test_update_port_override_repairs_legacy_host_mode(self, mock_docker_client):
        """port_override repairs a legacy host-mode service whose port cannot be inferred.

        In non-traefik host mode without a preset the port is unknowable, so the
        guard would normally raise. Supplying port_override must bypass the guard
        and let the service be recreated with the given port.
        """
        container = self._make_container(
            image_tags=["redis:7"],
            labels={
                "oduflow.managed": "true",
                "oduflow.service": "redis",
                "oduflow.host_mode": "true",
            },
            attrs={"Config": {"Env": []}, "HostConfig": {}, "Mounts": []},
        )
        pulled_image = MagicMock()
        pulled_image.id = container.image.id
        mock_docker_client.images.pull.return_value = pulled_image
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            side_effect=NotFoundError("no preset"),
        ):
            result = service_ops.update_service(
                TEST_SETTINGS, TEST_TEAM, "redis", port_override=7700
            )

        assert result["config_updated"] is True
        assert result["url"] == "http://localhost:7700"
        container.stop.assert_called_once()
        container.remove.assert_called_once_with(v=True)

    def test_update_net_admin_override_adds_cap(self, mock_docker_client):
        """cap_add_override adds NET_ADMIN and recreates the container."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )
        pulled_image = MagicMock()
        pulled_image.id = container.image.id
        mock_docker_client.images.pull.return_value = pulled_image
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(
                TEST_SETTINGS, TEST_TEAM, "redis", cap_add_override=["NET_ADMIN"]
            )

        assert result["config_updated"] is True
        assert result["image_updated"] is False
        run_kwargs = mock_docker_client.containers.run.call_args
        assert run_kwargs[1]["cap_add"] == ["NET_ADMIN"]

    def test_update_clear_net_admin(self, mock_docker_client):
        """cap_add_override=[] removes a previously set capability."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )
        pulled_image = MagicMock()
        pulled_image.id = container.image.id
        mock_docker_client.images.pull.return_value = pulled_image
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
            "cap_add": ["NET_ADMIN"],
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(
                TEST_SETTINGS, TEST_TEAM, "redis", cap_add_override=[]
            )

        assert result["config_updated"] is True
        run_kwargs = mock_docker_client.containers.run.call_args
        assert "cap_add" not in run_kwargs[1]

    def test_update_privileged_override(self, mock_docker_client):
        """privileged_override switches the service to privileged mode and recreates."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )
        pulled_image = MagicMock()
        pulled_image.id = container.image.id
        mock_docker_client.images.pull.return_value = pulled_image
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.run.return_value = MagicMock()

        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(
                TEST_SETTINGS, TEST_TEAM, "redis", privileged_override=True
            )

        assert result["config_updated"] is True
        run_kwargs = mock_docker_client.containers.run.call_args
        assert run_kwargs[1]["privileged"] is True

    def test_update_caps_unchanged_no_recreate(self, mock_docker_client):
        """Capability overrides equal to current values do not trigger a recreate."""
        container = self._make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={"Config": {"Env": []}},
        )
        pulled_image = MagicMock()
        pulled_image.id = container.image.id
        mock_docker_client.images.pull.return_value = pulled_image
        mock_docker_client.containers.get.return_value = container
        mock_docker_client.networks.get.return_value = MagicMock()

        preset = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
            "cap_add": ["NET_ADMIN"],
        }

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=preset,
        ):
            result = service_ops.update_service(
                TEST_SETTINGS,
                TEST_TEAM,
                "redis",
                cap_add_override=["NET_ADMIN"],
                privileged_override=False,
            )

        assert result["config_updated"] is False
        assert result["image_updated"] is False
        container.stop.assert_not_called()


class TestGetServiceInfo:
    def _make_container(self, image_tags, image_id, labels, attrs, status="running"):
        container = MagicMock()
        container.name = labels.get("__name") or "oduflow-svc-redis"
        container.image.tags = image_tags
        container.image.id = image_id
        container.labels = {k: v for k, v in labels.items() if k != "__name"}
        container.attrs = attrs
        container.status = status
        return container

    def test_get_service_info_port_mode(self, mock_docker_client):
        container = self._make_container(
            image_tags=["redis:7"],
            image_id="sha256:abc123def456",
            labels={
                "__name": "oduflow-svc-redis",
                "oduflow.managed": "true",
                "oduflow.service": "redis",
            },
            attrs={
                "Config": {
                    "Env": [
                        "REDIS_PASSWORD=secret",
                        "PATH=/usr/bin",
                    ]
                },
                "NetworkSettings": {
                    "Ports": {
                        "6379/tcp": [{"HostIp": "0.0.0.0", "HostPort": "6379"}]
                    }
                },
                "Mounts": [
                    {
                        "Type": "volume",
                        "Name": "oduflow-vol-1-data",
                        "Destination": "/data",
                        "RW": True,
                    }
                ],
                "HostConfig": {"CapAdd": ["NET_ADMIN"], "Privileged": False},
                "State": {"StartedAt": "2026-05-26T13:00:00Z"},
                "RestartCount": 2,
            },
        )
        mock_docker_client.containers.get.return_value = container

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value={"name": "redis"},
        ):
            info = service_ops.get_service_info(TEST_SETTINGS, TEST_TEAM, "redis")

        assert info["name"] == "redis"
        assert info["container_name"] == "oduflow-svc-redis"
        assert info["image"] == "redis:7"
        assert info["image_digest"] == "sha256:abc123def456"
        assert info["status"] == "running"
        assert info["port"] == 6379
        assert info["url"] == "http://localhost:6379"
        assert info["env_vars"] == {"REDIS_PASSWORD": "secret"}
        assert info["host_mode"] is False
        assert info["volumes"] == [
            {"volume": "data", "mount_path": "/data", "mode": "rw"}
        ]
        assert info["cap_add"] == ["NET_ADMIN"]
        assert info["privileged"] is False
        assert info["restart_count"] == 2
        assert info["started_at"] == "2026-05-26T13:00:00Z"
        assert info["has_preset"] is True

    def test_get_service_info_traefik_mode(self, mock_docker_client):
        container = self._make_container(
            image_tags=["getmeili/meilisearch:v1.6"],
            image_id="sha256:meili123",
            labels={
                "__name": "oduflow-svc-meili",
                "oduflow.managed": "true",
                "oduflow.service": "meili",
                "traefik.http.routers.oduflow-svc-meili.rule": "Host(`meili.example.com`)",
                "traefik.http.services.oduflow-svc-meili.loadbalancer.server.port": "7700",
            },
            attrs={
                "Config": {"Env": ["MEILI_MASTER_KEY=abc"]},
                "Mounts": [],
                "HostConfig": {"CapAdd": [], "Privileged": False},
                "State": {"StartedAt": "2026-05-26T13:00:00Z"},
                "RestartCount": 0,
            },
        )
        mock_docker_client.containers.get.return_value = container

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            side_effect=NotFoundError("no preset"),
        ):
            info = service_ops.get_service_info(
                TRAEFIK_SETTINGS, TRAEFIK_TEAM, "meili"
            )

        assert info["hostname"] == "meili.example.com"
        assert info["url"] == "https://meili.example.com"
        assert info["port"] == 7700
        assert info["has_preset"] is False

    def test_get_service_info_not_found(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(NotFoundError, match="Service 'redis' not found"):
            service_ops.get_service_info(TEST_SETTINGS, TEST_TEAM, "redis")

    def test_get_service_info_not_managed(self, mock_docker_client):
        """A container with name oduflow-svc-X but no oduflow.service label is rejected."""
        container = self._make_container(
            image_tags=["redis:7"],
            image_id="sha256:abc",
            labels={"some_other_label": "foo"},
            attrs={
                "Config": {"Env": []},
                "Mounts": [],
                "HostConfig": {},
                "State": {},
            },
        )
        mock_docker_client.containers.get.return_value = container

        with pytest.raises(NotFoundError, match="Service 'redis' not found"):
            service_ops.get_service_info(TEST_SETTINGS, TEST_TEAM, "redis")


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
