import json
from unittest.mock import MagicMock

import docker

from oduflow.docker_ops import system_ops
from oduflow.settings import ExtraRoute, Settings, TeamSettings


def _traefik_settings(tmp_path, tls, extra_routes=()):
    team = TeamSettings(team_id="1", hostname="dev.example.com")
    return Settings(
        routing_mode="traefik",
        routing_tls=tls,
        acme_email="admin@example.com",
        etc_dir=str(tmp_path),
        teams={"1": team},
        extra_routes=tuple(extra_routes),
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

    def test_extra_route_generates_router_and_service(self, tmp_path):
        cfg = tmp_path / "traefik.yml"
        route = ExtraRoute(
            name="legacy-api", host="api.example.com", url="https://10.0.0.5:8443"
        )
        system_ops._write_traefik_dynamic_config(
            _traefik_settings(tmp_path, True, [route]), str(cfg)
        )
        http = json.loads(cfg.read_text())["http"]
        router = http["routers"]["oduflow-route-legacy-api"]
        assert router["rule"] == "Host(`api.example.com`)"
        assert router["service"] == "oduflow-route-legacy-api"
        assert router["entryPoints"] == ["websecure"]
        assert router["tls"] == {"certResolver": "letsencrypt"}
        service = http["services"]["oduflow-route-legacy-api"]
        assert service["loadBalancer"]["servers"] == [{"url": "https://10.0.0.5:8443"}]

    def test_extra_route_loopback_rewritten_to_host(self, tmp_path):
        cfg = tmp_path / "traefik.yml"
        route = ExtraRoute(
            name="local", host="api.example.com", url="http://127.0.0.1:3000"
        )
        system_ops._write_traefik_dynamic_config(
            _traefik_settings(tmp_path, False, [route]), str(cfg)
        )
        http = json.loads(cfg.read_text())["http"]
        service = http["services"]["oduflow-route-local"]
        assert service["loadBalancer"]["servers"] == [
            {"url": "http://host.docker.internal:3000"}
        ]
        # A non-loopback host is left untouched.
        assert (
            system_ops._resolve_upstream_url("http://192.168.1.9:3000")
            == "http://192.168.1.9:3000"
        )
        # localhost is rewritten too, but a hostname that merely starts with
        # "localhost" (e.g. localhost.example.com) is not.
        assert (
            system_ops._resolve_upstream_url("http://localhost:5000")
            == "http://host.docker.internal:5000"
        )
        assert (
            system_ops._resolve_upstream_url("http://localhost.example.com:5000")
            == "http://localhost.example.com:5000"
        )
        # https:// loopbacks are NOT rewritten: swapping the host would break
        # backend TLS cert verification (cert is for localhost/127.0.0.1).
        assert (
            system_ops._resolve_upstream_url("https://localhost:5000")
            == "https://localhost:5000"
        )
        assert (
            system_ops._resolve_upstream_url("https://127.0.0.1:5000")
            == "https://127.0.0.1:5000"
        )


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

    def test_mounts_dynamic_directory_with_file_provider(self, tmp_path):
        client = self._client_no_container()
        system_ops._ensure_traefik(client, _traefik_settings(tmp_path, True))
        kwargs = client.containers.run.call_args[1]
        cmd = kwargs["command"]
        # Directory provider (not single-file) so operators can drop in *.yml.
        assert "--providers.file.directory=/etc/traefik/dynamic" in cmd
        assert not any(a.startswith("--providers.file.filename=") for a in cmd)
        dyn = str(tmp_path / "traefik-dynamic")
        assert kwargs["volumes"][dyn] == {"bind": "/etc/traefik/dynamic", "mode": "ro"}
        # Oduflow's generated config lands inside that directory.
        assert (tmp_path / "traefik-dynamic" / "oduflow.yml").is_file()

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
                "Cmd": [
                    "--providers.file.directory=/etc/traefik/dynamic",
                    "--entrypoints.web.http.redirections.entryPoint.to=websecure",
                ]
            }
        }
        client = MagicMock()
        client.containers.get.return_value = existing
        system_ops._ensure_traefik(client, _traefik_settings(tmp_path, False))
        existing.stop.assert_called_once()
        existing.remove.assert_called_once()
        client.containers.run.assert_called_once()
        assert client.containers.run.call_args[1]["ports"] == {"80/tcp": 80}

    def test_drift_recreates_on_old_single_file_provider(self, tmp_path):
        # Existing container watches a single file (older layout); TLS matches
        # but it must be recreated onto the directory provider so operator
        # drop-in *.yml files are honoured.
        existing = MagicMock()
        existing.attrs = {
            "Config": {
                "Cmd": [
                    "--entrypoints.web.address=:80",
                    "--providers.file.filename=/etc/traefik/dynamic/oduflow.yml",
                ]
            }
        }
        existing.status = "running"
        client = MagicMock()
        client.containers.get.return_value = existing
        system_ops._ensure_traefik(client, _traefik_settings(tmp_path, False))
        existing.stop.assert_called_once()
        existing.remove.assert_called_once()
        client.containers.run.assert_called_once()

    def test_no_drift_when_mode_matches(self, tmp_path):
        # Existing container already in the desired (no-TLS, directory) mode:
        # reuse it.
        existing = MagicMock()
        existing.attrs = {
            "Config": {
                "Cmd": [
                    "--entrypoints.web.address=:80",
                    "--providers.file.directory=/etc/traefik/dynamic",
                ]
            }
        }
        existing.status = "running"
        client = MagicMock()
        client.containers.get.return_value = existing
        system_ops._ensure_traefik(client, _traefik_settings(tmp_path, False))
        existing.remove.assert_not_called()
        client.containers.run.assert_not_called()
