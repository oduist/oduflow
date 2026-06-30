import pytest
import docker
from unittest.mock import MagicMock, patch

from oduflow.docker_ops import volume_ops, service_ops
from oduflow.errors import ConflictError, NotFoundError
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


@pytest.fixture
def mock_docker_client():
    with (
        patch("oduflow.docker_ops.volume_ops.get_client") as vol_mock,
        patch("oduflow.docker_ops.service_ops.get_client") as svc_mock,
    ):
        client_instance = MagicMock()
        vol_mock.return_value = client_instance
        svc_mock.return_value = client_instance
        yield client_instance


class TestDockerVolumeName:
    def test_basic(self):
        result = volume_ops.docker_volume_name(TEST_TEAM, "redis-data")
        assert result == "oduflow-vol-1-redis-data"

    def test_different_team(self):
        team2 = TeamSettings(
            team_id="2", data_dir="/tmp", port_registry_path="/tmp/p.json"
        )
        result = volume_ops.docker_volume_name(team2, "mydata")
        assert result == "oduflow-vol-2-mydata"


class TestCreateVolume:
    def test_create_basic(self, mock_docker_client):
        mock_docker_client.volumes.get.side_effect = docker.errors.NotFound("nf")
        mock_vol = MagicMock()
        mock_docker_client.volumes.create.return_value = mock_vol

        result = volume_ops.create_volume(TEST_SETTINGS, TEST_TEAM, "redis-data")

        assert result["name"] == "redis-data"
        assert result["docker_name"] == "oduflow-vol-1-redis-data"
        mock_docker_client.volumes.create.assert_called_once()
        call_kwargs = mock_docker_client.volumes.create.call_args
        assert call_kwargs[1]["name"] == "oduflow-vol-1-redis-data"
        labels = call_kwargs[1]["labels"]
        assert labels["oduflow.managed"] == "true"
        assert labels["oduflow.team"] == "1"
        assert labels["oduflow.volume"] == "redis-data"

    def test_create_with_description(self, mock_docker_client):
        mock_docker_client.volumes.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.volumes.create.return_value = MagicMock()

        result = volume_ops.create_volume(
            TEST_SETTINGS, TEST_TEAM, "pg-data", description="PostgreSQL data"
        )

        assert result["description"] == "PostgreSQL data"
        labels = mock_docker_client.volumes.create.call_args[1]["labels"]
        assert labels["oduflow.description"] == "PostgreSQL data"

    def test_create_conflict(self, mock_docker_client):
        mock_docker_client.volumes.get.return_value = MagicMock()

        with pytest.raises(ConflictError, match="already exists"):
            volume_ops.create_volume(TEST_SETTINGS, TEST_TEAM, "redis-data")

    def test_create_invalid_name(self, mock_docker_client):
        with pytest.raises(ConflictError, match="Invalid volume name"):
            volume_ops.create_volume(TEST_SETTINGS, TEST_TEAM, "bad name!")

    def test_create_empty_name(self, mock_docker_client):
        with pytest.raises(ConflictError, match="Invalid volume name"):
            volume_ops.create_volume(TEST_SETTINGS, TEST_TEAM, "")


class TestListVolumes:
    def test_list_empty(self, mock_docker_client):
        mock_docker_client.volumes.list.return_value = []
        mock_docker_client.containers.list.return_value = []

        result = volume_ops.list_volumes(TEST_SETTINGS, TEST_TEAM)
        assert result == []

    def test_list_with_volumes(self, mock_docker_client):
        vol = MagicMock()
        vol.name = "oduflow-vol-1-redis-data"
        vol.attrs = {
            "CreatedAt": "2025-01-01T00:00:00Z",
            "Labels": {
                "oduflow.managed": "true",
                "oduflow.team": "1",
                "oduflow.volume": "redis-data",
                "oduflow.description": "Redis cache",
            },
        }
        mock_docker_client.volumes.list.return_value = [vol]
        mock_docker_client.containers.list.return_value = []

        result = volume_ops.list_volumes(TEST_SETTINGS, TEST_TEAM)
        assert len(result) == 1
        assert result[0]["name"] == "redis-data"
        assert result[0]["description"] == "Redis cache"
        assert result[0]["used_by"] == []

    def test_list_with_usage(self, mock_docker_client):
        vol = MagicMock()
        vol.name = "oduflow-vol-1-redis-data"
        vol.attrs = {
            "CreatedAt": "2025-01-01T00:00:00Z",
            "Labels": {
                "oduflow.managed": "true",
                "oduflow.team": "1",
                "oduflow.volume": "redis-data",
                "oduflow.description": "",
            },
        }
        mock_docker_client.volumes.list.return_value = [vol]

        # Service container using this volume
        container = MagicMock()
        container.labels = {
            "oduflow.managed": "true",
            "oduflow.team": "1",
            "oduflow.service": "redis",
        }
        container.attrs = {
            "Mounts": [
                {
                    "Type": "volume",
                    "Name": "oduflow-vol-1-redis-data",
                    "Destination": "/data",
                }
            ]
        }
        mock_docker_client.containers.list.return_value = [container]

        result = volume_ops.list_volumes(TEST_SETTINGS, TEST_TEAM)
        assert result[0]["used_by"] == ["redis"]

    def test_list_sorted(self, mock_docker_client):
        vol_b = MagicMock()
        vol_b.name = "oduflow-vol-1-zz-data"
        vol_b.attrs = {
            "CreatedAt": "",
            "Labels": {
                "oduflow.managed": "true",
                "oduflow.team": "1",
                "oduflow.volume": "zz-data",
                "oduflow.description": "",
            },
        }
        vol_a = MagicMock()
        vol_a.name = "oduflow-vol-1-aa-data"
        vol_a.attrs = {
            "CreatedAt": "",
            "Labels": {
                "oduflow.managed": "true",
                "oduflow.team": "1",
                "oduflow.volume": "aa-data",
                "oduflow.description": "",
            },
        }
        mock_docker_client.volumes.list.return_value = [vol_b, vol_a]
        mock_docker_client.containers.list.return_value = []

        result = volume_ops.list_volumes(TEST_SETTINGS, TEST_TEAM)
        assert result[0]["name"] == "aa-data"
        assert result[1]["name"] == "zz-data"


class TestInspectVolume:
    def test_inspect(self, mock_docker_client):
        vol = MagicMock()
        vol.attrs = {
            "CreatedAt": "2025-01-01T00:00:00Z",
            "Driver": "local",
            "Mountpoint": "/var/lib/docker/volumes/oduflow-vol-1-redis-data/_data",
            "Labels": {
                "oduflow.volume": "redis-data",
                "oduflow.description": "Redis cache",
            },
        }
        mock_docker_client.volumes.get.return_value = vol
        mock_docker_client.containers.list.return_value = []

        result = volume_ops.inspect_volume(TEST_SETTINGS, TEST_TEAM, "redis-data")
        assert result["name"] == "redis-data"
        assert result["driver"] == "local"
        assert result["used_by"] == []

    def test_inspect_not_found(self, mock_docker_client):
        mock_docker_client.volumes.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(NotFoundError, match="not found"):
            volume_ops.inspect_volume(TEST_SETTINGS, TEST_TEAM, "nonexistent")


class TestDeleteVolume:
    def test_delete(self, mock_docker_client):
        vol = MagicMock()
        mock_docker_client.volumes.get.return_value = vol
        mock_docker_client.containers.list.return_value = []

        result = volume_ops.delete_volume(TEST_SETTINGS, TEST_TEAM, "redis-data")
        assert result["name"] == "redis-data"
        vol.remove.assert_called_once()

    def test_delete_not_found(self, mock_docker_client):
        mock_docker_client.volumes.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(NotFoundError, match="not found"):
            volume_ops.delete_volume(TEST_SETTINGS, TEST_TEAM, "nonexistent")

    def test_delete_in_use(self, mock_docker_client):
        vol = MagicMock()
        mock_docker_client.volumes.get.return_value = vol

        container = MagicMock()
        container.labels = {
            "oduflow.managed": "true",
            "oduflow.team": "1",
            "oduflow.service": "redis",
        }
        container.attrs = {
            "Mounts": [
                {
                    "Type": "volume",
                    "Name": "oduflow-vol-1-redis-data",
                    "Destination": "/data",
                }
            ]
        }
        mock_docker_client.containers.list.return_value = [container]

        with pytest.raises(ConflictError, match="in use"):
            volume_ops.delete_volume(TEST_SETTINGS, TEST_TEAM, "redis-data")

        vol.remove.assert_not_called()


class TestParseVolumeMounts:
    def test_basic(self):
        result = volume_ops.parse_volume_mounts("mydata:/data")
        assert len(result) == 1
        assert result[0] == {"volume": "mydata", "mount_path": "/data", "mode": "rw"}

    def test_with_mode(self):
        result = volume_ops.parse_volume_mounts("mydata:/data:ro")
        assert result[0]["mode"] == "ro"

    def test_multiple_comma(self):
        result = volume_ops.parse_volume_mounts("a:/data,b:/logs:ro")
        assert len(result) == 2
        assert result[0]["volume"] == "a"
        assert result[1]["volume"] == "b"
        assert result[1]["mode"] == "ro"

    def test_multiple_newline(self):
        result = volume_ops.parse_volume_mounts("a:/data\nb:/logs")
        assert len(result) == 2

    def test_empty(self):
        assert volume_ops.parse_volume_mounts("") == []
        assert volume_ops.parse_volume_mounts("   ") == []

    def test_none(self):
        assert volume_ops.parse_volume_mounts(None) == []

    def test_invalid_format(self):
        with pytest.raises(ValueError, match="Invalid volume mount"):
            volume_ops.parse_volume_mounts("nopath")


class TestResolveVolumeBinds:
    def test_resolve(self, mock_docker_client):
        mock_docker_client.volumes.get.return_value = MagicMock()

        mounts = [{"volume": "mydata", "mount_path": "/data", "mode": "rw"}]
        result = volume_ops.resolve_volume_binds(TEST_TEAM, mounts)

        assert "oduflow-vol-1-mydata" in result
        assert result["oduflow-vol-1-mydata"] == {"bind": "/data", "mode": "rw"}

    def test_resolve_not_found(self, mock_docker_client):
        mock_docker_client.volumes.get.side_effect = docker.errors.NotFound("nf")

        mounts = [{"volume": "missing", "mount_path": "/data", "mode": "rw"}]
        with pytest.raises(NotFoundError, match="not found"):
            volume_ops.resolve_volume_binds(TEST_TEAM, mounts)

    def test_resolve_external_volume(self, mock_docker_client):
        """A genuinely external (non-Oduflow) volume is used as-is when the
        team-scoped name doesn't exist."""

        def _get(name):
            if name == "oduflow-vol-1-my-external-data":
                raise docker.errors.NotFound("nf")
            return MagicMock()  # original name exists

        mock_docker_client.volumes.get.side_effect = _get

        mounts = [
            {"volume": "my-external-data", "mount_path": "/data", "mode": "ro"}
        ]
        result = volume_ops.resolve_volume_binds(TEST_TEAM, mounts)

        assert "my-external-data" in result
        assert result["my-external-data"] == {"bind": "/data", "mode": "ro"}

    @pytest.mark.parametrize(
        "reserved",
        ["oduflow-db-data", "oduflow-traefik-acme", "oduflow-vol-2-secret"],
    )
    def test_resolve_rejects_reserved_oduflow_volume(
        self, mock_docker_client, reserved
    ):
        """Raw-name fallback must never reach an Oduflow-managed volume — the
        shared DB data, a system volume, or another team's volume (issue #40)."""
        # The team-scoped lookup misses, so the code falls back to the raw name.
        mock_docker_client.volumes.get.side_effect = docker.errors.NotFound("nf")

        mounts = [{"volume": reserved, "mount_path": "/x", "mode": "rw"}]
        with pytest.raises(NotFoundError, match="reserved"):
            volume_ops.resolve_volume_binds(TEST_TEAM, mounts)

    def test_resolve_empty(self, mock_docker_client):
        result = volume_ops.resolve_volume_binds(TEST_TEAM, [])
        assert result == {}


class TestCreateServiceWithVolumes:
    def test_create_with_volumes(self, mock_docker_client):
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.run.return_value = MagicMock()
        mock_docker_client.volumes.get.return_value = MagicMock()

        volumes = [{"volume": "redis-data", "mount_path": "/data", "mode": "rw"}]
        result = service_ops.create_service(
            TEST_SETTINGS, TEST_TEAM, "redis", "redis:7", 6379, volumes=volumes
        )

        assert result["name"] == "redis"
        run_kwargs = mock_docker_client.containers.run.call_args
        assert "volumes" in run_kwargs[1]
        assert "oduflow-vol-1-redis-data" in run_kwargs[1]["volumes"]

    def test_create_without_volumes(self, mock_docker_client):
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.run.return_value = MagicMock()

        service_ops.create_service(TEST_SETTINGS, TEST_TEAM, "redis", "redis:7", 6379)

        run_kwargs = mock_docker_client.containers.run.call_args
        assert "volumes" not in run_kwargs[1]
