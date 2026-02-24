import json
import pytest
from oduflow.settings import Settings
from oduflow.docker_ops.system_ops import _file_size_mb, _update_template_sizes


class TestFileSizeMb:
    def test_existing_file(self, tmp_path):
        f = tmp_path / "data.bin"
        f.write_bytes(b"\x00" * 2 * 1024 * 1024)  # 2 MB
        assert abs(_file_size_mb(str(f)) - 2.0) < 0.01

    def test_missing_file(self):
        assert _file_size_mb("/nonexistent/path/file.dat") == 0.0

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty"
        f.touch()
        assert _file_size_mb(str(f)) == 0.0


class TestUpdateTemplateSizes:
    def _make_template(self, tmp_path, name="default"):
        tpl_dir = tmp_path / "templates" / name
        tpl_dir.mkdir(parents=True)
        # Create filestore with some files
        fs_dir = tpl_dir / "filestore"
        fs_dir.mkdir()
        (fs_dir / "a.bin").write_bytes(b"\x00" * 1024 * 512)  # 0.5 MB
        (fs_dir / "b.bin").write_bytes(b"\x00" * 1024 * 512)  # 0.5 MB
        # Create dump
        dump = tpl_dir / "dump.pgdump"
        dump.write_bytes(b"\x00" * 1024 * 256)  # 0.25 MB
        return Settings(data_dir=str(tmp_path))

    def test_sizes_saved_to_metadata(self, tmp_path):
        settings = self._make_template(tmp_path)
        _update_template_sizes(settings, "default")

        meta_path = settings.get_template_metadata_path("default")
        with open(meta_path) as f:
            meta = json.load(f)

        assert abs(meta["filestore_size_mb"] - 1.0) < 0.1
        assert abs(meta["dump_size_mb"] - 0.25) < 0.05

    def test_preserves_existing_metadata(self, tmp_path):
        settings = self._make_template(tmp_path)
        meta_path = settings.get_template_metadata_path("default")
        with open(meta_path, "w") as f:
            json.dump({"odoo_image": "odoo:17.0", "use_overlay": False}, f)

        _update_template_sizes(settings, "default")

        with open(meta_path) as f:
            meta = json.load(f)

        assert meta["odoo_image"] == "odoo:17.0"
        assert meta["use_overlay"] is False
        assert "filestore_size_mb" in meta
        assert "dump_size_mb" in meta

    def test_with_provided_metadata(self, tmp_path):
        settings = self._make_template(tmp_path)
        provided = {"odoo_image": "odoo:18.0", "custom_key": "value"}
        result = _update_template_sizes(settings, "default", metadata=provided)

        assert result["odoo_image"] == "odoo:18.0"
        assert result["custom_key"] == "value"
        assert "filestore_size_mb" in result
        assert "dump_size_mb" in result

    def test_no_filestore(self, tmp_path):
        tpl_dir = tmp_path / "templates" / "bare"
        tpl_dir.mkdir(parents=True)
        (tpl_dir / "dump.pgdump").write_bytes(b"\x00" * 1024 * 1024)  # 1 MB
        settings = Settings(data_dir=str(tmp_path))

        _update_template_sizes(settings, "bare")

        with open(settings.get_template_metadata_path("bare")) as f:
            meta = json.load(f)

        assert meta["filestore_size_mb"] == 0.0
        assert meta["dump_size_mb"] == 1.0

    def test_no_dump(self, tmp_path):
        tpl_dir = tmp_path / "templates" / "nodump"
        fs_dir = tpl_dir / "filestore"
        fs_dir.mkdir(parents=True)
        (fs_dir / "file.bin").write_bytes(b"\x00" * 1024 * 1024)  # 1 MB
        settings = Settings(data_dir=str(tmp_path))

        _update_template_sizes(settings, "nodump")

        with open(settings.get_template_metadata_path("nodump")) as f:
            meta = json.load(f)

        assert meta["filestore_size_mb"] == 1.0
        assert meta["dump_size_mb"] == 0.0
