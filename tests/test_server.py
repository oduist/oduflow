import pytest
from unittest.mock import patch

@patch("flow.server.get_http_headers")
@patch("flow.odoo_manager.provision_env")
def test_provision_env_tool(mock_provision, mock_headers):
    user_id = "user123"
    mock_headers.return_value = {"API_KEY": user_id}
    mock_provision.return_value = {
        "url": "http://localhost:8069",
        "odoo_container": "odoo-test",
        "db_container": "db-test",
        "workspace": "/tmp/workspace",
        "network": "net-test"
    }
    
    from flow.server import provision_env as provision_tool
    result = provision_tool.fn("feat/branch", "https://repo.url")
    
    assert "Environment provisioned successfully!" in result
    assert "URL: http://localhost:8069" in result
    mock_provision.assert_called_once_with(user_id, "feat/branch", "https://repo.url", "17.0")

@patch("flow.server.get_http_headers")
@patch("flow.odoo_manager.teardown_env")
def test_teardown_env_tool(mock_teardown, mock_headers):
    user_id = "user123"
    mock_headers.return_value = {"API_KEY": user_id}
    from flow.server import teardown_env as teardown_tool
    
    result = teardown_tool.fn("feat/branch")
    
    assert "torn down" in result
    mock_teardown.assert_called_once_with(user_id, "feat/branch")

@patch("flow.server.get_http_headers")
@patch("flow.odoo_manager.list_envs")
def test_list_envs_tool(mock_list, mock_headers):
    user_id = "user123"
    mock_headers.return_value = {"API_KEY": user_id}
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
    mock_list.assert_called_once_with(user_id)

@patch("flow.server.get_http_headers")
@patch("flow.odoo_manager.execute_test")
def test_execute_test_tool(mock_test, mock_headers):
    user_id = "user123"
    mock_headers.return_value = {"API_KEY": user_id}
    mock_test.return_value = "Success"
    from flow.server import execute_test as test_tool
    
    result = test_tool.fn("feat/branch", "base")
    
    assert "Success" in result
    mock_test.assert_called_once_with(user_id, "feat/branch", "base")

@patch("flow.server.get_http_headers")
@patch("flow.server.config")
def test_api_key_verification(mock_config, mock_headers):
    # Case 1: FLOW_API_KEYS is set, correct key provided
    mock_config.FLOW_API_KEYS = "key1,key2"
    mock_headers.return_value = {"API_KEY": "key1"}
    
    from flow.server import _get_user_id
    assert _get_user_id() == "key1"
    
    # Case 2: FLOW_API_KEYS is set, incorrect key provided
    mock_headers.return_value = {"API_KEY": "wrong-key"}
    with pytest.raises(Exception, match="Invalid API_KEY"):
        _get_user_id()
    
    # Case 3: FLOW_API_KEYS is NOT set, any key should work
    mock_config.FLOW_API_KEYS = ""
    mock_headers.return_value = {"API_KEY": "any-key"}
    assert _get_user_id() == "any-key"

@patch("flow.server.get_http_headers")
def test_missing_api_key_header(mock_headers):
    mock_headers.return_value = {}
    from flow.server import _get_user_id
    with pytest.raises(Exception, match="Missing API_KEY header"):
        _get_user_id()
