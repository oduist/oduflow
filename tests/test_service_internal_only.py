"""Contract tests for internal-only auxiliary services.

An internal-only service is reachable from sibling containers on the team
network by container name, and by nothing else: no Traefik router, no public
hostname, no published host port.
"""

import docker
import pytest
from unittest.mock import MagicMock, patch

from oduflow.docker_ops import service_ops, service_presets
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


@pytest.fixture(autouse=True)
def _fake_team_network(monkeypatch):
    monkeypatch.setattr(
        "oduflow.docker_ops.system_ops.ensure_team_network",
        lambda client, settings, team: f"oduflow-{team.team_id}-net",
    )


@pytest.fixture
def no_preset_writes(monkeypatch):
    """Keep create_service's auto-save away from the real data dir."""
    monkeypatch.setattr(
        "oduflow.docker_ops.service_ops.service_presets.save_preset",
        lambda *args, **kwargs: {},
    )


@pytest.fixture
def mock_docker_client():
    with (
        patch("oduflow.docker_ops.service_ops.get_client") as svc_mock,
        patch("oduflow.docker_ops.system_ops.get_client") as sys_mock,
        patch("oduflow.docker_ops.volume_ops.get_client") as vol_mock,
    ):
        client_instance = MagicMock()
        svc_mock.return_value = client_instance
        sys_mock.return_value = client_instance
        vol_mock.return_value = client_instance
        yield client_instance


def _make_container(
    *,
    image_tags,
    image_id="sha256:old",
    labels,
    attrs,
    status="running",
    name="oduflow-1-svc-nats",
):
    container = MagicMock()
    container.name = name
    container.image.tags = image_tags
    container.image.id = image_id
    container.labels = labels
    container.attrs = attrs
    container.status = status
    return container


@pytest.mark.usefixtures("no_preset_writes")
class TestCreateInternalOnly:
    def test_create_without_port_or_routes(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        result = service_ops.create_service(
            TRAEFIK_SETTINGS,
            TRAEFIK_TEAM,
            "nats",
            "nats:2.10",
            None,
            internal_only=True,
        )

        run_kwargs = mock_docker_client.containers.run.call_args[1]
        labels = run_kwargs["labels"]

        # Joined to the team network, addressable by container name.
        assert run_kwargs["network"] == "oduflow-1-net"
        assert result["container_name"] == "oduflow-1-svc-nats"
        # No hostname.
        assert result.get("hostname") is None
        # No published host port.
        assert "ports" not in run_kwargs
        # No router, service or middleware — only the explicit opt-out.
        assert [key for key in labels if key.startswith("traefik.")] == [
            "traefik.enable"
        ]
        assert labels["traefik.enable"] == "false"
        assert labels["oduflow.internal_only"] == "true"
        assert result["internal_only"] is True
        assert result["url"] is None
        assert result["routes"] == []

    def test_create_publishes_no_host_port_outside_traefik(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        service_ops.create_service(
            TEST_SETTINGS,
            TEST_TEAM,
            "nats",
            "nats:2.10",
            None,
            internal_only=True,
        )

        run_kwargs = mock_docker_client.containers.run.call_args[1]
        assert "ports" not in run_kwargs
        assert run_kwargs["network"] == "oduflow-1-net"

    def test_create_saves_mode_in_preset(self, mock_docker_client, monkeypatch):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        saved = {}
        monkeypatch.setattr(
            "oduflow.docker_ops.service_ops.service_presets.save_preset",
            lambda *args, **kwargs: saved.update(kwargs),
        )

        service_ops.create_service(
            TRAEFIK_SETTINGS,
            TRAEFIK_TEAM,
            "nats",
            "nats:2.10",
            None,
            internal_only=True,
        )

        assert saved["internal_only"] is True
        assert saved["hostname"] is None
        assert saved["routes"] is None

    @pytest.mark.parametrize(
        "kwargs, message",
        [
            ({"port": 4222}, "omit port"),
            ({"routes": [{"path": "/x", "port": 8080}]}, "cannot define HTTP routes"),
            ({"hostname": "nats"}, "omit hostname"),
            ({"host_mode": True}, "cannot be combined with host_mode"),
        ],
    )
    def test_create_rejects_conflicting_exposure(
        self, mock_docker_client, kwargs, message
    ):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        port = kwargs.pop("port", None)

        with pytest.raises(ValueError) as exc_info:
            service_ops.create_service(
                TRAEFIK_SETTINGS,
                TRAEFIK_TEAM,
                "nats",
                "nats:2.10",
                port,
                internal_only=True,
                **kwargs,
            )

        assert message in str(exc_info.value)
        mock_docker_client.containers.run.assert_not_called()

    def test_published_service_still_requires_port_or_routes(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(ValueError) as exc_info:
            service_ops.create_service(
                TRAEFIK_SETTINGS, TRAEFIK_TEAM, "nats", "nats:2.10", None
            )

        assert "port must be between 1 and 65535" in str(exc_info.value)

    def test_default_create_is_unchanged(self, mock_docker_client):
        """Callers that never pass internal_only keep the old behaviour."""
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        result = service_ops.create_service(
            TEST_SETTINGS, TEST_TEAM, "redis", "redis:7", 6379
        )

        run_kwargs = mock_docker_client.containers.run.call_args[1]
        assert run_kwargs["ports"] == {"6379/tcp": 6379}
        assert "oduflow.internal_only" not in run_kwargs["labels"]
        assert result["internal_only"] is False
        assert result["url"] == "http://localhost:6379"


@pytest.mark.usefixtures("no_preset_writes")
class TestDescribeInternalOnly:
    def _internal_container(self):
        return _make_container(
            image_tags=["nats:2.10"],
            image_id="sha256:nats",
            labels={
                "oduflow.managed": "true",
                "oduflow.service": "nats",
                "oduflow.internal_only": "true",
            },
            attrs={
                "Config": {"Env": ["PATH=/usr/bin"]},
                "NetworkSettings": {"Ports": {}},
                "Mounts": [],
                "HostConfig": {"CapAdd": [], "Privileged": False},
                "State": {"StartedAt": "2026-07-29T10:00:00Z"},
                "RestartCount": 0,
            },
        )

    def test_get_service_info_reports_mode(self, mock_docker_client):
        mock_docker_client.containers.get.return_value = self._internal_container()

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value={"name": "nats"},
        ):
            info = service_ops.get_service_info(TRAEFIK_SETTINGS, TRAEFIK_TEAM, "nats")

        assert info["internal_only"] is True
        assert info["hostname"] is None
        assert info["url"] is None
        assert info["port"] is None
        assert info["routes"] == []
        assert info["container_name"] == "oduflow-1-svc-nats"

    def test_list_services_reports_mode(self, mock_docker_client):
        mock_docker_client.containers.list.return_value = [self._internal_container()]

        services = service_ops.list_services(TRAEFIK_SETTINGS, TRAEFIK_TEAM)

        assert len(services) == 1
        assert services[0]["internal_only"] is True
        assert services[0]["url"] is None

    def test_published_service_reports_false(self, mock_docker_client):
        container = _make_container(
            image_tags=["redis:7"],
            labels={"oduflow.managed": "true", "oduflow.service": "redis"},
            attrs={
                "Config": {"Env": []},
                "NetworkSettings": {
                    "Ports": {"6379/tcp": [{"HostIp": "0.0.0.0", "HostPort": "6379"}]}
                },
                "Mounts": [],
                "HostConfig": {"CapAdd": [], "Privileged": False},
            },
            name="oduflow-1-svc-redis",
        )
        mock_docker_client.containers.list.return_value = [container]

        services = service_ops.list_services(TEST_SETTINGS, TEST_TEAM)

        assert services[0]["internal_only"] is False
        assert services[0]["port"] == 6379


@pytest.mark.usefixtures("no_preset_writes")
class TestUpdateInternalOnly:
    def _published_container(self):
        return _make_container(
            image_tags=["nats:2.10"],
            image_id="sha256:old",
            labels={
                "oduflow.managed": "true",
                "oduflow.service": "nats",
                "traefik.enable": "true",
                "traefik.http.routers.oduflow-1-svc-nats.rule": "Host(`nats.example.com`)",
                "traefik.http.services.oduflow-1-svc-nats.loadbalancer.server.port": "8222",
            },
            attrs={
                "Config": {"Env": [], "Image": "nats:2.10"},
                "NetworkSettings": {"Ports": {}},
                "Mounts": [
                    {
                        "Type": "volume",
                        "Name": "oduflow-traefik-acme",
                        "Destination": "/etc/traefik",
                        "RW": False,
                    }
                ],
                "HostConfig": {"CapAdd": [], "Privileged": False},
            },
        )

    def _internal_container(self):
        return _make_container(
            image_tags=["nats:2.10"],
            image_id="sha256:old",
            labels={
                "oduflow.managed": "true",
                "oduflow.service": "nats",
                "oduflow.internal_only": "true",
            },
            attrs={
                "Config": {"Env": [], "Image": "nats:2.10"},
                "NetworkSettings": {"Ports": {}},
                "Mounts": [
                    {
                        "Type": "volume",
                        "Name": "oduflow-traefik-acme",
                        "Destination": "/etc/traefik",
                        "RW": False,
                    }
                ],
                "HostConfig": {"CapAdd": [], "Privileged": False},
            },
        )

    def test_external_to_internal_drops_router_and_port(self, mock_docker_client):
        container = self._published_container()
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.images.pull.return_value = MagicMock(id="sha256:old")

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value={"name": "nats", "image": "nats:2.10", "port": 8222},
        ):
            result = service_ops.update_service(
                TRAEFIK_SETTINGS, TRAEFIK_TEAM, "nats", internal_only_override=True
            )

        # The old container is replaced, so its port bindings and labels go away.
        container.stop.assert_called_once()
        container.remove.assert_called_once_with(v=True)
        run_kwargs = mock_docker_client.containers.run.call_args[1]
        assert "ports" not in run_kwargs
        new_labels = run_kwargs["labels"]
        assert [key for key in new_labels if key.startswith("traefik.")] == [
            "traefik.enable"
        ]
        assert new_labels["traefik.enable"] == "false"
        assert new_labels["oduflow.internal_only"] == "true"
        assert result["config_updated"] is True
        assert result["internal_only"] is True
        assert result["url"] is None

    def test_internal_to_external_restores_router(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = [
            self._internal_container(),
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.images.pull.return_value = MagicMock(id="sha256:old")

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value={
                "name": "nats",
                "image": "nats:2.10",
                "port": 0,
                "internal_only": True,
            },
        ):
            result = service_ops.update_service(
                TRAEFIK_SETTINGS,
                TRAEFIK_TEAM,
                "nats",
                internal_only_override=False,
                port_override=8222,
            )

        labels = mock_docker_client.containers.run.call_args[1]["labels"]
        assert (
            labels["traefik.http.routers.oduflow-1-svc-nats.rule"]
            == "Host(`nats.example.com`)"
        )
        assert "oduflow.internal_only" not in labels
        assert labels["traefik.enable"] == "true"
        assert result["internal_only"] is False
        assert result["url"] == "https://nats.example.com"

    def test_internal_to_external_requires_new_exposure(self, mock_docker_client):
        mock_docker_client.containers.get.return_value = self._internal_container()

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value={
                "name": "nats",
                "image": "nats:2.10",
                "port": 0,
                "internal_only": True,
            },
        ):
            with pytest.raises(NotFoundError) as exc_info:
                service_ops.update_service(
                    TRAEFIK_SETTINGS, TRAEFIK_TEAM, "nats", internal_only_override=False
                )

        assert "Cannot determine port" in str(exc_info.value)
        mock_docker_client.containers.run.assert_not_called()

    def test_switching_to_internal_rejects_exposure_args(self, mock_docker_client):
        container = self._published_container()
        mock_docker_client.containers.get.return_value = container

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value={"name": "nats", "image": "nats:2.10", "port": 8222},
        ):
            with pytest.raises(ValueError) as exc_info:
                service_ops.update_service(
                    TRAEFIK_SETTINGS,
                    TRAEFIK_TEAM,
                    "nats",
                    internal_only_override=True,
                    port_override=4222,
                )

        assert "cannot be combined with port" in str(exc_info.value)
        container.stop.assert_not_called()

    def test_internal_stays_internal_without_override(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = [
            self._internal_container(),
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.images.pull.return_value = MagicMock(id="sha256:new")

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value={
                "name": "nats",
                "image": "nats:2.10",
                "port": 0,
                "internal_only": True,
            },
        ):
            result = service_ops.update_service(
                TRAEFIK_SETTINGS, TRAEFIK_TEAM, "nats", image_override="nats:2.11"
            )

        run_kwargs = mock_docker_client.containers.run.call_args[1]
        assert run_kwargs["network"] == "oduflow-1-net"
        assert "ports" not in run_kwargs
        assert run_kwargs["labels"]["oduflow.internal_only"] == "true"
        assert result["internal_only"] is True
        assert result["image_updated"] is True

    def test_internal_no_op_update_reports_no_url(self, mock_docker_client):
        mock_docker_client.containers.get.return_value = self._internal_container()
        mock_docker_client.images.pull.return_value = MagicMock(id="sha256:old")

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value={
                "name": "nats",
                "image": "nats:2.10",
                "port": 0,
                "internal_only": True,
            },
        ):
            result = service_ops.update_service(TRAEFIK_SETTINGS, TRAEFIK_TEAM, "nats")

        assert result["image_updated"] is False
        assert result["config_updated"] is False
        assert result["internal_only"] is True
        assert result["url"] is None
        mock_docker_client.containers.run.assert_not_called()

    def test_internal_rejects_host_mode_override(self, mock_docker_client):
        container = self._internal_container()
        mock_docker_client.containers.get.return_value = container

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value={
                "name": "nats",
                "image": "nats:2.10",
                "port": 0,
                "internal_only": True,
            },
        ):
            with pytest.raises(ValueError) as exc_info:
                service_ops.update_service(
                    TRAEFIK_SETTINGS, TRAEFIK_TEAM, "nats", host_mode_override=True
                )

        assert "cannot be combined with host_mode" in str(exc_info.value)
        container.stop.assert_not_called()

    def test_legacy_container_without_preset_keeps_mode(self, mock_docker_client):
        """A pre-preset internal-only container is recognised by its label."""
        mock_docker_client.containers.get.side_effect = [
            self._internal_container(),
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.images.pull.return_value = MagicMock(id="sha256:new")

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            side_effect=NotFoundError("no preset"),
        ):
            result = service_ops.update_service(TRAEFIK_SETTINGS, TRAEFIK_TEAM, "nats")

        assert result["internal_only"] is True
        run_kwargs = mock_docker_client.containers.run.call_args[1]
        assert "ports" not in run_kwargs


@pytest.mark.usefixtures("no_preset_writes")
class TestExposureModeConflict:
    """Preset and live container disagreeing about the exposure mode.

    The preset is authoritative but written best-effort, so it can fall behind
    the container. Both directions of the disagreement are refused rather than
    guessed, because either guess silently changes public exposure. An explicit
    override resolves it.
    """

    PRESET_PUBLISHED = {"name": "nats", "image": "nats:2.10", "port": 8222}
    PRESET_INTERNAL = {
        "name": "nats",
        "image": "nats:2.10",
        "port": 0,
        "internal_only": True,
    }

    def _container(self, *, internal_label: bool):
        labels = {
            "oduflow.managed": "true",
            "oduflow.service": "nats",
        }
        if internal_label:
            labels["oduflow.internal_only"] = "true"
            labels["traefik.enable"] = "false"
        else:
            labels["traefik.enable"] = "true"
            labels["traefik.http.routers.oduflow-1-svc-nats.rule"] = (
                "Host(`nats.example.com`)"
            )
            labels[
                "traefik.http.services.oduflow-1-svc-nats.loadbalancer.server.port"
            ] = "8222"
        return _make_container(
            image_tags=["nats:2.10"],
            image_id="sha256:old",
            labels=labels,
            attrs={
                "Config": {"Env": [], "Image": "nats:2.10"},
                "NetworkSettings": {"Ports": {}},
                "Mounts": [
                    {
                        "Type": "volume",
                        "Name": "oduflow-traefik-acme",
                        "Destination": "/etc/traefik",
                        "RW": False,
                    }
                ],
                "HostConfig": {"CapAdd": [], "Privileged": False},
            },
        )

    def test_preset_published_but_container_internal_is_refused(
        self, mock_docker_client
    ):
        container = self._container(internal_label=True)
        mock_docker_client.containers.get.return_value = container

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=self.PRESET_PUBLISHED,
        ):
            with pytest.raises(ConflictError) as exc_info:
                service_ops.update_service(TRAEFIK_SETTINGS, TRAEFIK_TEAM, "nats")

        message = str(exc_info.value)
        assert "preset says published" in message
        assert "running container is internal-only" in message
        assert "internal_only=true or internal_only=false" in message
        # Refused before touching the running service.
        container.stop.assert_not_called()
        mock_docker_client.containers.run.assert_not_called()

    def test_preset_internal_but_container_published_is_refused(
        self, mock_docker_client
    ):
        container = self._container(internal_label=False)
        mock_docker_client.containers.get.return_value = container

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=self.PRESET_INTERNAL,
        ):
            with pytest.raises(ConflictError) as exc_info:
                service_ops.update_service(TRAEFIK_SETTINGS, TRAEFIK_TEAM, "nats")

        message = str(exc_info.value)
        assert "preset says internal-only" in message
        assert "running container is published" in message
        container.stop.assert_not_called()
        mock_docker_client.containers.run.assert_not_called()

    def test_explicit_internal_only_true_resolves_the_conflict(
        self, mock_docker_client
    ):
        mock_docker_client.containers.get.side_effect = [
            self._container(internal_label=True),
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.images.pull.return_value = MagicMock(id="sha256:old")

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=self.PRESET_PUBLISHED,
        ):
            result = service_ops.update_service(
                TRAEFIK_SETTINGS, TRAEFIK_TEAM, "nats", internal_only_override=True
            )

        labels = mock_docker_client.containers.run.call_args[1]["labels"]
        assert [key for key in labels if key.startswith("traefik.")] == [
            "traefik.enable"
        ]
        assert labels["traefik.enable"] == "false"
        assert labels["oduflow.internal_only"] == "true"
        assert result["internal_only"] is True
        assert result["url"] is None

    def test_legacy_preset_and_legacy_container_agree_as_published(
        self, mock_docker_client
    ):
        """Neither side carries the key: both mean published, no conflict.

        This is the shape of every service that existed before internal-only —
        a preset without the field and a container without the label. The
        absence must not read as a disagreement.
        """
        assert "internal_only" not in self.PRESET_PUBLISHED
        container = self._container(internal_label=False)
        assert "oduflow.internal_only" not in container.labels
        mock_docker_client.containers.get.side_effect = [
            container,
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.images.pull.return_value = MagicMock(id="sha256:new")

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=self.PRESET_PUBLISHED,
        ):
            result = service_ops.update_service(TRAEFIK_SETTINGS, TRAEFIK_TEAM, "nats")

        assert result["internal_only"] is False
        assert result["url"] == "https://nats.example.com"
        labels = mock_docker_client.containers.run.call_args[1]["labels"]
        assert labels["traefik.enable"] == "true"
        assert "oduflow.internal_only" not in labels

    def test_legacy_published_pair_is_not_recreated_without_changes(
        self, mock_docker_client
    ):
        """The absent-field pair must not look like drift and force a recreate."""
        mock_docker_client.containers.get.return_value = self._container(
            internal_label=False
        )
        mock_docker_client.images.pull.return_value = MagicMock(id="sha256:old")

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=self.PRESET_PUBLISHED,
        ):
            result = service_ops.update_service(TRAEFIK_SETTINGS, TRAEFIK_TEAM, "nats")

        assert result["config_updated"] is False
        assert result["image_updated"] is False
        mock_docker_client.containers.run.assert_not_called()

    def test_matching_internal_pair_is_not_a_conflict(self, mock_docker_client):
        """Both sides say internal-only: agreement, not disagreement."""
        mock_docker_client.containers.get.return_value = self._container(
            internal_label=True
        )
        mock_docker_client.images.pull.return_value = MagicMock(id="sha256:old")

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=self.PRESET_INTERNAL,
        ):
            result = service_ops.update_service(TRAEFIK_SETTINGS, TRAEFIK_TEAM, "nats")

        assert result["internal_only"] is True
        assert result["config_updated"] is False
        mock_docker_client.containers.run.assert_not_called()

    def test_explicit_internal_only_false_resolves_the_conflict(
        self, mock_docker_client
    ):
        mock_docker_client.containers.get.side_effect = [
            self._container(internal_label=True),
            docker.errors.NotFound("nf"),
        ]
        mock_docker_client.images.pull.return_value = MagicMock(id="sha256:old")

        with patch(
            "oduflow.docker_ops.service_ops.service_presets.get_preset",
            return_value=self.PRESET_PUBLISHED,
        ):
            result = service_ops.update_service(
                TRAEFIK_SETTINGS,
                TRAEFIK_TEAM,
                "nats",
                internal_only_override=False,
                port_override=8222,
            )

        labels = mock_docker_client.containers.run.call_args[1]["labels"]
        assert (
            labels["traefik.http.routers.oduflow-1-svc-nats.rule"]
            == "Host(`nats.example.com`)"
        )
        assert labels["traefik.enable"] == "true"
        assert "oduflow.internal_only" not in labels
        assert result["internal_only"] is False
        assert result["url"] == "https://nats.example.com"


class TestInternalOnlyPresets:
    @pytest.fixture
    def tmp_team(self, tmp_path):
        return TeamSettings(
            team_id="1",
            data_dir=str(tmp_path),
            port_registry_path=str(tmp_path / "ports.json"),
        )

    def test_round_trip(self, tmp_team):
        service_presets.save_preset(
            tmp_team, "nats", "nats:2.10", None, internal_only=True
        )
        preset = service_presets.get_preset(tmp_team, "nats")
        assert preset["internal_only"] is True
        assert preset["port"] == 0

    def test_published_preset_omits_the_key(self, tmp_team):
        service_presets.save_preset(tmp_team, "redis", "redis:7", 6379)
        preset = service_presets.get_preset(tmp_team, "redis")
        assert "internal_only" not in preset

    def test_legacy_preset_reads_as_published(self, tmp_team):
        """Presets written before the mode existed keep their meaning."""
        service_presets.save_preset(tmp_team, "redis", "redis:7", 6379)
        preset = service_presets.get_preset(tmp_team, "redis")
        assert preset.get("internal_only", False) is False


# -- REST API contract -------------------------------------------------------
#
# The dashboard is not the only caller, so the server-side validation is
# exercised directly rather than through the UI.


def _web_client(tmp_path):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from oduflow.locking import LockManager
    from oduflow.web_ui import mount_web_ui

    team = TeamSettings(team_id="1", hostname="example.com", data_dir=str(tmp_path))
    settings = Settings(
        routing_mode="traefik",
        routing_tls=False,
        base_data_dir=str(tmp_path),
        teams={"1": team},
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app)


class TestInternalOnlyWebApi:
    def test_create_accepts_internal_only_without_port(self, tmp_path):
        client = _web_client(tmp_path)
        with patch("oduflow.web_ui.service_ops.create_service") as create:
            create.return_value = {
                "name": "nats",
                "container_name": "oduflow-1-svc-nats",
                "image": "nats:2.10",
                "url": None,
                "internal_only": True,
                "routes": [],
            }
            response = client.post(
                "/api/services/create",
                json={"name": "nats", "image": "nats:2.10", "internal_only": True},
            )

        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert create.call_args.args[4] is None
        assert create.call_args.kwargs["internal_only"] is True

    def test_create_still_requires_exposure_when_not_internal(self, tmp_path):
        client = _web_client(tmp_path)

        response = client.post(
            "/api/services/create", json={"name": "nats", "image": "nats:2.10"}
        )

        assert response.status_code == 400
        assert "internal_only" in response.json()["error"]

    def test_create_rejects_internal_only_with_port(self, tmp_path):
        client = _web_client(tmp_path)

        response = client.post(
            "/api/services/create",
            json={
                "name": "nats",
                "image": "nats:2.10",
                "internal_only": True,
                "port": 4222,
            },
        )

        assert response.status_code == 400
        assert "omit port" in response.json()["error"]

    def test_update_passes_mode_through(self, tmp_path):
        client = _web_client(tmp_path)
        result = {
            "name": "nats",
            "container_name": "oduflow-1-svc-nats",
            "image": "nats:2.10",
            "url": None,
            "internal_only": True,
            "routes": [],
        }
        with patch("oduflow.web_ui.service_ops.update_service") as update:
            update.return_value = result
            response = client.post(
                "/api/services/nats/update", json={"internal_only": True}
            )

        assert response.status_code == 200
        assert update.call_args.kwargs["internal_only_override"] is True

    def test_update_without_the_key_keeps_current_mode(self, tmp_path):
        client = _web_client(tmp_path)
        with patch("oduflow.web_ui.service_ops.update_service") as update:
            update.return_value = {
                "name": "nats",
                "container_name": "oduflow-1-svc-nats",
                "image": "nats:2.10",
                "url": "https://nats.example.com",
                "internal_only": False,
                "routes": [],
            }
            response = client.post("/api/services/nats/update", json={})

        assert response.status_code == 200
        assert update.call_args.kwargs["internal_only_override"] is None

    def test_restore_accepts_internal_only_preset(self, tmp_path):
        client = _web_client(tmp_path)
        with patch("oduflow.web_ui.service_ops.create_service") as create:
            create.return_value = {
                "name": "nats",
                "container_name": "oduflow-1-svc-nats",
                "image": "nats:2.10",
                "url": None,
                "internal_only": True,
                "routes": [],
            }
            response = client.post(
                "/api/service-presets/restore",
                json={"name": "nats", "image": "nats:2.10", "internal_only": True},
            )

        assert response.status_code == 200
        assert create.call_args.kwargs["internal_only"] is True
