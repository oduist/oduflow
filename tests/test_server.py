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


class TestMCPBootstrapInstructions:
    def test_instructions_point_agents_to_dynamic_guides(self):
        import oduflow.server

        instructions = oduflow.server.mcp.instructions

        assert instructions
        assert "get_agent_instructions" in instructions
        assert "get_odoo_development_guide" in instructions
        assert 'odoo:18.0 means version="18"' in instructions


class TestCLIInitDestroy:
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
            branch="main",
            template_name="none",
            repo_url="https://repo.url",
            odoo_image="odoo:17.0",
        )
        assert "Environment provisioned successfully!" in result
        assert "Database: oduflow_1_main" in result
        assert "Template: none (init from scratch)" in result

    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_create_with_env_vars(self, mock_create):
        mock_create.return_value = {
            "url": "http://localhost:50000",
            "odoo_container": "oduflow-main-odoo",
            "database": "oduflow_1_main",
            "workspace": "/tmp/ws",
        }
        result = _call_tool(
            "create_environment",
            branch="main",
            template_name="none",
            repo_url="https://repo.url",
            odoo_image="odoo:17.0",
            env_vars="FOO=bar,BAZ=qux",
        )
        assert mock_create.call_args.kwargs["env_vars"] == {
            "FOO": "bar",
            "BAZ": "qux",
        }
        assert "Env vars: FOO=bar, BAZ=qux" in result

    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_create_without_env_vars(self, mock_create):
        mock_create.return_value = {
            "url": "http://localhost:50000",
            "odoo_container": "oduflow-main-odoo",
            "database": "oduflow_1_main",
            "workspace": "/tmp/ws",
        }
        _call_tool(
            "create_environment",
            branch="main",
            template_name="none",
            repo_url="https://repo.url",
            odoo_image="odoo:17.0",
        )
        assert mock_create.call_args.kwargs["env_vars"] is None


class TestUpdateEnvironmentTool:
    @patch("oduflow.docker_ops.env_ops.update_environment")
    def test_update_rebuild_only(self, mock_update):
        mock_update.return_value = {
            "url": "http://localhost:50000",
            "odoo_container": "oduflow-main-odoo",
            "database": "oduflow_1_main",
            "workspace": "/tmp/ws",
            "image": "odoo:17.0",
            "image_updated": False,
            "env_vars": {},
        }
        result = _call_tool("update_environment", env_name="main")
        assert mock_update.call_args.kwargs == {
            "env_override": None,
            "image_override": None,
        }
        assert "Environment updated successfully!" in result
        assert "Image: odoo:17.0" in result
        assert "(updated)" not in result

    @patch("oduflow.docker_ops.env_ops.update_environment")
    def test_update_image_and_env(self, mock_update):
        mock_update.return_value = {
            "url": "http://localhost:50000",
            "odoo_container": "oduflow-main-odoo",
            "database": "oduflow_1_main",
            "workspace": "/tmp/ws",
            "image": "odoo:17.0",
            "image_updated": True,
            "env_vars": {"FOO": "new"},
        }
        result = _call_tool(
            "update_environment",
            env_name="main",
            env_vars="FOO=new",
            odoo_image="odoo:17.0",
        )
        assert mock_update.call_args.kwargs == {
            "env_override": {"FOO": "new"},
            "image_override": "odoo:17.0",
        }
        assert "Image: odoo:17.0 (updated)" in result
        assert "Env vars: FOO=new" in result


class TestDeleteEnvironmentTool:
    @patch("oduflow.docker_ops.env_ops.delete_environment")
    def test_delete(self, mock_delete):
        mock_delete.return_value = []
        result = _call_tool("delete_environment", env_name="main")
        assert "torn down" in result
        mock_delete.assert_called_once_with(TEST_SETTINGS, TEST_TEAM, "main")

    @patch("oduflow.docker_ops.env_ops.delete_environment")
    def test_delete_with_warnings(self, mock_delete):
        mock_delete.return_value = [
            'Failed to drop database "oduflow_main": connection refused'
        ]
        result = _call_tool("delete_environment", env_name="main")
        assert "torn down" in result
        assert "Warnings:" in result
        assert "Failed to drop database" in result

    @patch("oduflow.docker_ops.env_ops.delete_environment")
    def test_delete_missing_raises(self, mock_delete):
        from oduflow.errors import NotFoundError

        mock_delete.side_effect = NotFoundError(
            "Environment 'firewall' does not exist."
        )
        with pytest.raises(ToolError, match="does not exist"):
            _call_tool("delete_environment", env_name="firewall")


class TestListEnvironmentsTool:
    @patch("oduflow.docker_ops.env_ops.list_environments")
    def test_list(self, mock_list):
        mock_list.return_value = [
            {
                "env_name": "main",
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

    @patch("oduflow.docker_ops.env_ops.list_environments")
    def test_list_shows_live_mount_path(self, mock_list):
        mock_list.return_value = [
            {
                "env_name": "local",
                "status": "running",
                "url": "http://localhost:50000",
                "local_path": "/Users/dev/addons",
                "containers": [],
            }
        ]

        result = _call_tool("list_environments")

        assert "Live-mount: /Users/dev/addons" in result


class TestAgentInstructionsTool:
    @patch("oduflow.docker_ops.env_ops.list_environments")
    def test_instructions_preface_local_mode(self, mock_list):
        mock_list.return_value = [
            {
                "env_name": "test-local",
                "local_path": "/Users/dev/addons",
            }
        ]

        result = _call_tool("get_agent_instructions")

        assert result.startswith("## Current Code Delivery Mode")
        assert "Live-mount/local_path mode is active" in result
        assert "upgrade=\"module\"" in result
        assert "Git commits are optional" in result

    @patch("oduflow.docker_ops.env_ops.list_environments")
    def test_instructions_preface_repo_url_mode(self, mock_list):
        mock_list.return_value = []

        result = _call_tool("get_agent_instructions")

        assert result.startswith("## Current Code Delivery Mode")
        assert "No live-mount/local_path environment was detected" in result
        assert "commit, push, then call `pull_and_apply`" in result


class TestInfoTool:
    @patch("oduflow.docker_ops.env_ops.get_environment_info")
    def test_info(self, mock_info):
        mock_info.return_value = {
            "env_name": "main",
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
        result = _call_tool("get_environment_info", env_name="main")
        assert "All containers running" in result
        assert "Database: oduflow_1_main" in result
        assert "DB (shared)" in result


class TestErrorHandling:
    @patch("oduflow.docker_ops.env_ops.restart_environment")
    def test_flow_error_raises_value_error(self, mock_restart):
        from oduflow.errors import NotFoundError

        mock_restart.side_effect = NotFoundError("container not found")
        with pytest.raises(ToolError, match="container not found"):
            _call_tool("restart_environment", env_name="main")


def _get_tool_fn(tool_name: str):
    """Get a sync-callable wrapper for a registered MCP tool."""
    import asyncio
    import inspect
    from oduflow.server import mcp

    fn = mcp._tool_manager._tools[tool_name].fn

    def sync_wrapper(*args, **kwargs):
        result = fn(*args, **kwargs)
        if inspect.isawaitable(result):
            return asyncio.run(result)
        return result

    return sync_wrapper


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
        mock_update.assert_called_once_with(
            TEST_SETTINGS,
            TEST_TEAM,
            "redis",
            env_override=None,
            image_override=None,
            port_override=None,
            hostname_override=None,
            host_mode_override=None,
            volume_override=None,
            cap_add_override=None,
            privileged_override=None,
        )

    @patch("oduflow.docker_ops.service_ops.update_service")
    def test_update_net_admin_and_privileged_mapping(self, mock_update):
        mock_update.return_value = {
            "name": "vpn",
            "container_name": "oduflow-svc-vpn",
            "url": "http://localhost:1194",
            "image": "vpn:latest",
        }
        _get_tool_fn("update_service")(name="vpn", net_admin=True, privileged=False)
        _, kwargs = mock_update.call_args
        assert kwargs["cap_add_override"] == ["NET_ADMIN"]
        assert kwargs["privileged_override"] is False

    @patch("oduflow.docker_ops.service_ops.update_service")
    def test_update_net_admin_false_clears_cap(self, mock_update):
        mock_update.return_value = {
            "name": "vpn",
            "container_name": "oduflow-svc-vpn",
            "url": "http://localhost:1194",
            "image": "vpn:latest",
        }
        _get_tool_fn("update_service")(name="vpn", net_admin=False)
        _, kwargs = mock_update.call_args
        assert kwargs["cap_add_override"] == []
        assert kwargs["privileged_override"] is None

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
    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_ops.reset_admin_password")
    def test_reset_default_password(self, mock_reset, mock_ensure):
        mock_reset.return_value = {
            "status": "ok",
            "login": "admin",
            "psql_output": "UPDATE 1",
        }
        result = _get_tool_fn("reset_admin_password")(env_name="main")
        assert "Admin password has been reset successfully" in result
        assert "Login: admin" in result
        assert "New password: test" in result
        mock_reset.assert_called_once_with(TEST_SETTINGS, TEST_TEAM, "main", "test")

    @patch("oduflow.docker_ops.env_ops.ensure_running", return_value=False)
    @patch("oduflow.docker_ops.odoo_ops.reset_admin_password")
    def test_reset_custom_password(self, mock_reset, mock_ensure):
        mock_reset.return_value = {
            "status": "ok",
            "login": "admin",
            "psql_output": "UPDATE 1",
        }
        result = _get_tool_fn("reset_admin_password")(
            env_name="main", new_password="s3cret"
        )
        assert "New password: s3cret" in result
        mock_reset.assert_called_once_with(TEST_SETTINGS, TEST_TEAM, "main", "s3cret")

    @patch("oduflow.docker_ops.odoo_ops.reset_admin_password")
    def test_reset_not_found(self, mock_reset):
        from oduflow.errors import NotFoundError

        mock_reset.side_effect = NotFoundError("Environment 'xyz' does not exist.")
        with pytest.raises(ToolError, match="does not exist"):
            _get_tool_fn("reset_admin_password")(env_name="xyz")


class TestEnvLock:
    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_busy_raises_tool_error(self, mock_create):
        import oduflow.server

        _locks = oduflow.server._locks
        _locks.acquire_env("main")
        try:
            with pytest.raises(ToolError, match="Another operation"):
                _call_tool(
                    "create_environment",
                    branch="main",
                    repo_url="https://x.git",
                    odoo_image="odoo:17.0",
                )
        finally:
            _locks.release_env("main")


class TestListServicePresetsTool:
    @patch("oduflow.docker_ops.service_presets.list_presets")
    def test_list_shows_all_fields(self, mock_list):
        mock_list.return_value = [
            {
                "name": "wg",
                "image": "linuxserver/wireguard:latest",
                "port": 51820,
                "hostname": "wg",
                "env_vars": {"PUID": "1000"},
                "host_mode": True,
                "privileged": True,
                "cap_add": ["NET_ADMIN", "SYS_MODULE"],
                "volumes": [
                    {"volume": "wg-data", "mount_path": "/config", "mode": "rw"},
                ],
            }
        ]
        result = _call_tool("list_service_presets")
        assert "wg" in result
        assert "image=linuxserver/wireguard:latest" in result
        assert "port=51820" in result
        assert "hostname=wg" in result
        assert "env=[PUID=1000]" in result
        assert "host_mode=true" in result
        assert "privileged=true" in result
        assert "cap_add=[NET_ADMIN,SYS_MODULE]" in result
        assert "volumes=[wg-data:/config:rw]" in result

    @patch("oduflow.docker_ops.service_presets.list_presets")
    def test_list_omits_absent_fields(self, mock_list):
        mock_list.return_value = [
            {
                "name": "redis",
                "image": "redis:7",
                "port": 6379,
                "hostname": "",
                "env_vars": {},
            }
        ]
        result = _call_tool("list_service_presets")
        assert "redis" in result
        assert "host_mode" not in result
        assert "privileged" not in result
        assert "cap_add" not in result
        assert "volumes" not in result


class TestRestoreServiceTool:
    @patch("oduflow.docker_ops.service_ops.create_service")
    @patch("oduflow.docker_ops.service_presets.get_preset")
    def test_restore_propagates_capabilities(self, mock_get, mock_create):
        mock_get.return_value = {
            "name": "wg",
            "image": "linuxserver/wireguard:latest",
            "port": 51820,
            "hostname": "wg",
            "env_vars": {"PUID": "1000"},
            "host_mode": True,
            "privileged": False,
            "cap_add": ["NET_ADMIN"],
            "volumes": [
                {"volume": "wg-data", "mount_path": "/config", "mode": "rw"},
            ],
        }
        mock_create.return_value = {
            "name": "wg",
            "container_name": "oduflow-svc-wg",
            "image": "linuxserver/wireguard:latest",
            "url": "http://localhost:51820",
        }
        result = _get_tool_fn("restore_service")(name="wg")

        kwargs = mock_create.call_args.kwargs
        assert kwargs["cap_add"] == ["NET_ADMIN"]
        assert kwargs["privileged"] is False
        assert kwargs["host_mode"] is True
        assert kwargs["volumes"] == [
            {"volume": "wg-data", "mount_path": "/config", "mode": "rw"},
        ]
        assert kwargs["env_vars"] == {"PUID": "1000"}
        assert "Service restored from preset!" in result
        assert "Capabilities: NET_ADMIN" in result
        assert "Volumes: wg-data:/config:rw" in result

    @patch("oduflow.docker_ops.service_ops.create_service")
    @patch("oduflow.docker_ops.service_presets.get_preset")
    def test_restore_propagates_privileged(self, mock_get, mock_create):
        mock_get.return_value = {
            "name": "dind",
            "image": "docker:dind",
            "port": 2375,
            "hostname": "",
            "env_vars": {},
            "privileged": True,
        }
        mock_create.return_value = {
            "name": "dind",
            "container_name": "oduflow-svc-dind",
            "image": "docker:dind",
            "url": "http://localhost:2375",
        }
        result = _get_tool_fn("restore_service")(name="dind")

        kwargs = mock_create.call_args.kwargs
        assert kwargs["privileged"] is True
        assert kwargs["cap_add"] is None
        assert "Privileged: true" in result

    @patch("oduflow.docker_ops.service_ops.create_service")
    @patch("oduflow.docker_ops.service_presets.get_preset")
    def test_restore_minimal_preset(self, mock_get, mock_create):
        mock_get.return_value = {
            "name": "redis",
            "image": "redis:7",
            "port": 6379,
            "hostname": "",
            "env_vars": {},
        }
        mock_create.return_value = {
            "name": "redis",
            "container_name": "oduflow-svc-redis",
            "image": "redis:7",
            "url": "http://localhost:6379",
        }
        _get_tool_fn("restore_service")(name="redis")

        kwargs = mock_create.call_args.kwargs
        assert kwargs["cap_add"] is None
        assert kwargs["privileged"] is False
        assert kwargs["host_mode"] is False
        assert kwargs["volumes"] is None
