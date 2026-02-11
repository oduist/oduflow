import pytest
from unittest.mock import patch, MagicMock

from oduflow.settings import Settings

TEST_SETTINGS = Settings(
    external_host="localhost",
    port_range_start=50000,
    port_range_end=50100,
    workspaces_dir="/tmp/flow-test/workspaces",
    home="/tmp/flow-test",
    db_user="odoo",
    db_password="odoo",
    port_registry_path="/tmp/flow-test/ports.json",
)


@pytest.fixture(autouse=True)
def inject_settings():
    import oduflow.server
    oduflow.server._settings = TEST_SETTINGS
    yield
    oduflow.server._settings = None


from tool_helpers import call_tool as _call_tool


class TestCLIInitDestroy:
    @patch("oduflow.docker_ops.system_ops.init_system")
    def test_cli_init(self, mock_init):
        from oduflow.server import _run_init
        mock_init.return_value = {"status": "initialized"}
        _run_init(TEST_SETTINGS, version="15.0", force=False)
        mock_init.assert_called_once_with(TEST_SETTINGS, version="15.0", force=False)

    @patch("oduflow.docker_ops.system_ops.destroy_system")
    def test_cli_destroy(self, mock_destroy):
        from oduflow.server import _run_destroy
        mock_destroy.return_value = {"status": "destroyed", "removed": "flow-db, flow-db-data, flow-net"}
        _run_destroy(TEST_SETTINGS)
        mock_destroy.assert_called_once_with(TEST_SETTINGS)


class TestCreateEnvironmentTool:
    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_create(self, mock_create):
        mock_create.return_value = {
            "url": "http://localhost:50000",
            "odoo_container": "oduflow-main-odoo",
            "database": "oduflow_main",
            "workspace": "/tmp/ws",
        }
        result = _call_tool("create_environment", branch_name="main", repo_url="https://repo.url", odoo_image="odoo:17.0")
        assert "Environment provisioned successfully!" in result
        assert "Database: oduflow_main" in result
        assert "Ref: default" in result


class TestDeleteEnvironmentTool:
    @patch("oduflow.docker_ops.env_ops.delete_environment")
    def test_delete(self, mock_delete):
        result = _call_tool("delete_environment", branch_name="main")
        assert "torn down" in result
        mock_delete.assert_called_once_with(TEST_SETTINGS, "main")


class TestListEnvironmentsTool:
    @patch("oduflow.docker_ops.env_ops.list_environments")
    def test_list(self, mock_list):
        mock_list.return_value = [
            {
                "branch": "main",
                "status": "running",
                "url": "http://localhost:50000",
                "containers": [{"name": "oduflow-main-odoo", "status": "running", "image": "odoo:15.0"}],
            }
        ]
        result = _call_tool("list_environments")
        assert "main" in result
        assert "oduflow-main-odoo" in result

    @patch("oduflow.docker_ops.env_ops.list_environments")
    def test_list_empty(self, mock_list):
        mock_list.return_value = []
        result = _call_tool("list_environments")
        assert "No active" in result


class TestStatusTool:
    @patch("oduflow.docker_ops.env_ops.get_environment_status")
    def test_status(self, mock_status):
        mock_status.return_value = {
            "branch": "main",
            "all_running": True,
            "odoo": {"status": "running", "running": True},
            "db": {"status": "running", "running": True},
        }
        result = _call_tool("get_environment_status", branch_name="main")
        assert "All containers running" in result
        assert "DB (shared)" in result


class TestErrorHandling:
    @patch("oduflow.docker_ops.env_ops.restart_environment")
    def test_flow_error_raises_value_error(self, mock_restart):
        from oduflow.errors import NotFoundError
        mock_restart.side_effect = NotFoundError("container not found")
        with pytest.raises(ValueError, match="container not found"):
            _call_tool("restart_environment", branch_name="main")


class TestMutex:
    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_busy_raises_value_error(self, mock_create):
        import oduflow.server
        oduflow.server._busy.acquire()
        try:
            with pytest.raises(ValueError, match="Another operation is in progress"):
                _call_tool("create_environment", branch_name="main", repo_url="https://x.git")
        finally:
            oduflow.server._busy.release()
