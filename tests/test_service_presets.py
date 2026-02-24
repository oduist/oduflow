import json
import os
import pytest
from oduflow.docker_ops import service_presets
from oduflow.errors import NotFoundError
from oduflow.settings import Settings


@pytest.fixture
def tmp_settings(tmp_path):
    return Settings(
        data_dir=str(tmp_path),
        workspaces_dir=str(tmp_path / "workspaces"),
        port_registry_path=str(tmp_path / "ports.json"),
    )


# ── TestSavePreset ──────────────────────────────────────────────────────


class TestSavePreset:
    def test_save_preset_basic(self, tmp_settings):
        service_presets.save_preset(tmp_settings, "redis", "redis:7", 6379)
        path = service_presets._presets_path(tmp_settings)
        assert os.path.isfile(path)
        with open(path) as fh:
            data = json.load(fh)
        assert "redis" in data
        assert data["redis"]["image"] == "redis:7"
        assert data["redis"]["port"] == 6379
        assert data["redis"]["hostname"] == ""
        assert data["redis"]["env_vars"] == {}

    def test_save_preset_with_env_vars(self, tmp_settings):
        env = {"MEILI_MASTER_KEY": "abc", "MEILI_ENV": "production"}
        service_presets.save_preset(
            tmp_settings, "meili", "getmeili/meilisearch:v1.6", 7700, env_vars=env
        )
        preset = service_presets.get_preset(tmp_settings, "meili")
        assert preset["env_vars"] == env

    def test_save_preset_with_hostname(self, tmp_settings):
        service_presets.save_preset(
            tmp_settings, "redis", "redis:7", 6379, hostname="redis.local"
        )
        preset = service_presets.get_preset(tmp_settings, "redis")
        assert preset["hostname"] == "redis.local"

    def test_save_preset_overwrites(self, tmp_settings):
        service_presets.save_preset(tmp_settings, "redis", "redis:6", 6379)
        service_presets.save_preset(tmp_settings, "redis", "redis:7", 6380)
        preset = service_presets.get_preset(tmp_settings, "redis")
        assert preset["image"] == "redis:7"
        assert preset["port"] == 6380

    def test_save_preset_creates_dir(self, tmp_path):
        nested = tmp_path / "deep" / "nested"
        settings = Settings(
            data_dir=str(nested),
            workspaces_dir=str(nested / "workspaces"),
            port_registry_path=str(nested / "ports.json"),
        )
        assert not nested.exists()
        service_presets.save_preset(settings, "redis", "redis:7", 6379)
        assert os.path.isfile(service_presets._presets_path(settings))

    def test_save_preset_multiple(self, tmp_settings):
        service_presets.save_preset(tmp_settings, "redis", "redis:7", 6379)
        service_presets.save_preset(
            tmp_settings, "meili", "getmeili/meilisearch:v1.6", 7700
        )
        with open(service_presets._presets_path(tmp_settings)) as fh:
            data = json.load(fh)
        assert "redis" in data
        assert "meili" in data


# ── TestListPresets ─────────────────────────────────────────────────────


class TestListPresets:
    def test_list_empty(self, tmp_settings):
        assert service_presets.list_presets(tmp_settings) == []

    def test_list_presets(self, tmp_settings):
        service_presets.save_preset(tmp_settings, "zz-last", "img:1", 1111)
        service_presets.save_preset(tmp_settings, "aa-first", "img:2", 2222)
        result = service_presets.list_presets(tmp_settings)
        assert len(result) == 2
        assert result[0]["name"] == "aa-first"
        assert result[1]["name"] == "zz-last"

    def test_list_preset_format(self, tmp_settings):
        service_presets.save_preset(
            tmp_settings,
            "redis",
            "redis:7",
            6379,
            hostname="r.local",
            env_vars={"K": "V"},
        )
        items = service_presets.list_presets(tmp_settings)
        assert len(items) == 1
        item = items[0]
        for key in ("name", "image", "port", "hostname", "env_vars"):
            assert key in item, f"Missing key: {key}"


# ── TestGetPreset ───────────────────────────────────────────────────────


class TestGetPreset:
    def test_get_preset(self, tmp_settings):
        service_presets.save_preset(
            tmp_settings,
            "redis",
            "redis:7",
            6379,
            hostname="r.local",
            env_vars={"A": "B"},
        )
        preset = service_presets.get_preset(tmp_settings, "redis")
        assert preset["name"] == "redis"
        assert preset["image"] == "redis:7"
        assert preset["port"] == 6379
        assert preset["hostname"] == "r.local"
        assert preset["env_vars"] == {"A": "B"}

    def test_get_preset_not_found(self, tmp_settings):
        with pytest.raises(NotFoundError):
            service_presets.get_preset(tmp_settings, "nonexistent")


# ── TestDeletePreset ────────────────────────────────────────────────────


class TestDeletePreset:
    def test_delete_preset(self, tmp_settings):
        service_presets.save_preset(tmp_settings, "redis", "redis:7", 6379)
        result = service_presets.delete_preset(tmp_settings, "redis")
        assert result == {"name": "redis"}
        with open(service_presets._presets_path(tmp_settings)) as fh:
            data = json.load(fh)
        assert "redis" not in data

    def test_delete_preset_not_found(self, tmp_settings):
        with pytest.raises(NotFoundError):
            service_presets.delete_preset(tmp_settings, "nonexistent")

    def test_delete_preserves_others(self, tmp_settings):
        service_presets.save_preset(tmp_settings, "redis", "redis:7", 6379)
        service_presets.save_preset(tmp_settings, "meili", "meili:1", 7700)
        service_presets.delete_preset(tmp_settings, "redis")
        remaining = service_presets.list_presets(tmp_settings)
        assert len(remaining) == 1
        assert remaining[0]["name"] == "meili"


# ── TestLoadPresetsCorruptFile ──────────────────────────────────────────


class TestLoadPresetsCorruptFile:
    def test_corrupt_json(self, tmp_settings):
        path = service_presets._presets_path(tmp_settings)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write("NOT VALID JSON {{{")
        result = service_presets._load_presets(tmp_settings)
        assert result == {}
