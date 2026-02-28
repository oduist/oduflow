import pytest
import docker
from unittest.mock import MagicMock, patch

from oduflow.docker_ops import system_ops, env_ops, odoo_ops
from oduflow.errors import ConflictError, NotFoundError, PrerequisiteNotMetError
from oduflow.settings import Settings, TeamSettings

TEST_TEAM = TeamSettings(
    team_id="1",
    data_dir="/tmp/flow-test",
    port_registry_path="/tmp/flow-test/ports.json",
    port_range_start=50000,
    port_range_end=50100,
)

TEST_SETTINGS = Settings(
    external_host="localhost",
    base_data_dir="/tmp/flow-test",
    db_user="odoo",
    db_password="odoo",
    etc_dir="/tmp/flow-test/etc",
    teams={"1": TEST_TEAM},
)


@pytest.fixture
def mock_docker_client():
    with (
        patch("oduflow.docker_ops.system_ops.get_client") as sys_mock,
        patch("oduflow.docker_ops.env_ops.get_client") as env_mock,
        patch("oduflow.docker_ops.odoo_ops.get_client") as odoo_mock,
    ):
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
        mock_docker_client.networks.create.assert_called_once()
        mock_docker_client.volumes.create.assert_called_once()

    @patch("oduflow.docker_ops.system_ops._db_exists", return_value=True)
    @patch("oduflow.docker_ops.system_ops._wait_pg_ready")
    def test_init_system_already_initialized(
        self, mock_pg, mock_db_exists, mock_docker_client
    ):
        mock_docker_client.networks.get.return_value = MagicMock()
        mock_docker_client.volumes.get.return_value = MagicMock()
        db_container = MagicMock()
        db_container.status = "running"
        mock_docker_client.containers.get.return_value = db_container

        result = system_ops.init_system(TEST_SETTINGS)

        assert result["status"] == "initialized"


class TestDestroySystem:
    def test_destroy_with_active_envs(self, mock_docker_client):
        container = MagicMock()
        container.labels = {"oduflow.branch": "main", "oduflow.managed": "true"}
        container.name = "oduflow-main-odoo"
        mock_docker_client.containers.list.return_value = [container]

        with pytest.raises(ConflictError, match="Active environments"):
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
    @patch(
        "oduflow.extra_addons.generate_odoo_conf",
        return_value="/tmp/flow-test/workspaces/feature-payments/odoo.conf",
    )
    @patch("oduflow.docker_ops.env_ops._copy_file_to_container")
    @patch("oduflow.docker_ops.env_ops._create_pg_role")
    @patch(
        "oduflow.docker_ops.env_ops.create_credentials",
        return_value={"pg_user": "u_1_feature-payments", "pg_password": "test-pw"},
    )
    @patch("oduflow.docker_ops.env_ops._ensure_system_ready")
    @patch("oduflow.docker_ops.env_ops.get_odoo_uid_gid", return_value="100:101")
    @patch("oduflow.docker_ops.env_ops._exec_sql")
    @patch("oduflow.docker_ops.env_ops._db_exists", return_value=True)
    @patch("oduflow.docker_ops.env_ops._mount_filestore")
    @patch("oduflow.docker_ops.env_ops._get_used_ports", return_value=set())
    @patch("oduflow.docker_ops.env_ops.allocate_port", return_value=50000)
    @patch("oduflow.docker_ops.env_ops.subprocess.run")
    @patch("oduflow.docker_ops.env_ops.os.chmod")
    @patch("oduflow.docker_ops.env_ops.os.makedirs")
    @patch("oduflow.docker_ops.env_ops.os.path.exists", return_value=False)
    def test_create(
        self,
        mock_exists,
        mock_makedirs,
        mock_chmod,
        mock_run,
        mock_alloc,
        mock_used,
        mock_mount,
        mock_db_exists,
        mock_sql,
        mock_uid_gid,
        mock_ready,
        mock_creds,
        mock_role,
        mock_copy_conf,
        mock_gen_conf,
        mock_docker_client,
    ):
        mock_odoo = MagicMock()
        mock_odoo.exec_run.return_value = (0, b"OK")
        mock_docker_client.containers.run.return_value = mock_odoo
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        result = env_ops.create_environment(
            TEST_SETTINGS,
            TEST_TEAM,
            "feature/payments",
            "https://github.com/org/repo.git",
            "odoo:15.0",
        )

        assert result["url"] == "http://localhost:50000"
        assert result["database"] == "oduflow_1_feature-payments"
        assert result["odoo_container"] == "oduflow-feature-payments-odoo"
        assert mock_sql.call_count == 2
        mock_creds.assert_called_once()
        mock_role.assert_called_once()
        mock_docker_client.containers.run.assert_called_once()
        mock_alloc.assert_called_once()

    @patch("oduflow.docker_ops.env_ops._db_exists", return_value=True)
    @patch("oduflow.docker_ops.env_ops._ensure_system_ready")
    def test_create_already_exists(
        self, mock_ready, mock_db_exists, mock_docker_client
    ):
        existing = MagicMock()
        existing.status = "running"
        existing.ports = {"8069/tcp": [{"HostPort": "50000"}]}
        mock_docker_client.containers.get.return_value = existing

        with pytest.raises(ConflictError, match="already exists"):
            env_ops.create_environment(
                TEST_SETTINGS,
                TEST_TEAM,
                "main",
                "https://github.com/org/repo.git",
                "odoo:15.0",
            )

    @patch("oduflow.docker_ops.env_ops._db_exists", return_value=True)
    @patch("oduflow.docker_ops.env_ops._ensure_system_ready")
    def test_create_system_not_ready(
        self, mock_ready, mock_db_exists, mock_docker_client
    ):
        mock_ready.side_effect = PrerequisiteNotMetError(
            "flow-db not found. Run init_system first."
        )

        with pytest.raises(PrerequisiteNotMetError, match="init_system"):
            env_ops.create_environment(
                TEST_SETTINGS,
                TEST_TEAM,
                "main",
                "https://github.com/org/repo.git",
                "odoo:15.0",
            )

    @patch(
        "oduflow.extra_addons.generate_odoo_conf",
        return_value="/tmp/flow-test/workspaces/feature-no-tpl/odoo.conf",
    )
    @patch("oduflow.docker_ops.env_ops._copy_file_to_container")
    @patch("oduflow.docker_ops.env_ops._create_pg_role")
    @patch(
        "oduflow.docker_ops.env_ops.create_credentials",
        return_value={"pg_user": "u_1_feature-no-tpl", "pg_password": "test-pw"},
    )
    @patch("oduflow.docker_ops.env_ops._ensure_system_ready")
    @patch("oduflow.docker_ops.env_ops.get_odoo_uid_gid", return_value="100:101")
    @patch("oduflow.docker_ops.env_ops._exec_sql")
    @patch("oduflow.docker_ops.env_ops._db_exists", return_value=False)
    @patch("oduflow.docker_ops.env_ops._mount_filestore")
    @patch("oduflow.docker_ops.env_ops._get_used_ports", return_value=set())
    @patch("oduflow.docker_ops.env_ops.allocate_port", return_value=50001)
    @patch("oduflow.docker_ops.env_ops.subprocess.run")
    @patch("oduflow.docker_ops.env_ops.os.chmod")
    @patch("oduflow.docker_ops.env_ops.os.makedirs")
    @patch("oduflow.docker_ops.env_ops.os.path.exists", return_value=False)
    def test_create_no_template(
        self,
        mock_exists,
        mock_makedirs,
        mock_chmod,
        mock_run,
        mock_alloc,
        mock_used,
        mock_mount,
        mock_db_exists,
        mock_sql,
        mock_uid_gid,
        mock_ready,
        mock_creds,
        mock_role,
        mock_copy_conf,
        mock_gen_conf,
        mock_docker_client,
    ):
        mock_odoo = MagicMock()
        mock_odoo.exec_run.return_value = (0, b"OK")
        mock_docker_client.containers.run.return_value = mock_odoo
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        result = env_ops.create_environment(
            TEST_SETTINGS,
            TEST_TEAM,
            "feature/no-tpl",
            "https://github.com/org/repo.git",
            "odoo:15.0",
            template_name=None,
        )

        assert result["url"] == "http://localhost:50001"
        assert result["database"] == "oduflow_1_feature-no-tpl"
        # Should create empty DB (no TEMPLATE clause)
        create_db_call = mock_sql.call_args_list[0]
        assert "TEMPLATE" not in create_db_call[0][2]
        # Should NOT mount filestore
        mock_mount.assert_not_called()
        # Should run -i base --stop-after-init
        init_cmd = mock_odoo.exec_run.call_args[0][0]
        assert "-i base" in init_cmd
        assert "--stop-after-init" in init_cmd
        # Should restart after init
        mock_odoo.restart.assert_called_once()


class TestDeleteEnvironment:
    @patch("oduflow.docker_ops.env_ops._drop_pg_role")
    @patch(
        "oduflow.docker_ops.env_ops.load_credentials",
        return_value={"pg_user": "u_1_feature-payments", "pg_password": "test-pw"},
    )
    @patch("oduflow.docker_ops.env_ops.release_port")
    @patch("oduflow.docker_ops.env_ops._exec_sql")
    @patch("oduflow.docker_ops.env_ops.shutil.rmtree")
    @patch(
        "oduflow.docker_ops.env_ops.os.path.exists",
        side_effect=lambda p: ".protected" not in p,
    )
    def test_delete(
        self,
        mock_exists,
        mock_rmtree,
        mock_sql,
        mock_release,
        mock_load_creds,
        mock_drop_role,
        mock_docker_client,
    ):
        container = MagicMock()
        mock_docker_client.containers.get.return_value = container

        env_ops.delete_environment(TEST_SETTINGS, TEST_TEAM, "feature/payments")

        container.stop.assert_called_once()
        container.remove.assert_called_once()
        mock_drop_role.assert_called_once_with(
            mock_docker_client, TEST_SETTINGS, "u_1_feature-payments"
        )
        mock_sql.assert_called_once()
        mock_rmtree.assert_called_once()
        mock_release.assert_called_once_with(
            TEST_TEAM.port_registry_path, "feature/payments"
        )


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

        result = env_ops.stop_environment(TEST_SETTINGS, TEST_TEAM, "main")

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


class TestGetEnvironmentInfo:
    @patch(
        "oduflow.docker_ops.env_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "test-pw"},
    )
    def test_all_running(self, mock_load_creds, mock_docker_client):
        odoo = MagicMock()
        odoo.status = "running"
        odoo.labels = {
            "oduflow.template": "default",
            "oduflow.repo": "https://github.com/example/repo.git",
            "oduflow.image": "odoo:17.0",
            "oduflow.extra_addons": "{}",
        }
        odoo.attrs = {"NetworkSettings": {"Ports": {}}}
        db = MagicMock()
        db.status = "running"

        def get_container(name):
            if name == "oduflow-db":
                return db
            return odoo

        mock_docker_client.containers.get.side_effect = get_container

        result = env_ops.get_environment_info(TEST_SETTINGS, TEST_TEAM, "main")

        assert result["all_running"] is True
        assert result["db"]["name"] == "oduflow-db"
        assert result["db_name"] == "oduflow_1_main"
        assert result["db_user"] == "u_1_main"
        assert result["repo_url"] == "https://github.com/example/repo.git"
        assert result["odoo_image"] == "odoo:17.0"
        assert result["template_name"] == "default"


class TestInstallModules:
    def test_install(self, mock_docker_client):
        container = MagicMock()
        container.exec_run.return_value = (0, b"OK")
        mock_docker_client.containers.get.return_value = container

        result = odoo_ops.install_odoo_modules(
            TEST_SETTINGS, TEST_TEAM, "main", "sale", "crm"
        )

        assert result["exit_code"] == 0
        args = container.exec_run.call_args[0][0]
        assert "-d oduflow_1_main" in args
        assert "-i sale,crm" in args


class TestUpgradeModules:
    def test_upgrade(self, mock_docker_client):
        container = MagicMock()
        container.exec_run.return_value = (0, b"OK")
        mock_docker_client.containers.get.return_value = container

        result = odoo_ops.upgrade_odoo_modules(TEST_SETTINGS, TEST_TEAM, "main", "sale")

        assert result["exit_code"] == 0
        args = container.exec_run.call_args[0][0]
        assert "-d oduflow_1_main" in args
        assert "-u sale" in args


class TestRunEnvironmentTests:
    @patch(
        "oduflow.docker_ops.odoo_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "test-pw"},
    )
    def test_run(self, mock_load_creds, mock_docker_client):
        container = MagicMock()
        container.exec_run.return_value = (0, b"All tests passed")
        mock_docker_client.containers.get.return_value = container

        output = odoo_ops.run_environment_tests(
            TEST_SETTINGS, TEST_TEAM, "main", "base"
        )

        assert "All tests passed" in output
        args = container.exec_run.call_args[0][0]
        assert "--db_host=oduflow-db" in args
        assert "--database=oduflow_1_main" in args
        assert "-r u_1_main" in args


class TestGetLogs:
    def test_logs(self, mock_docker_client):
        container = MagicMock()
        container.logs.return_value = b"log line 1\nlog line 2"
        mock_docker_client.containers.get.return_value = container

        output = odoo_ops.get_environment_logs(TEST_SETTINGS, "main", 50)

        assert "log line 1" in output
        container.logs.assert_called_with(tail=50, stdout=True, stderr=True)
