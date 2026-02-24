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
        s = Settings(port_range_start=50100, port_range_end=50000)
        with pytest.raises(ValueError, match="Invalid port range"):
            s.validate()

    def test_validate_ok(self):
        s = Settings()
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


class TestTemplatePaths:
    def test_get_template_dir_default(self):
        s = Settings(data_dir="/srv/data")
        assert s.get_template_dir("default") == "/srv/data/templates/default"

    def test_get_template_dir_named(self):
        s = Settings(data_dir="/srv/data")
        assert s.get_template_dir("myproject") == "/srv/data/templates/myproject"

    def test_get_template_sql_path(self):
        s = Settings(data_dir="/srv/data")
        assert s.get_template_sql_path("v17") == "/srv/data/templates/v17/dump.pgdump"

    def test_get_template_filestore_path(self):
        s = Settings(data_dir="/srv/data")
        assert s.get_template_filestore_path("v17") == "/srv/data/templates/v17/filestore"

    def test_dump_methods_delegate_to_template(self):
        s = Settings(data_dir="/srv/data")
        assert s.get_template_sql_path("default") == "/srv/data/templates/default/dump.pgdump"
        assert s.get_template_filestore_path("default") == "/srv/data/templates/default/filestore"

    def test_list_templates_empty(self, tmp_path):
        s = Settings(data_dir=str(tmp_path))
        assert s.list_templates() == []

    def test_list_templates(self, tmp_path):
        templates_dir = tmp_path / "templates"
        (templates_dir / "alpha").mkdir(parents=True)
        (templates_dir / "beta").mkdir(parents=True)
        (templates_dir / "not-a-dir").touch()  # should be ignored
        s = Settings(data_dir=str(tmp_path))
        assert s.list_templates() == ["alpha", "beta"]
