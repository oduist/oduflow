"""Live-Docker verification of the internal-only exposure mode.

Asserts the properties that matter against a real daemon rather than a mock: no
host port bindings, no Traefik router, genuine reachability from a sibling
container by container name, and — against a real Traefik deliberately
configured with ``exposedByDefault=true`` — that no router or service for the
container appears in the proxy's dynamic configuration.

Run with:  pytest tests/test_service_internal_only_docker.py -m integration -v
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import pytest

docker = pytest.importorskip("docker")

from oduflow.docker_ops import service_ops  # noqa: E402
from oduflow.settings import Settings, TeamSettings  # noqa: E402

pytestmark = pytest.mark.integration

NATS_IMAGE = "nats:2.10-alpine"
PEER_IMAGE = "busybox:1.36"
TRAEFIK_IMAGE = "traefik:v3"


@pytest.fixture
def live(tmp_path):
    """Traefik mode without TLS: routers would be created, but must not be."""
    team = TeamSettings(
        team_id="itest",
        hostname="example.invalid",
        data_dir=str(tmp_path),
        port_registry_path=str(tmp_path / "ports.json"),
    )
    settings = Settings(
        routing_mode="traefik",
        routing_tls=False,
        base_data_dir=str(tmp_path),
        db_user="odoo",
        db_password="odoo",
        teams={"itest": team},
    )
    client = docker.from_env()
    created = []

    yield settings, team, client, created

    for name in created:
        try:
            container = client.containers.get(name)
            container.stop(timeout=3)
            container.remove(v=True, force=True)
        except docker.errors.NotFound:
            pass
    try:
        client.networks.get("oduflow-itest-net").remove()
    except docker.errors.NotFound:
        pass
    except docker.errors.APIError:
        # Another container on this daemon is still attached; leaving the
        # network behind is harmless and must not fail the test.
        pass


def test_internal_only_nats_is_private_but_reachable(live):
    settings, team, client, created = live

    result = service_ops.create_service(
        settings, team, "nats", NATS_IMAGE, None, internal_only=True
    )
    container_name = result["container_name"]
    created.append(container_name)

    container = client.containers.get(container_name)
    container.reload()

    # 1. No host port binding — nothing is listening on a host interface.
    host_config = container.attrs["HostConfig"]
    assert not (host_config.get("PortBindings") or {})
    assert not host_config.get("PublishAllPorts")
    published = {
        port: mappings
        for port, mappings in (
            container.attrs["NetworkSettings"].get("Ports") or {}
        ).items()
        if mappings
    }
    assert published == {}

    # 2. No Traefik router/service/middleware — only the explicit opt-out.
    traefik_labels = {
        key: value
        for key, value in container.labels.items()
        if key.startswith("traefik.")
    }
    assert traefik_labels == {"traefik.enable": "false"}
    assert container.labels["oduflow.internal_only"] == "true"

    # 3. No hostname at all.
    assert result["url"] is None
    assert result.get("hostname") is None

    # 4. Attached to the team network under its ordinary container name.
    networks = container.attrs["NetworkSettings"]["Networks"]
    assert "oduflow-itest-net" in networks

    # ...and actually reachable there: open a TCP connection to the NATS
    # client port from a sibling container, by container name.
    peer = client.containers.run(
        PEER_IMAGE,
        command=["sh", "-c", f"nc -z -w 5 {container_name} 4222 && echo REACHED"],
        network="oduflow-itest-net",
        detach=True,
    )
    created.append(peer.name)
    peer.wait(timeout=30)
    assert "REACHED" in peer.logs().decode()

    # Introspection reports the mode rather than an empty public config.
    info = service_ops.get_service_info(settings, team, "nats")
    assert info["internal_only"] is True
    assert info["hostname"] is None
    assert info["port"] is None
    assert info["url"] is None


def test_switching_to_internal_only_removes_exposure(live):
    settings, team, client, created = live

    service_ops.create_service(settings, team, "nats", NATS_IMAGE, 8222)
    created.append("oduflow-itest-svc-nats")

    published = client.containers.get("oduflow-itest-svc-nats")
    assert any(key.startswith("traefik.http.routers.") for key in published.labels), (
        "precondition: the published service has a Traefik router"
    )

    service_ops.update_service(settings, team, "nats", internal_only_override=True)

    recreated = client.containers.get("oduflow-itest-svc-nats")
    recreated.reload()
    assert not (recreated.attrs["HostConfig"].get("PortBindings") or {})
    assert {
        key: value
        for key, value in recreated.labels.items()
        if key.startswith("traefik.")
    } == {"traefik.enable": "false"}
    assert recreated.labels["oduflow.internal_only"] == "true"


def _traefik_api(host_port: int, path: str, *, timeout: float = 2.0):
    """GET a Traefik API endpoint, returning parsed JSON or None if not ready."""
    url = f"http://127.0.0.1:{host_port}/api{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None


def _wait_for_router(host_port: int, needle: str, *, deadline_s: float = 45.0):
    """Poll Traefik until a router whose name contains *needle* is registered.

    Returns the full router list once the marker appears; raises otherwise. The
    marker is a *published* service, so its presence proves Traefik has scanned
    the Docker daemon — which is what makes the subsequent absence assertion
    meaningful rather than merely early.
    """
    end = time.monotonic() + deadline_s
    last = None
    while time.monotonic() < end:
        routers = _traefik_api(host_port, "/http/routers")
        if routers:
            last = routers
            if any(needle in str(r.get("name", "")) for r in routers):
                return routers
        time.sleep(1.0)
    raise AssertionError(
        f"Traefik never registered a router matching {needle!r} within "
        f"{deadline_s}s; last seen: {last!r}"
    )


def test_internal_only_absent_from_traefik_dynamic_configuration(live):
    """The opt-out label holds even when the proxy exposes everything by default.

    Oduflow's own Traefik runs with ``exposedByDefault=false``, so a label-less
    container gets no router regardless. This test removes that safety net on
    purpose: with ``exposedByDefault=true`` every container is published unless
    it says otherwise, so a router appearing here would mean the internal-only
    guarantee rests entirely on a setting living in another container.
    """
    settings, team, client, created = live

    # Positive control: an ordinary published service. It must be a
    # long-running image — Traefik only routes running containers, so a
    # short-lived one would make the control silently vacuous.
    service_ops.create_service(settings, team, "web", NATS_IMAGE, 8222)
    created.append("oduflow-itest-svc-web")

    internal = service_ops.create_service(
        settings, team, "nats", NATS_IMAGE, None, internal_only=True
    )
    created.append(internal["container_name"])

    traefik = client.containers.run(
        TRAEFIK_IMAGE,
        name="oduflow-itest-traefik",
        detach=True,
        network="oduflow-itest-net",
        # Bound to loopback: the API must not be reachable off this machine.
        ports={"8080/tcp": ("127.0.0.1", None)},
        volumes={
            "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "ro"}
        },
        command=[
            "--log.level=INFO",
            "--api.insecure=true",
            "--providers.docker=true",
            # Deliberately the opposite of Oduflow's own configuration.
            "--providers.docker.exposedbydefault=true",
            "--entrypoints.web.address=:80",
        ],
    )
    created.append(traefik.name)
    traefik.reload()
    host_port = int(
        traefik.attrs["NetworkSettings"]["Ports"]["8080/tcp"][0]["HostPort"]
    )

    routers = _wait_for_router(host_port, "oduflow-itest-svc-web")

    # The published service is routed...
    assert any("oduflow-itest-svc-web" in str(r.get("name", "")) for r in routers)

    # ...and the internal-only one is nowhere in the dynamic configuration.
    offending_routers = [
        r for r in routers if "oduflow-itest-svc-nats" in json.dumps(r)
    ]
    assert offending_routers == [], (
        f"internal-only service leaked into Traefik routers: {offending_routers}"
    )

    services = _traefik_api(host_port, "/http/services") or []
    offending_services = [
        s for s in services if "oduflow-itest-svc-nats" in json.dumps(s)
    ]
    assert offending_services == [], (
        f"internal-only service leaked into Traefik services: {offending_services}"
    )
