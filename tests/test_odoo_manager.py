import pytest
import docker
import os
from unittest.mock import MagicMock, patch
from flow.odoo_manager import provision_env, teardown_env, list_envs, execute_test

@pytest.fixture
def mock_docker_client():
    with patch("flow.odoo_manager.get_client") as mock:
        client_instance = mock.return_value
        yield client_instance

@patch("flow.odoo_manager._get_available_port")
@patch("flow.odoo_manager.subprocess.run")
@patch("flow.odoo_manager.os.makedirs")
@patch("flow.odoo_manager.os.path.exists")
@patch("flow.odoo_manager.shutil.rmtree")
def test_provision_env(mock_rmtree, mock_exists, mock_makedirs, mock_run, mock_get_port, mock_docker_client):
    # Setup mocks
    mock_exists.return_value = True
    mock_get_port.return_value = 50000
    mock_docker_client.networks.get.side_effect = docker.errors.NotFound("Network not found")
    mock_network = MagicMock()
    mock_docker_client.networks.create.return_value = mock_network
    
    mock_odoo_container = MagicMock()
    mock_docker_client.containers.run.return_value = mock_odoo_container
    
    # Execute
    result = provision_env("user123", "feat/test-branch", repo_url="https://github.com/org/repo.git", version="17.0")
    
    # Assertions
    assert result["url"] == "http://localhost:50000"
    assert "workspace" in result
    assert mock_run.called
    assert mock_rmtree.called
    assert mock_docker_client.networks.create.called
    assert mock_docker_client.containers.run.call_count == 2
    
    # Check labels in Odoo run call
    args, kwargs = mock_docker_client.containers.run.call_args_list[1]
    assert kwargs['labels']['flow.user'] == "user123"
    assert kwargs['ports'] == {'8072/tcp': 50000}

@patch("flow.odoo_manager.shutil.rmtree")
@patch("flow.odoo_manager.os.path.exists")
def test_teardown_env(mock_exists, mock_rmtree, mock_docker_client):
    # Setup mocks
    mock_exists.return_value = True
    mock_container = MagicMock()
    mock_docker_client.containers.list.return_value = [mock_container]
    mock_network = MagicMock()
    mock_docker_client.networks.list.return_value = [mock_network]
    
    # Execute
    teardown_env("user123", "feat/test-branch")
    
    # Assertions
    mock_container.stop.assert_called_once()
    mock_container.remove.assert_called_once_with(v=True)
    mock_network.remove.assert_called_once()
    assert mock_rmtree.called

def test_list_envs(mock_docker_client):
    # Setup mocks
    mock_container = MagicMock()
    mock_container.labels = {"flow.branch": "feat/test", "flow.managed": "true", "flow.user": "user123"}
    mock_container.name = "flow-user123-feat-test-odoo"
    mock_container.status = "running"
    mock_container.image.tags = ["odoo:17.0"]
    mock_container.attrs = {
        'NetworkSettings': {
            'Ports': {
                '8072/tcp': [{'HostPort': '50000'}]
            }
        }
    }
    mock_docker_client.containers.list.return_value = [mock_container]
    
    # Execute
    envs = list_envs("user123")
    
    # Assertions
    assert len(envs) == 1
    assert envs[0]["branch"] == "feat/test"
    assert envs[0]["status"] == "running"
    assert envs[0]["url"] == "http://localhost:50000"

def test_execute_test(mock_docker_client):
    # Setup mocks
    mock_container = MagicMock()
    mock_container.exec_run.return_value = (0, b"All tests passed")
    mock_docker_client.containers.get.return_value = mock_container
    
    # Execute
    output = execute_test("user123", "feat/test", "base")
    
    # Assertions
    assert output == "All tests passed"
    mock_container.exec_run.assert_called()
