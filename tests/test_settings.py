import pytest
from oduflow.settings import Settings


class TestSettings:
    def test_defaults(self):
        s = Settings()
        assert s.external_host == "localhost"
        assert s.port_range_start == 50000
        assert s.port_range_end == 50100
        assert s.db_user == "odoo"

    def test_validate_port_range(self):
        s = Settings(port_range_start=50100, port_range_end=50000, workspaces_dir="/tmp")
        with pytest.raises(ValueError, match="Invalid port range"):
            s.validate()

    def test_validate_workspaces_dir(self):
        s = Settings(workspaces_dir="")
        with pytest.raises(ValueError, match="workspaces_dir must be set"):
            s.validate()

    def test_validate_ok(self):
        s = Settings(workspaces_dir="/tmp")
        s.validate()

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("EXTERNAL_HOST", "10.0.0.1")
        monkeypatch.setenv("PORT_RANGE_START", "60000")
        monkeypatch.setenv("PORT_RANGE_END", "60100")
        s = Settings.from_env()
        assert s.external_host == "10.0.0.1"
        assert s.port_range_start == 60000
        assert s.port_range_end == 60100

    def test_frozen(self):
        s = Settings()
        with pytest.raises(AttributeError):
            s.external_host = "changed"  # type: ignore[misc]


class TestRefPaths:
    def test_get_ref_dir_default(self):
        s = Settings(home="/srv/data")
        assert s.get_ref_dir() == "/srv/data/refs/default"

    def test_get_ref_dir_named(self):
        s = Settings(home="/srv/data")
        assert s.get_ref_dir("myproject") == "/srv/data/refs/myproject"

    def test_get_ref_sql_path(self):
        s = Settings(home="/srv/data")
        assert s.get_ref_sql_path("v17") == "/srv/data/refs/v17/dump.sql"

    def test_get_ref_filestore_path(self):
        s = Settings(home="/srv/data")
        assert s.get_ref_filestore_path("v17") == "/srv/data/refs/v17/filestore"

    def test_dump_methods_delegate_to_ref(self):
        s = Settings(home="/srv/data")
        assert s.get_dump_sql_path() == s.get_ref_sql_path("default")
        assert s.get_dump_filestore_path() == s.get_ref_filestore_path("default")

    def test_list_refs_empty(self, tmp_path):
        s = Settings(home=str(tmp_path))
        assert s.list_refs() == []

    def test_list_refs(self, tmp_path):
        refs_dir = tmp_path / "refs"
        (refs_dir / "alpha").mkdir(parents=True)
        (refs_dir / "beta").mkdir(parents=True)
        (refs_dir / "not-a-dir").touch()  # should be ignored
        s = Settings(home=str(tmp_path))
        assert s.list_refs() == ["alpha", "beta"]
