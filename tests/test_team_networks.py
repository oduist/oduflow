from unittest.mock import MagicMock, patch

import docker

from oduflow.docker_ops.system_ops import ensure_team_network
from oduflow.migrations import _migrate_per_team_networks
from oduflow.naming import get_team_network_name
from oduflow.settings import Settings, TeamSettings

TEAM = TeamSettings(team_id="1")
SETTINGS = Settings(teams={"1": TEAM})


def test_network_name():
    assert get_team_network_name("1") == "oduflow-1-net"


class TestEnsureTeamNetwork:
    def test_creates_and_attaches_infra(self):
        client = MagicMock()
        client.networks.get.side_effect = docker.errors.NotFound("nf")
        net = MagicMock()
        client.networks.create.return_value = net
        pg = MagicMock()
        client.containers.get.return_value = pg

        with patch("oduflow.docker_ops.system_ops._ensure_iptables_accept"):
            name = ensure_team_network(client, SETTINGS, TEAM)

        assert name == "oduflow-1-net"
        labels = client.networks.create.call_args[1]["labels"]
        assert labels["oduflow.team"] == "1"
        net.connect.assert_called_once_with(pg)  # port mode: PG only

    def test_traefik_attached_in_traefik_mode(self):
        settings = Settings(
            routing_mode="traefik", acme_email="x@y.z", teams={"1": TEAM}
        )
        client = MagicMock()
        net = MagicMock()
        client.networks.get.return_value = net  # network already exists

        ensure_team_network(client, settings, TEAM)

        # PG and Traefik both attached.
        assert net.connect.call_count == 2

    def test_already_connected_is_fine(self):
        client = MagicMock()
        net = MagicMock()
        client.networks.get.return_value = net
        net.connect.side_effect = docker.errors.APIError(
            "endpoint already exists in network"
        )

        assert ensure_team_network(client, SETTINGS, TEAM) == "oduflow-1-net"


class _FakeNet:
    def __init__(self):
        self.connected: list[str] = []
        self.disconnected: list[str] = []

    def connect(self, container):
        self.connected.append(container.name)

    def disconnect(self, container):
        self.disconnected.append(container.name)


class TestPerTeamNetworksMigration:
    def _container(self, name, labels, networks, network_mode="bridge"):
        c = MagicMock()
        c.name = name
        c.labels = labels
        c.attrs = {
            "HostConfig": {"NetworkMode": network_mode},
            "NetworkSettings": {"Networks": {n: {} for n in networks}},
            "Config": {"Cmd": []},
        }
        return c

    def test_moves_containers_and_recreates_traefik(self):
        env = self._container(
            "oduflow-1-main-odoo",
            {"oduflow.managed": "true", "oduflow.team": "1"},
            ["oduflow-net"],
        )
        hostsvc = self._container(
            "oduflow-1-svc-vpn",
            {"oduflow.managed": "true", "oduflow.team": "1"},
            [],
            network_mode="host",
        )
        traefik = MagicMock()
        traefik.attrs = {"Config": {"Cmd": ["--providers.docker.network=oduflow-net"]}}

        team_net = _FakeNet()
        shared_net = _FakeNet()
        client = MagicMock()
        client.containers.get.return_value = traefik
        client.containers.list.return_value = [env, hostsvc]
        client.networks.get.side_effect = lambda name: (
            shared_net if name == "oduflow-net" else team_net
        )

        settings = Settings(
            routing_mode="traefik", acme_email="x@y.z", teams={"1": TEAM}
        )
        with (
            patch("oduflow.docker_ops.client.get_client", return_value=client),
            patch(
                "oduflow.docker_ops.system_ops.ensure_team_network",
                return_value="oduflow-1-net",
            ),
        ):
            _migrate_per_team_networks(settings)

        # Stale traefik removed (system init recreates it after migrations).
        traefik.stop.assert_called_once()
        traefik.remove.assert_called_once()
        # Env container moved; host-mode service untouched.
        assert team_net.connected == ["oduflow-1-main-odoo"]
        assert shared_net.disconnected == ["oduflow-1-main-odoo"]

    def test_idempotent(self):
        env = self._container(
            "oduflow-1-main-odoo",
            {"oduflow.managed": "true", "oduflow.team": "1"},
            ["oduflow-1-net"],  # already moved
        )
        traefik = MagicMock()
        traefik.attrs = {"Config": {"Cmd": ["--entrypoints.web.address=:80"]}}

        team_net = _FakeNet()
        shared_net = _FakeNet()
        client = MagicMock()
        client.containers.get.return_value = traefik
        client.containers.list.return_value = [env]
        client.networks.get.side_effect = lambda name: (
            shared_net if name == "oduflow-net" else team_net
        )

        with (
            patch("oduflow.docker_ops.client.get_client", return_value=client),
            patch(
                "oduflow.docker_ops.system_ops.ensure_team_network",
                return_value="oduflow-1-net",
            ),
        ):
            _migrate_per_team_networks(SETTINGS)

        traefik.stop.assert_not_called()
        assert team_net.connected == []
        assert shared_net.disconnected == []
