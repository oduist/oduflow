import pytest
import docker
from unittest.mock import MagicMock, patch, call
from flow.odoo_manager import (
    create_environment,
    delete_environment,
    list_environments,
    run_environment_tests,
    get_environment_logs,
    restart_environment,
    stop_environment,
    start_environment,
    init_system,
    destroy_system,
    install_odoo_modules,
    upgrade_odoo_modules,
    _slugify_branch,
    _get_db_name,
    get_environment_status,
)


class TestSlugify:
    def test_simple(self):
        assert _slugify_branch("main") == "main"

    def test_slash(self):
        assert _slugify_branch("feature/payments") == "feature-payments"

    def test_complex(self):
        assert _slugify_branch("hotfix/CRM-123/fix") == "hotfix-crm-123-fix"

    def test_special_chars(self):
        assert _slugify_branch("feat/hello@world!") == "feat-helloworld"

    def test_truncation(self):
        long_name = "a" * 100
        assert len(_slugify_branch(long_name)) == 63


class TestGetDbName:
    def test_main(self):
        assert _get_db_name("main") == "flow_main"

    def test_feature(self):
        assert _get_db_name("feature/payments") == "flow_feature-payments"


@pytest.fixture
def mock_docker_client():
    with patch("flow.odoo_manager.get_client") as mock:
        client_instance = mock.return_value
        yield client_instance


class TestInitSystem:
    @patch("flow.odoo_manager.subprocess.run")
    @patch("flow.odoo_manager.os.path.isfile", return_value=True)
    def test_init_system_fresh(self, mock_isfile, mock_subproc, mock_docker_client):
        mock_docker_client.networks.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.volumes.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        mock_container = MagicMock()
        mock_docker_client.containers.run.return_value = mock_container

        def get_container(name):
            if name == "flow-db":
                c = MagicMock()
                c.status = "running"
                c.exec_run.return_value = (0, b"")
                return c
            raise docker.errors.NotFound("nf")

        mock_docker_client.containers.get.side_effect = None
        mock_docker_client.containers.get.side_effect = get_container

        result = init_system(dump_path="/tmp/test.dump")

        assert result["status"] == "initialized"
        assert result["template_db"] == "odoo_ref"
        mock_docker_client.networks.create.assert_called_once()
        mock_docker_client.volumes.create.assert_called_once()

    @patch("flow.odoo_manager._db_exists", return_value=True)
    @patch("flow.odoo_manager._wait_pg_ready")
    def test_init_system_already_initialized(self, mock_pg, mock_db_exists, mock_docker_client):
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.volumes.get.return_value = MagicMock()
        db_container = MagicMock()
        db_container.status = "running"
        mock_docker_client.containers.get.return_value = db_container

        result = init_system()

        assert result["status"] == "already initialized"


class TestDestroySystem:
    def test_destroy_with_active_envs(self, mock_docker_client):
        container = MagicMock()
        container.labels = {"flow.branch": "main", "flow.managed": "true"}
        container.name = "flow-main-odoo"
        mock_docker_client.containers.list.return_value = [container]

        with pytest.raises(Exception, match="Active environments exist"):
            destroy_system()

    def test_destroy_clean(self, mock_docker_client):
        mock_docker_client.containers.list.return_value = []
        db = MagicMock()
        mock_docker_client.containers.get.return_value = db
        vol = MagicMock()
        mock_docker_client.volumes.get.return_value = vol
        net = MagicMock()
        mock_docker_client.networks.get.return_value = net

        result = destroy_system()

        assert result["status"] == "destroyed"
        db.stop.assert_called_once()
        db.remove.assert_called_once()
        vol.remove.assert_called_once()
        net.remove.assert_called_once()


class TestCreateEnvironment:
    @patch("flow.odoo_manager._get_available_port", return_value=50000)
    @patch("flow.odoo_manager._ensure_system_ready")
    @patch("flow.odoo_manager._exec_sql")
    @patch("flow.odoo_manager.subprocess.run")
    @patch("flow.odoo_manager.os.makedirs")
    @patch("flow.odoo_manager.os.path.exists", return_value=False)
    def test_create(self, mock_exists, mock_makedirs, mock_run, mock_sql, mock_ready, mock_port, mock_docker_client):
        mock_odoo = MagicMock()
        mock_docker_client.containers.run.return_value = mock_odoo

        result = create_environment("feature/payments", "https://github.com/org/repo.git")

        assert result["url"] == "http://localhost:50000"
        assert result["database"] == "flow_feature-payments"
        assert result["odoo_container"] == "flow-feature-payments-odoo"
        mock_sql.assert_called_once()
        mock_docker_client.containers.run.assert_called_once()

    @patch("flow.odoo_manager._ensure_system_ready")
    def test_create_system_not_ready(self, mock_ready, mock_docker_client):
        mock_ready.side_effect = Exception("flow-db not found. Run init_system first.")

        with pytest.raises(Exception, match="init_system"):
            create_environment("main", "https://github.com/org/repo.git")


class TestDeleteEnvironment:
    @patch("flow.odoo_manager._exec_sql")
    @patch("flow.odoo_manager.shutil.rmtree")
    @patch("flow.odoo_manager.os.path.exists", return_value=True)
    def test_delete(self, mock_exists, mock_rmtree, mock_sql, mock_docker_client):
        container = MagicMock()
        mock_docker_client.containers.get.return_value = container

        delete_environment("feature/payments")

        container.stop.assert_called_once()
        container.remove.assert_called_once()
        mock_sql.assert_called_once()
        mock_rmtree.assert_called_once()


class TestRestartEnvironment:
    def test_restart(self, mock_docker_client):
        container = MagicMock()
        mock_docker_client.containers.get.return_value = container

        result = restart_environment("main")

        assert result["odoo_container"] == "flow-main-odoo"
        container.restart.assert_called_once()

    def test_restart_not_found(self, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(Exception, match="not found"):
            restart_environment("main")


class TestStopEnvironment:
    def test_stop(self, mock_docker_client):
        container = MagicMock()
        mock_docker_client.containers.get.return_value = container

        result = stop_environment("main")

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

        result = start_environment("main")

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

        result = get_environment_status("main")

        assert result["all_running"] is True
        assert result["db"]["name"] == "flow-db"


class TestInstallModules:
    def test_install(self, mock_docker_client):
        container = MagicMock()
        container.exec_run.return_value = (0, b"OK")
        mock_docker_client.containers.get.return_value = container

        result = install_odoo_modules("main", "sale", "crm")

        assert result["exit_code"] == 0
        args = container.exec_run.call_args[0][0]
        assert "-d flow_main" in args
        assert "-i sale,crm" in args


class TestUpgradeModules:
    def test_upgrade(self, mock_docker_client):
        container = MagicMock()
        container.exec_run.return_value = (0, b"OK")
        mock_docker_client.containers.get.return_value = container

        result = upgrade_odoo_modules("main", "sale")

        assert result["exit_code"] == 0
        args = container.exec_run.call_args[0][0]
        assert "-d flow_main" in args
        assert "-u sale" in args


class TestRunEnvironmentTests:
    def test_run(self, mock_docker_client):
        container = MagicMock()
        container.exec_run.return_value = (0, b"All tests passed")
        mock_docker_client.containers.get.return_value = container

        output = run_environment_tests("main", "base")

        assert "All tests passed" in output
        args = container.exec_run.call_args[0][0]
        assert "--db_host=flow-db" in args
        assert "--database=flow_main" in args


class TestGetLogs:
    def test_logs(self, mock_docker_client):
        container = MagicMock()
        container.logs.return_value = b"log line 1\nlog line 2"
        mock_docker_client.containers.get.return_value = container

        output = get_environment_logs("main", 50)

        assert "log line 1" in output
        container.logs.assert_called_with(tail=50, stdout=True, stderr=True)
