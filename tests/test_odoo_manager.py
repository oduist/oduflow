import pytest
import docker
from unittest.mock import MagicMock, patch

from flow.docker_ops import system_ops, env_ops, odoo_ops
from flow.errors import NotFoundError, PrerequisiteNotMetError, ConflictError
from flow.settings import Settings

TEST_SETTINGS = Settings(
    external_host="localhost",
    port_range_start=50000,
    port_range_end=50100,
    workspaces_dir="/tmp/flow-test/workspaces",
    dump_file_path="/tmp/flow-test/odoo_ref.dump",
    db_user="odoo",
    db_password="odoo",
)


@pytest.fixture
def mock_docker_client():
    with patch("flow.docker_ops.system_ops.get_client") as sys_mock, \
         patch("flow.docker_ops.env_ops.get_client") as env_mock, \
         patch("flow.docker_ops.odoo_ops.get_client") as odoo_mock:
        client_instance = MagicMock()
        sys_mock.return_value = client_instance
        env_mock.return_value = client_instance
        odoo_mock.return_value = client_instance
        yield client_instance


class TestInitSystem:
    @patch("flow.docker_ops.system_ops._copy_file_to_container")
    @patch("flow.docker_ops.system_ops.os.path.isfile", return_value=True)
    def test_init_system_fresh(self, mock_isfile, mock_copy, mock_docker_client):
        mock_docker_client.networks.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.volumes.get.side_effect = docker.errors.NotFound("nf")

        mock_container = MagicMock()
        mock_docker_client.containers.run.return_value = mock_container

        def get_container(name):
            if name == "flow-db":
                c = MagicMock()
                c.status = "running"
                c.exec_run.return_value = (0, b"")
                return c
            raise docker.errors.NotFound("nf")

        mock_docker_client.containers.get.side_effect = get_container

        result = system_ops.init_system(TEST_SETTINGS, dump_path="/tmp/test.dump")

        assert result["status"] == "initialized"
        assert result["template_db"] == "odoo_ref"
        mock_docker_client.networks.create.assert_called_once()
        mock_docker_client.volumes.create.assert_called_once()

    @patch("flow.docker_ops.system_ops._db_exists", return_value=True)
    @patch("flow.docker_ops.system_ops._wait_pg_ready")
    def test_init_system_already_initialized(self, mock_pg, mock_db_exists, mock_docker_client):
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.volumes.get.return_value = MagicMock()
        db_container = MagicMock()
        db_container.status = "running"
        mock_docker_client.containers.get.return_value = db_container

        result = system_ops.init_system(TEST_SETTINGS)

        assert result["status"] == "already initialized"


class TestDestroySystem:
    def test_destroy_with_active_envs(self, mock_docker_client):
        container = MagicMock()
        container.labels = {"flow.branch": "main", "flow.managed": "true"}
        container.name = "flow-main-odoo"
        mock_docker_client.containers.list.return_value = [container]

        with pytest.raises(ConflictError, match="Active environments exist"):
            system_ops.destroy_system(TEST_SETTINGS)

    def test_destroy_clean(self, mock_docker_client):
        mock_docker_client.containers.list.return_value = []
        db = MagicMock()
        mock_docker_client.containers.get.return_value = db
        vol = MagicMock()
        mock_docker_client.volumes.get.return_value = vol
        net = MagicMock()
        mock_docker_client.networks.get.return_value = net

        result = system_ops.destroy_system(TEST_SETTINGS)

        assert result["status"] == "destroyed"
        db.stop.assert_called_once()
        db.remove.assert_called_once()
        vol.remove.assert_called_once()
        net.remove.assert_called_once()


class TestCreateEnvironment:
    @patch("flow.docker_ops.env_ops._ensure_system_ready")
    @patch("flow.docker_ops.env_ops._exec_sql")
    @patch("flow.docker_ops.env_ops.subprocess.run")
    @patch("flow.docker_ops.env_ops.os.makedirs")
    @patch("flow.docker_ops.env_ops.os.path.exists", return_value=False)
    def test_create(self, mock_exists, mock_makedirs, mock_run, mock_sql, mock_ready, mock_docker_client):
        mock_odoo = MagicMock()
        mock_odoo.ports = {"8069/tcp": [{"HostPort": "50000"}]}
        mock_docker_client.containers.run.return_value = mock_odoo

        result = env_ops.create_environment(TEST_SETTINGS, "feature/payments", "https://github.com/org/repo.git")

        assert result["url"] == "http://localhost:50000"
        assert result["database"] == "flow_feature-payments"
        assert result["odoo_container"] == "flow-feature-payments-odoo"
        mock_sql.assert_called_once()
        mock_docker_client.containers.run.assert_called_once()

    @patch("flow.docker_ops.env_ops._ensure_system_ready")
    def test_create_system_not_ready(self, mock_ready, mock_docker_client):
        mock_ready.side_effect = PrerequisiteNotMetError("flow-db not found. Run init_system first.")

        with pytest.raises(PrerequisiteNotMetError, match="init_system"):
            env_ops.create_environment(TEST_SETTINGS, "main", "https://github.com/org/repo.git")


class TestDeleteEnvironment:
    @patch("flow.docker_ops.env_ops._exec_sql")
    @patch("flow.docker_ops.env_ops.shutil.rmtree")
    @patch("flow.docker_ops.env_ops.os.path.exists", return_value=True)
    def test_delete(self, mock_exists, mock_rmtree, mock_sql, mock_docker_client):
        container = MagicMock()
        mock_docker_client.containers.get.return_value = container

        env_ops.delete_environment(TEST_SETTINGS, "feature/payments")

        container.stop.assert_called_once()
        container.remove.assert_called_once()
        mock_sql.assert_called_once()
        mock_rmtree.assert_called_once()


class TestRestartEnvironment:
    def test_restart(self, mock_docker_client):
        container = MagicMock()
        mock_docker_client.containers.get.return_value = container

        result = env_ops.restart_environment(TEST_SETTINGS, "main")

        assert result["odoo_container"] == "flow-main-odoo"
        container.restart.assert_called_once()

    def test_restart_not_found(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(NotFoundError, match="not found"):
            env_ops.restart_environment(TEST_SETTINGS, "main")


class TestStopEnvironment:
    def test_stop(self, mock_docker_client):
        container = MagicMock()
        mock_docker_client.containers.get.return_value = container

        result = env_ops.stop_environment(TEST_SETTINGS, "main")

        assert "flow-main-odoo" in result["stopped"]
        container.stop.assert_called_once()


class TestStartEnvironment:
    def test_start(self, mock_docker_client):
        db = MagicMock()
        db.status = "running"
        odoo = MagicMock()

        def get_container(name):
            if name == "flow-db":
                return db
            return odoo

        mock_docker_client.containers.get.side_effect = get_container

        result = env_ops.start_environment(TEST_SETTINGS, "main")

        assert "flow-main-odoo" in result["started"]
        odoo.start.assert_called_once()


class TestGetEnvironmentStatus:
    def test_all_running(self, mock_docker_client):
        odoo = MagicMock()
        odoo.status = "running"
        db = MagicMock()
        db.status = "running"

        def get_container(name):
            if name == "flow-db":
                return db
            return odoo

        mock_docker_client.containers.get.side_effect = get_container

        result = env_ops.get_environment_status(TEST_SETTINGS, "main")

        assert result["all_running"] is True
        assert result["db"]["name"] == "flow-db"


class TestInstallModules:
    def test_install(self, mock_docker_client):
        container = MagicMock()
        container.exec_run.return_value = (0, b"OK")
        mock_docker_client.containers.get.return_value = container

        result = odoo_ops.install_odoo_modules(TEST_SETTINGS, "main", "sale", "crm")

        assert result["exit_code"] == 0
        args = container.exec_run.call_args[0][0]
        assert "-d flow_main" in args
        assert "-i sale,crm" in args


class TestUpgradeModules:
    def test_upgrade(self, mock_docker_client):
        container = MagicMock()
        container.exec_run.return_value = (0, b"OK")
        mock_docker_client.containers.get.return_value = container

        result = odoo_ops.upgrade_odoo_modules(TEST_SETTINGS, "main", "sale")

        assert result["exit_code"] == 0
        args = container.exec_run.call_args[0][0]
        assert "-d flow_main" in args
        assert "-u sale" in args


class TestRunEnvironmentTests:
    def test_run(self, mock_docker_client):
        container = MagicMock()
        container.exec_run.return_value = (0, b"All tests passed")
        mock_docker_client.containers.get.return_value = container

        output = odoo_ops.run_environment_tests(TEST_SETTINGS, "main", "base")

        assert "All tests passed" in output
        args = container.exec_run.call_args[0][0]
        assert "--db_host=flow-db" in args
        assert "--database=flow_main" in args


class TestGetLogs:
    def test_logs(self, mock_docker_client):
        container = MagicMock()
        container.logs.return_value = b"log line 1\nlog line 2"
        mock_docker_client.containers.get.return_value = container

        output = odoo_ops.get_environment_logs(TEST_SETTINGS, "main", 50)

        assert "log line 1" in output
        container.logs.assert_called_with(tail=50, stdout=True, stderr=True)
