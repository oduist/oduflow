import pytest
from unittest.mock import patch, MagicMock


class TestInitSystemTool:
    @patch("flow.odoo_manager.init_system")
    def test_init_system(self, mock_init):
        mock_init.return_value = {"status": "initialized", "template_db": "odoo_ref"}
        from flow.server import init_system
        result = init_system("/tmp/dump.dump", "15.0", False)
        assert "initialized" in result
        mock_init.assert_called_once_with(dump_path="/tmp/dump.dump", version="15.0", force=False)

    @patch("flow.odoo_manager.init_system")
    def test_init_system_empty_path(self, mock_init):
        mock_init.return_value = {"status": "initialized", "template_db": "odoo_ref"}
        from flow.server import init_system
        result = init_system("", "15.0", False)
        mock_init.assert_called_once_with(dump_path=None, version="15.0", force=False)


class TestDestroySystemTool:
    @patch("flow.odoo_manager.destroy_system")
    def test_destroy(self, mock_destroy):
        mock_destroy.return_value = {"status": "destroyed", "removed": "flow-db, flow-db-data, flow-net"}
        from flow.server import destroy_system
        result = destroy_system()
        assert "destroyed" in result


class TestCreateEnvironmentTool:
    @patch("flow.odoo_manager.create_environment")
    def test_create(self, mock_create):
        mock_create.return_value = {
            "url": "http://localhost:50000",
            "odoo_container": "flow-main-odoo",
            "database": "flow_main",
            "workspace": "/tmp/ws",
        }
        from flow.server import create_environment
        result = create_environment("main", "https://repo.url")
        assert "Environment provisioned successfully!" in result
        assert "Database: flow_main" in result


class TestDeleteEnvironmentTool:
    @patch("flow.odoo_manager.delete_environment")
    def test_delete(self, mock_delete):
        from flow.server import delete_environment
        result = delete_environment("main")
        assert "torn down" in result
        mock_delete.assert_called_once_with("main")


class TestListEnvironmentsTool:
    @patch("flow.odoo_manager.list_environments")
    def test_list(self, mock_list):
        mock_list.return_value = [
            {
                "branch": "main",
                "status": "running",
                "url": "http://localhost:50000",
                "containers": [{"name": "flow-main-odoo", "status": "running", "image": "odoo:15.0"}],
            }
        ]
        from flow.server import list_environments
        result = list_environments()
        assert "main" in result
        assert "flow-main-odoo" in result

    @patch("flow.odoo_manager.list_environments")
    def test_list_empty(self, mock_list):
        mock_list.return_value = []
        from flow.server import list_environments
        result = list_environments()
        assert "No active" in result


class TestStatusTool:
    @patch("flow.odoo_manager.get_environment_status")
    def test_status(self, mock_status):
        mock_status.return_value = {
            "branch": "main",
            "all_running": True,
            "odoo": {"status": "running", "running": True},
            "db": {"status": "running", "running": True},
        }
        from flow.server import get_environment_status
        result = get_environment_status("main")
        assert "All containers running" in result
        assert "DB (shared)" in result
