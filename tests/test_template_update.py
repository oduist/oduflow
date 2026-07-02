"""Tests for non-destructive template updates (issue #2).

The core under test is ``env_ops.remount_template_overlays`` — the context
manager that unmounts overlay environments (keeping their ``upper`` deltas),
lets the caller swap the template's lower filestore layer, and remounts them.
"""

import contextlib
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import docker
import pytest

from oduflow.docker_ops import env_ops, system_ops
from oduflow.naming import get_filestore_paths, get_resource_name
from oduflow.settings import Settings, TeamSettings


def _make_team_and_settings(tmp_path):
    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path),
        port_registry_path=str(tmp_path / "ports.json"),
    )
    settings = Settings(base_data_dir=str(tmp_path), teams={"1": team})
    return team, settings


class FakeContainer:
    def __init__(self, status="running", tags=("odoo:17.0",)):
        self.status = status
        self.image = SimpleNamespace(tags=list(tags))
        self.stop_calls = 0
        self.start_calls = 0

    def stop(self, timeout=None):
        self.stop_calls += 1
        self.status = "exited"

    def start(self):
        self.start_calls += 1
        self.status = "running"


class FakeContainers:
    def __init__(self, mapping):
        self.mapping = mapping

    def get(self, name):
        try:
            return self.mapping[name]
        except KeyError:
            raise docker.errors.NotFound(name)


class FakeClient:
    def __init__(self, mapping):
        self.containers = FakeContainers(mapping)


def _build(tmp_path, envs, mounted_envs, statuses=None):
    """Set up workspaces + fakes.

    ``envs``: list of (env_name, template_name).
    ``mounted_envs``: iterable of env names whose overlay is currently mounted.
    ``statuses``: optional dict env_name -> container status.
    """
    statuses = statuses or {}
    team, settings = _make_team_and_settings(tmp_path)
    mounted: set[str] = set()
    mapping: dict[str, FakeContainer] = {}
    env_dicts: list[dict] = []
    for env_name, tpl in envs:
        paths = get_filestore_paths(env_name, team.workspaces_dir)
        os.makedirs(paths["upper"], exist_ok=True)
        os.makedirs(paths["work"], exist_ok=True)
        with open(os.path.join(paths["upper"], "marker.txt"), "w") as f:
            f.write("delta")
        env_dicts.append(
            {"env_name": env_name, "template_name": tpl, "odoo_image": "odoo:17.0"}
        )
        mapping[get_resource_name(env_name, "odoo", settings.prefix, team.team_id)] = (
            FakeContainer(status=statuses.get(env_name, "running"))
        )
        if env_name in mounted_envs:
            mounted.add(paths["merged"])
    client = FakeClient(mapping)
    return team, settings, client, mounted, env_dicts, mapping


@contextlib.contextmanager
def _patched(team, env_dicts, mounted):
    """Patch list_environments, (un)mount primitives and os.path.ismount."""

    def fake_unmount(env_name, team_):
        mounted.discard(get_filestore_paths(env_name, team_.workspaces_dir)["merged"])

    mount_mock = MagicMock()
    with (
        patch.object(env_ops, "list_environments", return_value=env_dicts),
        patch.object(
            env_ops, "_unmount_filestore", side_effect=fake_unmount
        ) as unmount_mock,
        patch.object(env_ops, "_mount_filestore", mount_mock),
        patch("os.path.ismount", side_effect=lambda p: p in mounted),
    ):
        yield unmount_mock, mount_mock


def _marker(team, env_name):
    return os.path.join(
        get_filestore_paths(env_name, team.workspaces_dir)["upper"], "marker.txt"
    )


class TestRemountTemplateOverlays:
    def test_preserves_upper_by_default(self, tmp_path):
        team, settings, client, mounted, env_dicts, mapping = _build(
            tmp_path, [("env1", "t"), ("env2", "t")], {"env1", "env2"}
        )
        with _patched(team, env_dicts, mounted) as (unmount_mock, mount_mock):
            with env_ops.remount_template_overlays(
                client, settings, team, "t"
            ) as result:
                pass

        assert set(result.affected) == {"env1", "env2"}
        assert result.failures == []
        # Deltas preserved.
        assert os.path.exists(_marker(team, "env1"))
        assert os.path.exists(_marker(team, "env2"))
        # Each env unmounted + remounted.
        assert unmount_mock.call_count == 2
        assert mount_mock.call_count == 2
        # Containers cycled.
        for c in mapping.values():
            assert c.stop_calls == 1
            assert c.start_calls == 1

    def test_reset_upper_wipes_deltas(self, tmp_path):
        team, settings, client, mounted, env_dicts, _ = _build(
            tmp_path, [("env1", "t")], {"env1"}
        )
        with _patched(team, env_dicts, mounted) as (_, mount_mock):
            with env_ops.remount_template_overlays(
                client, settings, team, "t", reset_upper=True
            ):
                pass

        # Delta removed, upper dir recreated empty.
        assert not os.path.exists(_marker(team, "env1"))
        assert os.path.isdir(get_filestore_paths("env1", team.workspaces_dir)["upper"])
        assert mount_mock.call_count == 1

    def test_skips_other_template_and_unmounted(self, tmp_path):
        team, settings, client, mounted, env_dicts, _ = _build(
            tmp_path,
            [("env1", "t"), ("env2", "other"), ("env3", "t")],
            {"env1", "env2"},  # env3 uses t but is NOT mounted
        )
        with _patched(team, env_dicts, mounted) as (unmount_mock, mount_mock):
            with env_ops.remount_template_overlays(
                client, settings, team, "t"
            ) as result:
                pass

        assert result.affected == ["env1"]
        assert unmount_mock.call_count == 1
        assert mount_mock.call_count == 1

    def test_exclude_envs(self, tmp_path):
        team, settings, client, mounted, env_dicts, _ = _build(
            tmp_path, [("env1", "t"), ("env2", "t")], {"env1", "env2"}
        )
        with _patched(team, env_dicts, mounted) as (unmount_mock, mount_mock):
            with env_ops.remount_template_overlays(
                client, settings, team, "t", exclude_envs=("env1",)
            ) as result:
                pass

        assert result.affected == ["env2"]
        # env1 untouched.
        assert os.path.exists(_marker(team, "env1"))
        assert unmount_mock.call_count == 1
        assert mount_mock.call_count == 1

    def test_remounts_even_if_block_raises(self, tmp_path):
        team, settings, client, mounted, env_dicts, _ = _build(
            tmp_path, [("env1", "t")], {"env1"}
        )
        with _patched(team, env_dicts, mounted) as (_, mount_mock):
            with pytest.raises(ValueError):
                with env_ops.remount_template_overlays(client, settings, team, "t"):
                    raise ValueError("boom")
        # finally still remounted.
        assert mount_mock.call_count == 1

    def test_stopped_env_not_restarted(self, tmp_path):
        team, settings, client, mounted, env_dicts, mapping = _build(
            tmp_path,
            [("env1", "t")],
            {"env1"},
            statuses={"env1": "exited"},
        )
        with _patched(team, env_dicts, mounted) as (unmount_mock, mount_mock):
            with env_ops.remount_template_overlays(client, settings, team, "t"):
                pass

        # Still unmounted + remounted, but never started back up.
        assert unmount_mock.call_count == 1
        assert mount_mock.call_count == 1
        container = mapping[get_resource_name("env1", "odoo", settings.prefix, "1")]
        assert container.start_calls == 0

    def test_partial_unmount_failure_isolated(self, tmp_path):
        team, settings, client, mounted, env_dicts, _ = _build(
            tmp_path, [("env1", "t"), ("env2", "t")], {"env1", "env2"}
        )

        def fake_unmount(env_name, team_):
            if env_name == "env1":
                raise RuntimeError("device busy")
            mounted.discard(
                get_filestore_paths(env_name, team_.workspaces_dir)["merged"]
            )

        mount_mock = MagicMock()
        with (
            patch.object(env_ops, "list_environments", return_value=env_dicts),
            patch.object(env_ops, "_unmount_filestore", side_effect=fake_unmount),
            patch.object(env_ops, "_mount_filestore", mount_mock),
            patch("os.path.ismount", side_effect=lambda p: p in mounted),
        ):
            with env_ops.remount_template_overlays(
                client, settings, team, "t"
            ) as result:
                pass

        failed_envs = {env for env, _ in result.failures}
        assert "env1" in failed_envs
        # env2 still remounted; env1 (still mounted) skipped.
        assert mount_mock.call_count == 1

    def test_no_affected_envs(self, tmp_path):
        team, settings, client, mounted, env_dicts, _ = _build(
            tmp_path, [("env1", "other")], set()
        )
        with _patched(team, env_dicts, mounted) as (unmount_mock, mount_mock):
            with env_ops.remount_template_overlays(
                client, settings, team, "t"
            ) as result:
                pass

        assert result.affected == []
        assert unmount_mock.call_count == 0
        assert mount_mock.call_count == 0


class TestRefreshTemplate:
    def _fake_remount(self, captured, affected):
        @contextlib.contextmanager
        def _cm(client, settings, team, name, *, reset_upper=False, exclude_envs=()):
            captured["reset_upper"] = reset_upper
            captured["name"] = name
            captured["exclude_envs"] = exclude_envs
            yield env_ops.RemountResult(list(affected))

        return _cm

    def test_preserves_by_default(self, tmp_path):
        team, settings = _make_team_and_settings(tmp_path)
        captured: dict = {}
        with (
            patch.object(system_ops, "get_client", return_value=MagicMock()),
            patch.object(
                env_ops,
                "remount_template_overlays",
                self._fake_remount(captured, ["env1", "env2"]),
            ),
        ):
            out = system_ops.refresh_template(settings, team, "t")

        assert captured["reset_upper"] is False
        assert captured["name"] == "t"
        assert out["status"] == "refreshed"
        assert out["affected_envs"] == ["env1", "env2"]
        assert out["reset_env_changes"] is False

    def test_reset_forwarded(self, tmp_path):
        team, settings = _make_team_and_settings(tmp_path)
        captured: dict = {}
        with (
            patch.object(system_ops, "get_client", return_value=MagicMock()),
            patch.object(
                env_ops,
                "remount_template_overlays",
                self._fake_remount(captured, []),
            ),
        ):
            out = system_ops.refresh_template(
                settings, team, "t", reset_env_changes=True
            )

        assert captured["reset_upper"] is True
        assert out["affected_envs"] == []
        assert out["reset_env_changes"] is True
