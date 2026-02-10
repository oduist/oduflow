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
