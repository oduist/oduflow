import pytest
import docker
from unittest.mock import MagicMock, patch

from oduflow.docker_ops import system_ops, env_ops, odoo_ops
from oduflow.errors import ConflictError, NotFoundError, PrerequisiteNotMetError
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


@pytest.fixture
def mock_docker_client():
    with patch("oduflow.docker_ops.system_ops.get_client") as sys_mock, \
         patch("oduflow.docker_ops.env_ops.get_client") as env_mock, \
         patch("oduflow.docker_ops.odoo_ops.get_client") as odoo_mock:
        client_instance = MagicMock()
        sys_mock.return_value = client_instance
        env_mock.return_value = client_instance
        odoo_mock.return_value = client_instance
        yield client_instance


class TestInitSystem:
    @patch("oduflow.docker_ops.system_ops._copy_file_to_container")
    @patch("oduflow.docker_ops.system_ops.os.path.isfile", return_value=True)
    def test_init_system_fresh(self, mock_isfile, mock_copy, mock_docker_client):
        mock_docker_client.networks.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.volumes.get.side_effect = docker.errors.NotFound("nf")

        mock_container = MagicMock()
        mock_docker_client.containers.run.return_value = mock_container

        def get_container(name):
            if name == "oduflow-db":
                c = MagicMock()
                c.status = "running"
                c.exec_run.return_value = (0, b"")
                return c
            raise docker.errors.NotFound("nf")

        mock_docker_client.containers.get.side_effect = get_container

        result = system_ops.init_system(TEST_SETTINGS)

        assert result["status"] == "initialized"
        assert result["template_db"] == "odoo_ref"
        mock_docker_client.networks.create.assert_called_once()
        mock_docker_client.volumes.create.assert_called_once()

    @patch("oduflow.docker_ops.system_ops._db_exists", return_value=True)
    @patch("oduflow.docker_ops.system_ops._wait_pg_ready")
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
        container.labels = {"oduflow.branch": "main", "oduflow.managed": "true"}
        container.name = "oduflow-main-odoo"
        mock_docker_client.containers.list.return_value = [container]

        with pytest.raises(ConflictError, match="Active environments exist"):
            system_ops.destroy_system(TEST_SETTINGS)

    def test_destroy_clean(self, mock_docker_client):
        mock_docker_client.containers.list.return_value = []
        db = MagicMock()

        def _get_container(name):
            if name == TEST_SETTINGS.shared_db_container:
                return db
            raise docker.errors.NotFound("not found")

        mock_docker_client.containers.get.side_effect = _get_container
        vol = MagicMock()

        def _get_volume(name):
            if name == TEST_SETTINGS.shared_db_volume:
                return vol
            raise docker.errors.NotFound("not found")

        mock_docker_client.volumes.get.side_effect = _get_volume
        net = MagicMock()
        mock_docker_client.networks.get.return_value = net

        result = system_ops.destroy_system(TEST_SETTINGS)

        assert result["status"] == "destroyed"
        db.stop.assert_called_once()
        db.remove.assert_called_once()
        vol.remove.assert_called_once()
        net.remove.assert_called_once()


class TestCreateEnvironment:
    @patch("oduflow.docker_ops.env_ops._ensure_system_ready")
    @patch("oduflow.docker_ops.env_ops.get_odoo_uid_gid", return_value="100:101")
    @patch("oduflow.docker_ops.env_ops._exec_sql")
    @patch("oduflow.docker_ops.env_ops._db_exists", return_value=False)
    @patch("oduflow.docker_ops.env_ops._mount_filestore")
    @patch("oduflow.docker_ops.env_ops._get_used_ports", return_value=set())
    @patch("oduflow.docker_ops.env_ops.allocate_port", return_value=50000)
    @patch("oduflow.docker_ops.env_ops.subprocess.run")
    @patch("oduflow.docker_ops.env_ops.os.chmod")
    @patch("oduflow.docker_ops.env_ops.os.makedirs")
    @patch("oduflow.docker_ops.env_ops.os.path.exists", return_value=False)
    def test_create(self, mock_exists, mock_makedirs, mock_chmod, mock_run, mock_alloc, mock_used, mock_mount, mock_db_exists, mock_sql, mock_uid_gid, mock_ready, mock_docker_client):
        mock_odoo = MagicMock()
        mock_docker_client.containers.run.return_value = mock_odoo
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        result = env_ops.create_environment(TEST_SETTINGS, "feature/payments", "https://github.com/org/repo.git")

        assert result["url"] == "http://localhost:50000"
        assert result["database"] == "oduflow_feature-payments"
        assert result["odoo_container"] == "oduflow-feature-payments-odoo"
        mock_sql.assert_called_once()
        mock_docker_client.containers.run.assert_called_once()
        mock_alloc.assert_called_once()

    @patch("oduflow.docker_ops.env_ops._ensure_system_ready")
    def test_create_already_exists(self, mock_ready, mock_docker_client):
        existing = MagicMock()
        existing.status = "running"
        existing.ports = {"8069/tcp": [{"HostPort": "50000"}]}
        mock_docker_client.containers.get.return_value = existing

        with pytest.raises(ConflictError, match="already exists"):
            env_ops.create_environment(TEST_SETTINGS, "main", "https://github.com/org/repo.git")

    @patch("oduflow.docker_ops.env_ops._ensure_system_ready")
    def test_create_system_not_ready(self, mock_ready, mock_docker_client):
        mock_ready.side_effect = PrerequisiteNotMetError("flow-db not found. Run init_system first.")

        with pytest.raises(PrerequisiteNotMetError, match="init_system"):
            env_ops.create_environment(TEST_SETTINGS, "main", "https://github.com/org/repo.git")


class TestDeleteEnvironment:
    @patch("oduflow.docker_ops.env_ops.release_port")
    @patch("oduflow.docker_ops.env_ops._exec_sql")
    @patch("oduflow.docker_ops.env_ops.shutil.rmtree")
    @patch("oduflow.docker_ops.env_ops.os.path.exists", return_value=True)
    def test_delete(self, mock_exists, mock_rmtree, mock_sql, mock_release, mock_docker_client):
        container = MagicMock()
        mock_docker_client.containers.get.return_value = container

        env_ops.delete_environment(TEST_SETTINGS, "feature/payments")

        container.stop.assert_called_once()
        container.remove.assert_called_once()
        mock_sql.assert_called_once()
        mock_rmtree.assert_called_once()
        mock_release.assert_called_once_with(TEST_SETTINGS.port_registry_path, "feature/payments")


class TestRestartEnvironment:
    def test_restart(self, mock_docker_client):
        container = MagicMock()
        mock_docker_client.containers.get.return_value = container

        result = env_ops.restart_environment(TEST_SETTINGS, "main")

        assert result["odoo_container"] == "oduflow-main-odoo"
        container.restart.assert_called_once()

    def test_restart_not_found(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(NotFoundError, match="does not exist"):
            env_ops.restart_environment(TEST_SETTINGS, "main")


class TestStopEnvironment:
    def test_stop(self, mock_docker_client):
        container = MagicMock()
        mock_docker_client.containers.get.return_value = container

        result = env_ops.stop_environment(TEST_SETTINGS, "main")

        assert "oduflow-main-odoo" in result["stopped"]
        container.stop.assert_called_once()


class TestStartEnvironment:
    def test_start(self, mock_docker_client):
        db = MagicMock()
        db.status = "running"
        odoo = MagicMock()

        def get_container(name):
            if name == "oduflow-db":
                return db
            return odoo

        mock_docker_client.containers.get.side_effect = get_container

        result = env_ops.start_environment(TEST_SETTINGS, "main")

        assert "oduflow-main-odoo" in result["started"]
        odoo.start.assert_called_once()


class TestGetEnvironmentStatus:
    def test_all_running(self, mock_docker_client):
        odoo = MagicMock()
        odoo.status = "running"
        db = MagicMock()
        db.status = "running"

        def get_container(name):
            if name == "oduflow-db":
                return db
            return odoo

        mock_docker_client.containers.get.side_effect = get_container

        result = env_ops.get_environment_status(TEST_SETTINGS, "main")

        assert result["all_running"] is True
        assert result["db"]["name"] == "oduflow-db"


class TestInstallModules:
    def test_install(self, mock_docker_client):
        container = MagicMock()
        container.exec_run.return_value = (0, b"OK")
        mock_docker_client.containers.get.return_value = container

        result = odoo_ops.install_odoo_modules(TEST_SETTINGS, "main", "sale", "crm")

        assert result["exit_code"] == 0
        args = container.exec_run.call_args[0][0]
        assert "-d oduflow_main" in args
        assert "-i sale,crm" in args


class TestUpgradeModules:
    def test_upgrade(self, mock_docker_client):
        container = MagicMock()
        container.exec_run.return_value = (0, b"OK")
        mock_docker_client.containers.get.return_value = container

        result = odoo_ops.upgrade_odoo_modules(TEST_SETTINGS, "main", "sale")

        assert result["exit_code"] == 0
        args = container.exec_run.call_args[0][0]
        assert "-d oduflow_main" in args
        assert "-u sale" in args


class TestRunEnvironmentTests:
    def test_run(self, mock_docker_client):
        container = MagicMock()
        container.exec_run.return_value = (0, b"All tests passed")
        mock_docker_client.containers.get.return_value = container

        output = odoo_ops.run_environment_tests(TEST_SETTINGS, "main", "base")

        assert "All tests passed" in output
        args = container.exec_run.call_args[0][0]
        assert "--db_host=oduflow-db" in args
        assert "--database=oduflow_main" in args


class TestGetLogs:
    def test_logs(self, mock_docker_client):
        container = MagicMock()
        container.logs.return_value = b"log line 1\nlog line 2"
        mock_docker_client.containers.get.return_value = container

        output = odoo_ops.get_environment_logs(TEST_SETTINGS, "main", 50)

        assert "log line 1" in output
        container.logs.assert_called_with(tail=50, stdout=True, stderr=True)
