import pytest
from unittest.mock import patch

from fastmcp.exceptions import ToolError
from oduflow.settings import Settings, TeamSettings

TEST_TEAM = TeamSettings(
    team_id="1",
    data_dir="/tmp/flow-test",
    port_registry_path="/tmp/flow-test/ports.json",
    port_range_start=50000,
    port_range_end=50100,
)

TEST_SETTINGS = Settings(
    base_data_dir="/tmp/flow-test",
    db_user="odoo",
    db_password="odoo",
    teams={"1": TEST_TEAM},
)


@pytest.fixture(autouse=True)
def inject_settings():
    import oduflow.server

    oduflow.server._settings = TEST_SETTINGS
    yield
    oduflow.server._settings = None


@pytest.fixture(autouse=True)
def inject_team():
    with patch("oduflow.server._resolve_team", return_value=TEST_TEAM):
        yield


from tool_helpers import call_tool as _call_tool  # noqa: E402


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

        mock_destroy.return_value = {
            "status": "destroyed",
            "removed": "flow-db, flow-db-data, flow-net",
        }
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
        result = _call_tool(
            "create_environment",
            branch_name="main",
            template_name="none",
            repo_url="https://repo.url",
            odoo_image="odoo:17.0",
        )
        assert "Environment provisioned successfully!" in result
        assert "Database: oduflow_1_main" in result
        assert "Template: none (init from scratch)" in result


class TestDeleteEnvironmentTool:
    @patch("oduflow.docker_ops.env_ops.delete_environment")
    def test_delete(self, mock_delete):
        mock_delete.return_value = []
        result = _call_tool("delete_environment", branch_name="main")
        assert "torn down" in result
        mock_delete.assert_called_once_with(TEST_SETTINGS, TEST_TEAM, "main")

    @patch("oduflow.docker_ops.env_ops.delete_environment")
    def test_delete_with_warnings(self, mock_delete):
        mock_delete.return_value = [
            'Failed to drop database "oduflow_main": connection refused'
        ]
        result = _call_tool("delete_environment", branch_name="main")
        assert "torn down" in result
        assert "Warnings:" in result
        assert "Failed to drop database" in result


class TestListEnvironmentsTool:
    @patch("oduflow.docker_ops.env_ops.list_environments")
    def test_list(self, mock_list):
        mock_list.return_value = [
            {
                "branch": "main",
                "status": "running",
                "url": "http://localhost:50000",
                "containers": [
                    {
                        "name": "oduflow-main-odoo",
                        "status": "running",
                        "image": "odoo:15.0",
                    }
                ],
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


class TestInfoTool:
    @patch("oduflow.docker_ops.env_ops.get_environment_info")
    def test_info(self, mock_info):
        mock_info.return_value = {
            "branch": "main",
            "db_name": "oduflow_1_main",
            "workspace": "/srv/oduflow/instance_1/workspaces/main",
            "all_running": True,
            "url": "http://localhost:50000/web?debug=1",
            "repo_url": "https://github.com/example/repo.git",
            "odoo_image": "odoo:17.0",
            "template_name": "default",
            "extra_addons": {},
            "git_user": "",
            "odoo": {"status": "running", "running": True},
            "db": {"status": "running", "running": True},
        }
        result = _call_tool("get_environment_info", branch_name="main")
        assert "All containers running" in result
        assert "Database: oduflow_1_main" in result
        assert "DB (shared)" in result


class TestErrorHandling:
    @patch("oduflow.docker_ops.env_ops.restart_environment")
    def test_flow_error_raises_value_error(self, mock_restart):
        from oduflow.errors import NotFoundError

        mock_restart.side_effect = NotFoundError("container not found")
        with pytest.raises(ToolError, match="container not found"):
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
        result = _get_tool_fn("create_service")(
            name="redis", image="redis:7", port=6379
        )
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
        _get_tool_fn("create_service")(
            name="redis", image="redis:7", port=6379, env_vars=""
        )
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
        mock_update.assert_called_once_with(TEST_SETTINGS, TEST_TEAM, "redis")

    @patch("oduflow.docker_ops.service_ops.update_service")
    def test_update_not_found(self, mock_update):
        from oduflow.errors import NotFoundError

        mock_update.side_effect = NotFoundError("Service 'redis' not found")
        with pytest.raises(ToolError, match="Service 'redis' not found"):
            _get_tool_fn("update_service")(name="redis")


class TestDeleteServiceTool:
    @patch("oduflow.docker_ops.service_ops.delete_service")
    def test_delete(self, mock_delete):
        mock_delete.return_value = {
            "name": "redis",
            "container_name": "oduflow-svc-redis",
        }
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
        with pytest.raises(ToolError, match="Service 'redis' not found"):
            _get_tool_fn("get_service_logs")(name="redis")


class TestResetAdminPasswordTool:
    @patch("oduflow.docker_ops.odoo_ops.reset_admin_password")
    def test_reset_default_password(self, mock_reset):
        mock_reset.return_value = {
            "status": "ok",
            "login": "admin",
            "psql_output": "UPDATE 1",
        }
        result = _get_tool_fn("reset_admin_password")(branch_name="main")
        assert "Admin password has been reset successfully" in result
        assert "Login: admin" in result
        assert "New password: test" in result
        mock_reset.assert_called_once_with(TEST_SETTINGS, TEST_TEAM, "main", "test")

    @patch("oduflow.docker_ops.odoo_ops.reset_admin_password")
    def test_reset_custom_password(self, mock_reset):
        mock_reset.return_value = {
            "status": "ok",
            "login": "admin",
            "psql_output": "UPDATE 1",
        }
        result = _get_tool_fn("reset_admin_password")(
            branch_name="main", new_password="s3cret"
        )
        assert "New password: s3cret" in result
        mock_reset.assert_called_once_with(TEST_SETTINGS, TEST_TEAM, "main", "s3cret")

    @patch("oduflow.docker_ops.odoo_ops.reset_admin_password")
    def test_reset_not_found(self, mock_reset):
        from oduflow.errors import NotFoundError

        mock_reset.side_effect = NotFoundError("Environment 'xyz' does not exist.")
        with pytest.raises(ToolError, match="does not exist"):
            _get_tool_fn("reset_admin_password")(branch_name="xyz")


class TestBranchLock:
    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_busy_raises_tool_error(self, mock_create):
        import oduflow.server

        _locks = oduflow.server._locks
        _locks.acquire_branch("main")
        try:
            with pytest.raises(ToolError, match="Another operation"):
                _call_tool(
                    "create_environment",
                    branch_name="main",
                    repo_url="https://x.git",
                    odoo_image="odoo:17.0",
                )
        finally:
            _locks.release_branch("main")
