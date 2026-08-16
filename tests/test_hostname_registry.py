from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

import docker
from oduflow.docker_ops import env_ops
from oduflow.errors import ConflictError, FlowError
from oduflow.hostname_registry import (
    allocate_hostname,
    get_hostname,
    release_hostname,
)
from oduflow.settings import Settings, TeamSettings


def _path(tmp_path):
    return str(tmp_path / "hostnames.json")


def test_automatic_slots_are_stable_and_reusable(tmp_path):
    path = _path(tmp_path)

    assert allocate_hostname(path, "feature-a", 2, hostname_prefix="dev") == "dev1"
    assert allocate_hostname(path, "feature-b", 2, hostname_prefix="dev") == "dev2"
    assert allocate_hostname(path, "feature-a", 2, hostname_prefix="dev") == "dev1"

    release_hostname(path, "feature-a")
    assert get_hostname(path, "feature-a") is None
    assert allocate_hostname(path, "feature-c", 2, hostname_prefix="dev") == "dev1"


def test_explicit_hostname_consumes_capacity_and_cannot_collide(tmp_path):
    path = _path(tmp_path)

    assert (
        allocate_hostname(
            path,
            "feature-a",
            2,
            requested_hostname="preview",
            hostname_prefix="dev",
        )
        == "preview"
    )
    with pytest.raises(ConflictError, match="already used"):
        allocate_hostname(
            path,
            "feature-b",
            2,
            requested_hostname="preview",
            hostname_prefix="dev",
        )

    assert allocate_hostname(path, "feature-b", 2, hostname_prefix="dev") == "dev1"
    with pytest.raises(FlowError, match="No free environment slots"):
        allocate_hostname(path, "feature-c", 2, hostname_prefix="dev")


def test_active_legacy_environments_count_toward_capacity(tmp_path):
    with pytest.raises(FlowError, match="configured: 2"):
        allocate_hostname(
            _path(tmp_path),
            "feature-c",
            2,
            active_envs={"feature-a", "feature-b"},
            used_hostnames={"feature-a", "feature-b"},
            hostname_prefix="dev",
        )


def test_parallel_allocations_never_share_a_slot(tmp_path):
    path = _path(tmp_path)
    envs = [f"feature-{number}" for number in range(20)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        hostnames = list(
            pool.map(
                lambda env: allocate_hostname(
                    path, env, len(envs), hostname_prefix="dev"
                ),
                envs,
            )
        )

    assert len(set(hostnames)) == len(envs)
    assert set(hostnames) == {f"dev{number}" for number in range(1, 21)}


def _traefik_settings(tmp_path, slots=2):
    data_dir = tmp_path / "team_1"
    team = TeamSettings(
        team_id="1",
        hostname="dev.example.com",
        data_dir=str(data_dir),
        hostname_registry_path=str(data_dir / "hostnames.json"),
        environment_slots=slots,
    )
    return Settings(
        routing_mode="traefik",
        routing_tls=False,
        teams={"1": team},
    ), team


def test_create_wrapper_assigns_short_hostname(tmp_path):
    settings, team = _traefik_settings(tmp_path)
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("missing")
    client.containers.list.return_value = []

    with (
        patch("oduflow.docker_ops.env_ops.get_client", return_value=client),
        patch(
            "oduflow.docker_ops.env_ops._create_environment_impl",
            return_value={"url": "https://dev1.example.com"},
        ) as provision,
    ):
        result = env_ops.create_environment(
            settings, team, "feature-a", "repo", "odoo:19.0"
        )

    assert result["url"] == "https://dev1.example.com"
    assert provision.call_args.kwargs["hostname"] == "dev1"
    assert get_hostname(team.hostname_registry_path, "feature-a") == "dev1"


def test_create_wrapper_accepts_explicit_parent_domain_prefix(tmp_path):
    settings, team = _traefik_settings(tmp_path)
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("missing")
    client.containers.list.return_value = []

    with (
        patch("oduflow.docker_ops.env_ops.get_client", return_value=client),
        patch(
            "oduflow.docker_ops.env_ops._create_environment_impl",
            return_value={"url": "https://qa.example.com"},
        ) as provision,
    ):
        env_ops.create_environment(
            settings,
            team,
            "feature-a",
            "repo",
            "odoo:19.0",
            hostname="qa",
        )

    assert provision.call_args.kwargs["hostname"] == "qa"
    assert get_hostname(team.hostname_registry_path, "feature-a") == "qa"


def test_automatic_allocation_skips_hostname_used_by_service(tmp_path):
    settings, team = _traefik_settings(tmp_path)
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("missing")
    service = MagicMock()
    service.labels = {
        "oduflow.managed": "true",
        "oduflow.team": "1",
        "traefik.http.routers.oduflow-1-service-dev1.rule": (
            "Host(`dev1.example.com`)"
        ),
    }
    client.containers.list.return_value = [service]

    with (
        patch("oduflow.docker_ops.env_ops.get_client", return_value=client),
        patch(
            "oduflow.docker_ops.env_ops._create_environment_impl",
            return_value={"url": "https://dev2.example.com"},
        ) as provision,
    ):
        env_ops.create_environment(settings, team, "feature-a", "repo", "odoo:19.0")

    assert provision.call_args.kwargs["hostname"] == "dev2"


def test_failed_create_releases_hostname_before_container_exists(tmp_path):
    settings, team = _traefik_settings(tmp_path)
    client = MagicMock()
    client.containers.get.side_effect = docker.errors.NotFound("missing")
    client.containers.list.return_value = []

    with (
        patch("oduflow.docker_ops.env_ops.get_client", return_value=client),
        patch(
            "oduflow.docker_ops.env_ops._create_environment_impl",
            side_effect=RuntimeError("boom"),
        ),
        pytest.raises(RuntimeError, match="boom"),
    ):
        env_ops.create_environment(settings, team, "feature-a", "repo", "odoo:19.0")

    assert get_hostname(team.hostname_registry_path, "feature-a") is None


def test_base_url_reads_persisted_short_hostname(tmp_path):
    settings, team = _traefik_settings(tmp_path)
    container = MagicMock()
    container.labels = {env_ops.ENV_HOSTNAME_LABEL: "dev2"}

    assert env_ops.get_env_base_url(settings, team, "feature-a", container) == (
        "https://dev2.example.com",
        "dev2.example.com",
    )
