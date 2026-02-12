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
        _run_init(TEST_SETTINGS)
        mock_init.assert_called_once_with(TEST_SETTINGS)

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
            "database": "oduflow_1_main",
            "workspace": "/tmp/ws",
        }
        result = _call_tool("create_environment", branch_name="main", repo_url="https://repo.url", odoo_image="odoo:17.0")
        assert "Environment provisioned successfully!" in result
        assert "Database: oduflow_1_main" in result
        assert "Template: default" in result


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


def _get_tool_fn(tool_name: str):
    """Get the raw function for a registered MCP tool (avoids name collision with call_tool)."""
    from oduflow.server import mcp
    return mcp._tool_manager._tools[tool_name].fn


class TestCreateServiceTool:
    @patch("oduflow.docker_ops.service_ops.create_service")
    def test_create(self, mock_create):
        mock_create.return_value = {
            "name": "redis",
            "container_name": "oduflow-svc-redis",
            "url": "http://localhost:6379",
            "image": "redis:7",
        }
        result = _get_tool_fn("create_service")(name="redis", image="redis:7", port=6379)
        assert "Service created successfully!" in result
        assert "redis" in result
        assert "oduflow-svc-redis" in result
        assert "http://localhost:6379" in result

    @patch("oduflow.docker_ops.service_ops.create_service")
    def test_create_with_env_vars_parsing(self, mock_create):
        mock_create.return_value = {
            "name": "meili",
            "container_name": "oduflow-svc-meili",
            "url": "http://localhost:7700",
            "image": "getmeili/meilisearch:v1.6",
        }
        _get_tool_fn("create_service")(
            name="meili",
            image="getmeili/meilisearch:v1.6",
            port=7700,
            env_vars="MEILI_MASTER_KEY=abc,MEILI_ENV=production",
        )
        call_kwargs = mock_create.call_args
        parsed_env = call_kwargs[1]["env_vars"]
        assert parsed_env == {"MEILI_MASTER_KEY": "abc", "MEILI_ENV": "production"}

    @patch("oduflow.docker_ops.service_ops.create_service")
    def test_create_empty_env_vars(self, mock_create):
        mock_create.return_value = {
            "name": "redis",
            "container_name": "oduflow-svc-redis",
            "url": "http://localhost:6379",
            "image": "redis:7",
        }
        _get_tool_fn("create_service")(name="redis", image="redis:7", port=6379, env_vars="")
        call_kwargs = mock_create.call_args
        assert call_kwargs[1]["env_vars"] is None


class TestUpdateServiceTool:
    @patch("oduflow.docker_ops.service_ops.update_service")
    def test_update(self, mock_update):
        mock_update.return_value = {
            "name": "redis",
            "container_name": "oduflow-svc-redis",
            "url": "http://localhost:6379",
            "image": "redis:7",
        }
        result = _get_tool_fn("update_service")(name="redis")
        assert "Service updated successfully!" in result
        assert "redis" in result
        assert "oduflow-svc-redis" in result
        mock_update.assert_called_once_with(TEST_SETTINGS, "redis")

    @patch("oduflow.docker_ops.service_ops.update_service")
    def test_update_not_found(self, mock_update):
        from oduflow.errors import NotFoundError
        mock_update.side_effect = NotFoundError("Service 'redis' not found")
        with pytest.raises(ValueError, match="Service 'redis' not found"):
            _get_tool_fn("update_service")(name="redis")


class TestDeleteServiceTool:
    @patch("oduflow.docker_ops.service_ops.delete_service")
    def test_delete(self, mock_delete):
        mock_delete.return_value = {"name": "redis", "container_name": "oduflow-svc-redis"}
        result = _get_tool_fn("delete_service")(name="redis")
        assert "deleted" in result
        assert "redis" in result
        mock_delete.assert_called_once_with(TEST_SETTINGS, "redis")


class TestListServicesTool:
    @patch("oduflow.docker_ops.service_ops.list_services")
    def test_list(self, mock_list):
        mock_list.return_value = [
            {
                "name": "redis",
                "container_name": "oduflow-svc-redis",
                "image": "redis:7",
                "status": "running",
                "port": 6379,
                "url": "http://localhost:6379",
                "env_vars": {"REDIS_PASSWORD": "secret"},
            }
        ]
        result = _call_tool("list_services")
        assert "redis" in result
        assert "oduflow-svc-redis" in result
        assert "redis:7" in result
        assert "6379" in result
        assert "REDIS_PASSWORD=secret" in result

    @patch("oduflow.docker_ops.service_ops.list_services")
    def test_list_empty(self, mock_list):
        mock_list.return_value = []
        result = _call_tool("list_services")
        assert "No active services" in result


class TestGetServiceLogsTool:
    @patch("oduflow.docker_ops.service_ops.get_service_logs")
    def test_logs(self, mock_logs):
        mock_logs.return_value = "2025-01-01 log line 1\n2025-01-01 log line 2"
        result = _get_tool_fn("get_service_logs")(name="redis", n_lines=50)
        assert "log line 1" in result
        assert "service 'redis'" in result
        mock_logs.assert_called_once_with(TEST_SETTINGS, "redis", 50)

    @patch("oduflow.docker_ops.service_ops.get_service_logs")
    def test_logs_error(self, mock_logs):
        from oduflow.errors import NotFoundError
        mock_logs.side_effect = NotFoundError("Service 'redis' not found")
        with pytest.raises(ValueError, match="Service 'redis' not found"):
            _get_tool_fn("get_service_logs")(name="redis")


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
