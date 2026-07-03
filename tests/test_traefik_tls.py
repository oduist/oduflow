import json
from unittest.mock import MagicMock

import docker

from oduflow.docker_ops import system_ops
from oduflow.settings import Settings, TeamSettings


def _traefik_settings(tmp_path, tls):
    team = TeamSettings(team_id="1", hostname="dev.example.com")
    return Settings(
        routing_mode="traefik",
        routing_tls=tls,
        acme_email="admin@example.com",
        etc_dir=str(tmp_path),
        teams={"1": team},
    )


class TestWriteDynamicConfig:
    def test_tls_router_uses_websecure(self, tmp_path):
        cfg = tmp_path / "traefik.yml"
        system_ops._write_traefik_dynamic_config(
            _traefik_settings(tmp_path, True), str(cfg)
        )
        router = json.loads(cfg.read_text())["http"]["routers"]["oduflow-team-1"]
        assert router["entryPoints"] == ["websecure"]
        assert router["tls"] == {"certResolver": "letsencrypt"}

    def test_no_tls_router_uses_web(self, tmp_path):
        cfg = tmp_path / "traefik.yml"
        system_ops._write_traefik_dynamic_config(
            _traefik_settings(tmp_path, False), str(cfg)
        )
        router = json.loads(cfg.read_text())["http"]["routers"]["oduflow-team-1"]
        assert router["entryPoints"] == ["web"]
        assert "tls" not in router


class TestEnsureTraefik:
    def _client_no_container(self):
        client = MagicMock()
        client.containers.get.side_effect = docker.errors.NotFound("nf")
        client.volumes.get.side_effect = docker.errors.NotFound("nf")
        return client

    def test_tls_mode_publishes_443_and_acme(self, tmp_path):
        client = self._client_no_container()
        system_ops._ensure_traefik(client, _traefik_settings(tmp_path, True))
        kwargs = client.containers.run.call_args[1]
        assert kwargs["ports"] == {"80/tcp": 80, "443/tcp": 443}
        cmd = kwargs["command"]
        assert "--entrypoints.websecure.address=:443" in cmd
        assert any("redirections" in a for a in cmd)
        assert any("certificatesresolvers" in a for a in cmd)
        assert "oduflow-traefik-acme" in kwargs["volumes"]
        # Traefik terminates TLS itself, so no upstream forwarded-header trust.
        assert not any("forwardedHeaders" in a for a in cmd)

    def test_no_tls_mode_port_80_only(self, tmp_path):
        client = self._client_no_container()
        system_ops._ensure_traefik(client, _traefik_settings(tmp_path, False))
        kwargs = client.containers.run.call_args[1]
        assert kwargs["ports"] == {"80/tcp": 80}
        cmd = kwargs["command"]
        assert "--entrypoints.web.address=:80" in cmd
        assert "--entrypoints.websecure.address=:443" not in cmd
        assert not any("redirections" in a for a in cmd)
        assert not any("certificatesresolvers" in a for a in cmd)
        assert "oduflow-traefik-acme" not in kwargs["volumes"]
        # Trust the upstream tunnel's X-Forwarded-* headers so X-Forwarded-Proto:
        # https survives to Oduflow (cookie Secure flag, https:// links).
        assert "--entrypoints.web.forwardedHeaders.insecure=true" in cmd
        # ACME volume is not created when TLS is off.
        client.volumes.create.assert_not_called()

    def test_drift_recreates_on_tls_change(self, tmp_path):
        # Existing container was built in TLS mode, but config now wants
        # tls=false: the stale container is removed and a new one created.
        existing = MagicMock()
        existing.attrs = {
            "Config": {
                "Cmd": ["--entrypoints.web.http.redirections.entryPoint.to=websecure"]
            }
        }
        client = MagicMock()
        client.containers.get.return_value = existing
        system_ops._ensure_traefik(client, _traefik_settings(tmp_path, False))
        existing.stop.assert_called_once()
        existing.remove.assert_called_once()
        client.containers.run.assert_called_once()
        assert client.containers.run.call_args[1]["ports"] == {"80/tcp": 80}

    def test_no_drift_when_mode_matches(self, tmp_path):
        # Existing container already in the desired (no-TLS) mode: reuse it.
        existing = MagicMock()
        existing.attrs = {"Config": {"Cmd": ["--entrypoints.web.address=:80"]}}
        existing.status = "running"
        client = MagicMock()
        client.containers.get.return_value = existing
        system_ops._ensure_traefik(client, _traefik_settings(tmp_path, False))
        existing.remove.assert_not_called()
        client.containers.run.assert_not_called()
