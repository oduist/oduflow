import pytest
from unittest.mock import patch, MagicMock
from flow.server import mcp

@patch("flow.odoo_manager.provision_env")
def test_provision_env_tool(mock_provision):
    mock_provision.return_value = {
        "url": "http://localhost:8069",
        "odoo_container": "odoo-test",
        "db_container": "db-test",
        "workspace": "/tmp/workspace",
        "network": "net-test"
    }
    
    # Call the tool directly through the FastMCP instance if possible, 
    # or just call the function decorated by it.
    from flow.server import provision_env as provision_tool
    
    result = provision_tool.fn("feat/branch", "https://repo.url")
    
    assert "Environment provisioned successfully!" in result
    assert "URL: http://localhost:8069" in result
    mock_provision.assert_called_once_with("feat/branch", "https://repo.url", "17.0")

@patch("flow.odoo_manager.teardown_env")
def test_teardown_env_tool(mock_teardown):
    from flow.server import teardown_env as teardown_tool
    
    result = teardown_tool.fn("feat/branch")
    
    assert "torn down" in result
    mock_teardown.assert_called_once_with("feat/branch")

@patch("flow.odoo_manager.list_envs")
def test_list_envs_tool(mock_list):
    mock_list.return_value = [
        {
            "branch": "feat/branch",
            "status": "running",
            "containers": [{"name": "odoo-test", "status": "running", "image": "odoo:17.0"}]
        }
    ]
    from flow.server import list_envs as list_tool
    
    result = list_tool.fn()
    
    assert "feat/branch" in result
    assert "odoo-test" in result

@patch("flow.odoo_manager.execute_test")
def test_execute_test_tool(mock_test):
    mock_test.return_value = "Success"
    from flow.server import execute_test as test_tool
    
    result = test_tool.fn("feat/branch", "base")
    
    assert "Success" in result
    mock_test.assert_called_once_with("feat/branch", "base")
