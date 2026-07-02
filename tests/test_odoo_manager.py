import json
import os

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
    base_data_dir="/tmp/flow-test",
    db_user="odoo",
    db_password="odoo",
    etc_dir="/tmp/flow-test/etc",
    teams={"1": TEST_TEAM},
)


@pytest.fixture(autouse=True)
def _no_db_quota(monkeypatch):
    # Quota enforcement is covered by tests/test_db_quota.py; here it would
    # only add a psql exec to every mocked create_environment call chain.
    monkeypatch.setattr(
        "oduflow.docker_ops.env_ops.check_db_quota", lambda *a, **kw: None
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
        mock_odoo.wait.return_value = {"StatusCode": 0}
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
        # Greenfield: isolated init container + serving container = 2 run calls.
        assert mock_docker_client.containers.run.call_count == 2
        mock_alloc.assert_called_once()

    @patch("oduflow.docker_ops.env_ops.release_port")
    @patch("oduflow.docker_ops.env_ops._cleanup_old_environment")
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
    def test_create_rolls_back_on_run_failure(
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
        mock_cleanup,
        mock_release_port,
        mock_docker_client,
    ):
        # Reach the serving containers.run, then fail it (e.g. bad image / port
        # bind) and assert the partially-created resources are rolled back (#49).
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.run.side_effect = RuntimeError("bind failed")

        with pytest.raises(RuntimeError, match="bind failed"):
            env_ops.create_environment(
                TEST_SETTINGS,
                TEST_TEAM,
                "feature/payments",
                "https://github.com/org/repo.git",
                "odoo:15.0",
                template_name="base",
            )

        # Rollback: the allocated port is released and the environment teardown
        # runs (in addition to the pre-create cleanup at the top of the function).
        mock_release_port.assert_called_once_with(
            TEST_TEAM.port_registry_path, "feature/payments"
        )
        assert mock_cleanup.call_count == 2

    @patch("oduflow.docker_ops.env_ops._cleanup_old_environment")
    @patch("oduflow.docker_ops.env_ops._ensure_system_ready")
    def test_create_refuses_db_name_collision(
        self, mock_ready, mock_cleanup, mock_docker_client
    ):
        # "Feature/Foo" normalises to the same DB as a running "feature-foo";
        # creation must be refused instead of dropping the live env's DB (#41).
        from oduflow.errors import ConflictError

        other = MagicMock()
        other.name = "oduflow-feature-foo-odoo"
        other.labels = {"oduflow.branch": "feature-foo"}
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        mock_docker_client.containers.list.return_value = [other]

        with pytest.raises(ConflictError, match="normalise to the same database"):
            env_ops.create_environment(
                TEST_SETTINGS,
                TEST_TEAM,
                "Feature/Foo",
                "https://github.com/org/repo.git",
                "odoo:15.0",
            )
        # The destructive cleanup (which drops the DB) must not run.
        mock_cleanup.assert_not_called()

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
            "flow-db not found. System not initialized. Restart oduflow."
        )

        with pytest.raises(PrerequisiteNotMetError, match="not initialized"):
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
        mock_odoo.wait.return_value = {"StatusCode": 0}
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
        # Greenfield init runs `-i base` in a SEPARATE isolated container
        # (the FIRST containers.run), before the serving container — so the
        # serving PID1 never races it. containers.run is called twice.
        assert mock_docker_client.containers.run.call_count == 2
        init_run = mock_docker_client.containers.run.call_args_list[0]
        assert "-i base" in init_run.kwargs["command"]
        assert "--stop-after-init" in init_run.kwargs["command"]

    @patch(
        "oduflow.extra_addons.generate_odoo_conf",
        return_value="/tmp/flow-test/workspaces/feature-local/odoo.conf",
    )
    @patch("oduflow.docker_ops.env_ops._copy_file_to_container")
    @patch("oduflow.docker_ops.env_ops._create_pg_role")
    @patch(
        "oduflow.docker_ops.env_ops.create_credentials",
        return_value={"pg_user": "u_1_feature-local", "pg_password": "test-pw"},
    )
    @patch("oduflow.docker_ops.env_ops._ensure_system_ready")
    @patch("oduflow.docker_ops.env_ops.get_odoo_uid_gid", return_value="100:101")
    @patch("oduflow.docker_ops.env_ops._exec_sql")
    @patch("oduflow.docker_ops.env_ops._db_exists", return_value=False)
    @patch("oduflow.docker_ops.env_ops._mount_filestore")
    @patch("oduflow.docker_ops.env_ops._get_used_ports", return_value=set())
    @patch("oduflow.docker_ops.env_ops.allocate_port", return_value=50002)
    @patch("oduflow.docker_ops.env_ops.subprocess.run")
    @patch("oduflow.docker_ops.env_ops.os.chmod")
    @patch("oduflow.docker_ops.env_ops.os.makedirs")
    @patch("oduflow.docker_ops.env_ops.os.path.exists", return_value=False)
    def test_create_local_path_skips_clone(
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
        tmp_path,
    ):
        mock_odoo = MagicMock()
        mock_odoo.exec_run.return_value = (0, b"OK")
        mock_odoo.wait.return_value = {"StatusCode": 0}
        mock_docker_client.containers.run.return_value = mock_odoo
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        local_dir = str(tmp_path)
        result = env_ops.create_environment(
            TEST_SETTINGS,
            TEST_TEAM,
            "feature/local",
            "",  # no repo_url in live-mount mode
            "odoo:17.0",
            template_name=None,
            local_path=local_dir,
        )

        # No git clone was invoked.
        for call in mock_run.call_args_list:
            assert "clone" not in (call.args[0] if call.args else [])
            assert "init" not in (call.args[0] if call.args else [])
        # Result and container label point at the live-mount directory.
        abs_local = os.path.abspath(local_dir)
        assert result["local_path"] == abs_local
        run_kwargs = mock_docker_client.containers.run.call_args.kwargs
        assert run_kwargs["labels"]["oduflow.local_path"] == abs_local
        # The local dir is bind-mounted to /mnt/extra-addons.
        assert run_kwargs["volumes"][abs_local]["bind"] == "/mnt/extra-addons"

    def test_create_local_path_rejects_when_disabled(self, tmp_path):
        settings = Settings(
            base_data_dir="/tmp/flow-test",
            db_user="odoo",
            db_password="odoo",
            etc_dir="/tmp/flow-test/etc",
            allow_local_path=False,
            teams={"1": TEST_TEAM},
        )

        with (
            patch("oduflow.docker_ops.env_ops.get_client") as mock_get_client,
            patch("oduflow.docker_ops.env_ops._ensure_system_ready"),
            patch("oduflow.docker_ops.env_ops._cleanup_old_environment"),
        ):
            mock_get_client.return_value.containers.get.side_effect = (
                docker.errors.NotFound("nf")
            )
            with pytest.raises(PrerequisiteNotMetError, match="local_path.*disabled"):
                env_ops.create_environment(
                    settings,
                    TEST_TEAM,
                    "feature/local",
                    "",
                    "odoo:17.0",
                    template_name=None,
                    local_path=str(tmp_path),
                )


class TestReloadTemplate:
    @patch("oduflow.docker_ops.system_ops._update_template_sizes")
    @patch("oduflow.docker_ops.system_ops._copy_file_to_container")
    @patch("oduflow.docker_ops.system_ops._is_text_dump", return_value=False)
    @patch("oduflow.docker_ops.system_ops._db_exists", return_value=False)
    @patch("oduflow.docker_ops.system_ops._exec_sql", return_value="5")
    @patch("oduflow.docker_ops.system_ops._wait_pg_ready")
    @patch("oduflow.docker_ops.system_ops.os.path.isfile", return_value=True)
    def test_restore_uses_no_owner(
        self,
        mock_isfile,
        mock_wait,
        mock_sql,
        mock_db_exists,
        mock_text,
        mock_copy,
        mock_sizes,
        mock_docker_client,
    ):
        # Custom-format dumps must be restored with --no-owner so the template
        # is not pinned to the source env's per-env role; otherwise deleting the
        # source env leaves an undroppable orphan role. (--no-owner is honored
        # only at restore time for -Fc archives, not at pg_dump time.)
        db_container = MagicMock()
        db_container.exec_run.return_value = (0, b"")
        mock_docker_client.containers.get.return_value = db_container

        system_ops.reload_template(TEST_SETTINGS, TEST_TEAM, "mytpl")

        restore_cmd = db_container.exec_run.call_args[0][0]
        joined = " ".join(restore_cmd)
        assert "pg_restore" in joined
        assert "--no-owner" in joined


class TestPullEnvironmentLocal:
    def test_local_snapshot_detects_added_modified_deleted(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))

        modified = repo / "sale" / "models" / "sale.py"
        deleted = repo / "sale" / "views" / "old.xml"
        unchanged = repo / "sale" / "views" / "same.xml"
        modified.parent.mkdir(parents=True)
        deleted.parent.mkdir(parents=True)
        modified.write_text("old")
        deleted.write_text("<old/>")
        unchanged.write_text("<same/>")

        env_ops._write_local_snapshot(str(repo), "env", team)

        old_stat = modified.stat()
        modified.write_text("new")
        os.utime(
            modified,
            ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1_000_000),
        )
        deleted.unlink()
        added = repo / "sale" / "views" / "new.xml"
        added.write_text("<new/>")

        base_ref, changed = env_ops._detect_local_changes(str(repo), "env", team)

        assert base_ref is None
        assert changed == [
            "sale/models/sale.py",
            "sale/views/new.xml",
            "sale/views/old.xml",
        ]

    def test_local_snapshot_repeated_after_advance_is_clean(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
        path = repo / "sale" / "models" / "sale.py"
        path.parent.mkdir(parents=True)
        path.write_text("old")

        env_ops._write_local_snapshot(str(repo), "env", team)
        old_stat = path.stat()
        path.write_text("new")
        os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1_000_000))

        assert env_ops._detect_local_changes(str(repo), "env", team)[1] == [
            "sale/models/sale.py"
        ]
        env_ops._write_local_snapshot(str(repo), "env", team)
        assert env_ops._detect_local_changes(str(repo), "env", team)[1] == []

    @patch("oduflow.docker_ops.env_ops._apply_actions")
    def test_local_auto_uses_path_only_classification(
        self, mock_apply, mock_docker_client, tmp_path
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
        py_file = repo / "sale" / "models" / "sale.py"
        py_file.parent.mkdir(parents=True)
        py_file.write_text("old")
        env_ops._write_local_snapshot(str(repo), "env", team)

        old_stat = py_file.stat()
        py_file.write_text("new")
        os.utime(py_file, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1_000_000))

        container = MagicMock()
        container.labels = {"oduflow.local_path": str(repo)}
        mock_docker_client.containers.get.return_value = container
        mock_apply.return_value = {
            "action": "restart",
            "changed_files": ["sale/models/sale.py"],
            "message": "Container restarted.",
        }

        result = env_ops.pull_environment(TEST_SETTINGS, team, "env")

        assert result["action"] == "restart"
        assert mock_apply.call_args.kwargs["do_restart"] is True
        assert env_ops._detect_local_changes(str(repo), "env", team)[1] == []

    @patch("oduflow.docker_ops.env_ops._apply_actions")
    def test_local_failed_apply_does_not_advance_snapshot(
        self, mock_apply, mock_docker_client, tmp_path
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
        xml_file = repo / "sale" / "security" / "rules.xml"
        xml_file.parent.mkdir(parents=True)
        xml_file.write_text("<old/>")
        env_ops._write_local_snapshot(str(repo), "env", team)

        old_stat = xml_file.stat()
        xml_file.write_text("<new/>")
        os.utime(xml_file, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1_000_000))

        container = MagicMock()
        container.labels = {"oduflow.local_path": str(repo)}
        mock_docker_client.containers.get.return_value = container
        mock_apply.return_value = {
            "action": "upgrade",
            "modules_upgraded": ["sale"],
            "exit_code": 1,
            "changed_files": ["sale/security/rules.xml"],
            "message": "Upgraded modules: sale",
        }

        result = env_ops.pull_environment(TEST_SETTINGS, team, "env", upgrade=["sale"])

        assert result["exit_code"] == 1
        assert env_ops._detect_local_changes(str(repo), "env", team)[1] == [
            "sale/security/rules.xml"
        ]

    @patch("oduflow.docker_ops.env_ops._apply_actions")
    def test_local_strict_block_does_not_advance_snapshot(
        self, mock_apply, mock_docker_client, tmp_path
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team"))
        xml_file = repo / "sale" / "security" / "rules.xml"
        xml_file.parent.mkdir(parents=True)
        xml_file.write_text("<old/>")
        env_ops._write_local_snapshot(str(repo), "env", team)

        old_stat = xml_file.stat()
        xml_file.write_text("<new/>")
        os.utime(xml_file, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 1_000_000))

        container = MagicMock()
        container.labels = {"oduflow.local_path": str(repo)}
        mock_docker_client.containers.get.return_value = container

        result = env_ops.pull_environment(
            TEST_SETTINGS, team, "env", restart=True, strict=True
        )

        assert result["action"] == "blocked"
        mock_apply.assert_not_called()
        assert env_ops._detect_local_changes(str(repo), "env", team)[1] == [
            "sale/security/rules.xml"
        ]

    @patch("oduflow.docker_ops.env_ops._write_local_snapshot")
    @patch("oduflow.docker_ops.env_ops._apply_actions")
    @patch("oduflow.docker_ops.env_ops._detect_local_changes")
    @patch("oduflow.docker_ops.env_ops.os.path.isdir", return_value=True)
    def test_local_explicit_guardrail_warns(
        self, mock_isdir, mock_detect, mock_apply, mock_write, mock_docker_client
    ):
        container = MagicMock()
        container.labels = {"oduflow.local_path": "/some/local"}
        mock_docker_client.containers.get.return_value = container
        # A security XML change recommends -u of 'sale'.
        mock_detect.return_value = (None, ["sale/security/ir_rule.xml"])
        mock_apply.return_value = {
            "action": "restart",
            "changed_files": ["sale/security/ir_rule.xml"],
            "message": "Container restarted.",
        }

        result = env_ops.pull_environment(
            TEST_SETTINGS, TEST_TEAM, "feature/local", restart=True
        )

        assert result["action"] == "restart"
        assert any("sale" in w for w in result.get("warnings", []))
        mock_apply.assert_called_once()
        mock_detect.assert_called_once()
        mock_write.assert_called_once()

    @patch("oduflow.docker_ops.env_ops._write_local_snapshot")
    @patch("oduflow.docker_ops.env_ops._apply_actions")
    @patch("oduflow.docker_ops.env_ops._detect_local_changes")
    @patch("oduflow.docker_ops.env_ops.os.path.isdir", return_value=True)
    def test_local_strict_blocks(
        self, mock_isdir, mock_detect, mock_apply, mock_write, mock_docker_client
    ):
        container = MagicMock()
        container.labels = {"oduflow.local_path": "/some/local"}
        mock_docker_client.containers.get.return_value = container
        mock_detect.return_value = (None, ["sale/security/ir_rule.xml"])

        result = env_ops.pull_environment(
            TEST_SETTINGS, TEST_TEAM, "feature/local", restart=True, strict=True
        )

        assert result["action"] == "blocked"
        assert any("sale" in w for w in result["warnings"])
        mock_apply.assert_not_called()
        mock_write.assert_not_called()

    @patch("oduflow.docker_ops.env_ops._write_local_snapshot")
    @patch("oduflow.docker_ops.env_ops._apply_actions")
    @patch("oduflow.docker_ops.env_ops._detect_local_changes")
    @patch("oduflow.docker_ops.env_ops.os.path.isdir", return_value=True)
    def test_local_explicit_correct_no_warn(
        self, mock_isdir, mock_detect, mock_apply, mock_write, mock_docker_client
    ):
        container = MagicMock()
        container.labels = {"oduflow.local_path": "/some/local"}
        mock_docker_client.containers.get.return_value = container
        mock_detect.return_value = (None, ["sale/security/ir_rule.xml"])
        mock_apply.return_value = {
            "action": "upgrade",
            "modules_upgraded": ["sale"],
            "changed_files": ["sale/security/ir_rule.xml"],
            "message": "Upgraded.",
        }

        result = env_ops.pull_environment(
            TEST_SETTINGS, TEST_TEAM, "feature/local", upgrade=["sale"]
        )

        assert result["action"] == "upgrade"
        assert not result.get("warnings")
        mock_apply.assert_called_once()
        mock_write.assert_called_once()


class TestCreateEnvironmentEnvVars:
    @patch(
        "oduflow.extra_addons.generate_odoo_conf",
        return_value="/tmp/flow-test/workspaces/feature-env/odoo.conf",
    )
    @patch("oduflow.docker_ops.env_ops._copy_file_to_container")
    @patch("oduflow.docker_ops.env_ops._create_pg_role")
    @patch(
        "oduflow.docker_ops.env_ops.create_credentials",
        return_value={"pg_user": "u_1_feature-env", "pg_password": "test-pw"},
    )
    @patch("oduflow.docker_ops.env_ops._ensure_system_ready")
    @patch("oduflow.docker_ops.env_ops.get_odoo_uid_gid", return_value="100:101")
    @patch("oduflow.docker_ops.env_ops._exec_sql")
    @patch("oduflow.docker_ops.env_ops._db_exists", return_value=False)
    @patch("oduflow.docker_ops.env_ops._mount_filestore")
    @patch("oduflow.docker_ops.env_ops._get_used_ports", return_value=set())
    @patch("oduflow.docker_ops.env_ops.allocate_port", return_value=50002)
    @patch("oduflow.docker_ops.env_ops.subprocess.run")
    @patch("oduflow.docker_ops.env_ops.os.chmod")
    @patch("oduflow.docker_ops.env_ops.os.makedirs")
    @patch("oduflow.docker_ops.env_ops.os.path.exists", return_value=False)
    def test_create_merges_env_vars_and_label(
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
        import json

        mock_odoo = MagicMock()
        mock_odoo.exec_run.return_value = (0, b"OK")
        mock_docker_client.containers.run.return_value = mock_odoo
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        env_ops.create_environment(
            TEST_SETTINGS,
            TEST_TEAM,
            "feature/env",
            "https://github.com/org/repo.git",
            "odoo:15.0",
            template_name=None,
            env_vars={"FOO": "bar", "BAZ": "qux"},
        )

        run_kwargs = mock_docker_client.containers.run.call_args.kwargs
        environment = run_kwargs["environment"]
        assert environment["FOO"] == "bar"
        assert environment["BAZ"] == "qux"
        assert environment["HOST"] == TEST_SETTINGS.shared_db_container
        assert environment["USER"] == "u_1_feature-env"
        stored = json.loads(run_kwargs["labels"]["oduflow.env_vars"])
        assert stored == {"FOO": "bar", "BAZ": "qux"}


class TestUpdateEnvironment:
    def _make_container(self):
        container = MagicMock()
        container.image.tags = ["odoo:15.0"]
        container.image.id = "sha256:old"
        container.labels = {
            "oduflow.template": "none",
            TEST_SETTINGS.image_label: "odoo:15.0",
            "oduflow.env_vars": '{"OLD": "1"}',
        }
        container.attrs = {
            "Config": {
                "Env": ["HOST=db", "USER=old", "PASSWORD=x", "OLD=1"],
                "Cmd": ["odoo", "-d", "oduflow_1_main", "--dev=xml"],
                "Image": "odoo:15.0",
            },
            "HostConfig": {
                "Binds": ["/host/repo:/mnt/extra-addons:rw"],
                "PortBindings": {"8069/tcp": [{"HostPort": "50000"}]},
            },
        }
        return container

    @patch(
        "oduflow.docker_ops.env_ops._install_pip_requirements", return_value=(False, "")
    )
    @patch("oduflow.docker_ops.env_ops._install_apt_packages", return_value="")
    @patch("oduflow.docker_ops.env_ops._resolve_instance_conf")
    @patch("oduflow.docker_ops.env_ops.os.path.isfile", return_value=False)
    @patch("oduflow.docker_ops.env_ops.os.path.isdir", return_value=False)
    @patch("oduflow.docker_ops.env_ops._create_pg_role")
    @patch(
        "oduflow.docker_ops.env_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "pw"},
    )
    def test_update_image_and_env_override(
        self,
        mock_creds,
        mock_role,
        mock_isdir,
        mock_isfile,
        mock_resolve_conf,
        mock_apt,
        mock_pip,
        mock_docker_client,
    ):
        import json

        mock_resolve_conf.return_value.exists.return_value = False
        container = self._make_container()
        mock_docker_client.containers.get.return_value = container
        new_image = MagicMock()
        new_image.id = "sha256:new"
        mock_docker_client.images.pull.return_value = new_image
        mock_docker_client.containers.run.return_value = MagicMock()

        result = env_ops.update_environment(
            TEST_SETTINGS,
            TEST_TEAM,
            "main",
            env_override={"FOO": "new"},
            image_override="odoo:17.0",
        )

        run_kwargs = mock_docker_client.containers.run.call_args.kwargs
        assert run_kwargs["image"] == "odoo:17.0"
        assert run_kwargs["environment"] == {
            "HOST": TEST_SETTINGS.shared_db_container,
            "USER": "u_1_main",
            "PASSWORD": "pw",
            "FOO": "new",
        }
        assert run_kwargs["labels"][TEST_SETTINGS.image_label] == "odoo:17.0"
        assert json.loads(run_kwargs["labels"]["oduflow.env_vars"]) == {"FOO": "new"}
        mock_docker_client.images.pull.assert_called_once_with("odoo:17.0")
        assert result["image"] == "odoo:17.0"
        assert result["image_updated"] is True
        assert result["env_vars"] == {"FOO": "new"}

    def test_update_aborts_when_image_unavailable(self, mock_docker_client):
        # Image can't be pulled and isn't local: abort WITHOUT removing the old
        # container, so the env is never left with no container (#49).
        from oduflow.errors import PrerequisiteNotMetError

        container = self._make_container()
        mock_docker_client.containers.get.return_value = container
        mock_docker_client.images.pull.side_effect = RuntimeError("network down")
        mock_docker_client.images.get.side_effect = docker.errors.ImageNotFound("nf")

        with pytest.raises(PrerequisiteNotMetError, match="could not be pulled"):
            env_ops.update_environment(
                TEST_SETTINGS, TEST_TEAM, "main", image_override="odoo:99.0"
            )

        # The existing container must be left running.
        container.stop.assert_not_called()
        container.remove.assert_not_called()
        mock_docker_client.containers.run.assert_not_called()

    @patch(
        "oduflow.docker_ops.env_ops._install_pip_requirements", return_value=(False, "")
    )
    @patch("oduflow.docker_ops.env_ops._install_apt_packages", return_value="")
    @patch("oduflow.docker_ops.env_ops._resolve_instance_conf")
    @patch("oduflow.docker_ops.env_ops.os.path.isfile", return_value=False)
    @patch("oduflow.docker_ops.env_ops.os.path.isdir", return_value=False)
    @patch("oduflow.docker_ops.env_ops._create_pg_role")
    @patch(
        "oduflow.docker_ops.env_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "pw"},
    )
    def test_update_no_overrides_keeps_label_env(
        self,
        mock_creds,
        mock_role,
        mock_isdir,
        mock_isfile,
        mock_resolve_conf,
        mock_apt,
        mock_pip,
        mock_docker_client,
    ):
        mock_resolve_conf.return_value.exists.return_value = False
        container = self._make_container()
        mock_docker_client.containers.get.return_value = container
        same_image = MagicMock()
        same_image.id = "sha256:old"
        mock_docker_client.images.pull.return_value = same_image
        mock_docker_client.containers.run.return_value = MagicMock()

        result = env_ops.update_environment(TEST_SETTINGS, TEST_TEAM, "main")

        run_kwargs = mock_docker_client.containers.run.call_args.kwargs
        # No image override → current image is reused and re-pulled
        assert run_kwargs["image"] == "odoo:15.0"
        mock_docker_client.images.pull.assert_called_once_with("odoo:15.0")
        # No env override → env restored from the persisted label
        assert run_kwargs["environment"]["OLD"] == "1"
        assert result["env_vars"] == {"OLD": "1"}
        assert result["image_updated"] is False

    @patch(
        "oduflow.docker_ops.env_ops.load_credentials",
        return_value={"pg_user": "u_1_xyz", "pg_password": "pw"},
    )
    def test_update_missing_raises(self, mock_creds, mock_docker_client):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")
        with pytest.raises(NotFoundError, match="does not exist"):
            env_ops.update_environment(TEST_SETTINGS, TEST_TEAM, "xyz")


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
        container.labels = {"oduflow.team": "1"}
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

    @patch("oduflow.docker_ops.env_ops.release_port")
    @patch("oduflow.docker_ops.env_ops._exec_sql")
    @patch("oduflow.docker_ops.env_ops.os.path.exists", return_value=False)
    def test_delete_missing_raises_not_found(
        self,
        mock_exists,
        mock_sql,
        mock_release,
        mock_docker_client,
    ):
        mock_docker_client.containers.get.side_effect = docker.errors.NotFound("nf")

        with pytest.raises(NotFoundError, match="does not exist"):
            env_ops.delete_environment(TEST_SETTINGS, TEST_TEAM, "ghost")

        mock_release.assert_not_called()
        mock_sql.assert_not_called()


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
        container.labels = {"oduflow.team": "1"}
        mock_docker_client.containers.get.return_value = container

        result = env_ops.stop_environment(TEST_SETTINGS, TEST_TEAM, "main")

        assert "oduflow-main-odoo" in result["stopped"]
        container.stop.assert_called_once()

    def test_stop_rejects_other_team_container(self, mock_docker_client):
        # Container names are not team-namespaced; a caller scoped to team 1
        # must not stop a container labelled for team 2 (issue #39).
        container = MagicMock()
        container.labels = {"oduflow.team": "2"}
        mock_docker_client.containers.get.return_value = container

        with pytest.raises(NotFoundError, match="does not exist"):
            env_ops.stop_environment(TEST_SETTINGS, TEST_TEAM, "main")
        container.stop.assert_not_called()


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
        container.labels = {TEST_SETTINGS.image_label: "odoo:17.0"}
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
        # Use -u (upgrade), not -i: the module is already installed, and -i would be
        # a no-op that never runs the test phase ("0 of 0 tests").
        assert "-u base" in args
        # Tests run via `docker exec` inside the already-running odoo container,
        # which already holds 8069/8072. --no-http is ignored under --test-enable
        # (tests need a live HTTP server), so the test server's ports are moved off
        # the defaults to avoid the bind conflict.
        assert "--http-port 8089" in args
        # Odoo 17 → modern --gevent-port flag.
        assert "--gevent-port 8090" in args
        assert "--workers 0" in args

    @patch(
        "oduflow.docker_ops.odoo_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "test-pw"},
    )
    def test_run_odoo_15_uses_longpolling_port(
        self, mock_load_creds, mock_docker_client
    ):
        # Odoo 15.0 has no --gevent-port (introduced in 16.0); it must get the
        # legacy --longpolling-port instead, parsed from the image tag.
        container = MagicMock()
        container.labels = {TEST_SETTINGS.image_label: "odoo:15.0"}
        container.exec_run.return_value = (0, b"All tests passed")
        mock_docker_client.containers.get.return_value = container

        odoo_ops.run_environment_tests(TEST_SETTINGS, TEST_TEAM, "main", "base")

        args = container.exec_run.call_args[0][0]
        assert "--longpolling-port 8090" in args
        assert "--gevent-port" not in args

    @patch(
        "oduflow.docker_ops.odoo_ops.load_credentials",
        return_value={"pg_user": "u_1_main", "pg_password": "test-pw"},
    )
    def test_run_custom_image_detects_version_via_binary(
        self, mock_load_creds, mock_docker_client
    ):
        # Custom-tagged image carries no version in the tag, so the major version
        # is resolved from `odoo --version` on the live container.
        container = MagicMock()
        container.labels = {TEST_SETTINGS.image_label: "oduist/customer_odoo"}
        container.exec_run.side_effect = [
            (0, b"Odoo Server 15.0\n"),  # odoo --version probe
            (0, b"All tests passed"),  # actual test run
        ]
        mock_docker_client.containers.get.return_value = container

        odoo_ops.run_environment_tests(TEST_SETTINGS, TEST_TEAM, "main", "base")

        assert container.exec_run.call_args_list[0][0][0] == "odoo --version"
        test_cmd = container.exec_run.call_args_list[1][0][0]
        assert "--longpolling-port 8090" in test_cmd
        assert "--gevent-port" not in test_cmd


class TestGetLogs:
    def test_logs(self, mock_docker_client):
        container = MagicMock()
        container.logs.return_value = b"log line 1\nlog line 2"
        mock_docker_client.containers.get.return_value = container

        output = odoo_ops.get_environment_logs(TEST_SETTINGS, "main", 50)

        assert "log line 1" in output
        container.logs.assert_called_with(tail=50, stdout=True, stderr=True)


class TestApplyActionsConf:
    """A changed ``.oduflow/odoo.conf`` must be reconstructed before a restart.

    PR #69 only triggered a plain restart, which reuses the stale
    ``/etc/odoo/odoo.conf`` copy and never picks up the new config.
    """

    @staticmethod
    def _client_with_container():
        client = MagicMock()
        container = MagicMock()
        client.containers.get.return_value = container
        return client, container

    @patch("oduflow.docker_ops.env_ops._reapply_odoo_conf", return_value=True)
    def test_restart_reapplies_conf_when_config_changed(self, mock_reapply):
        client, container = self._client_with_container()
        result = env_ops._apply_actions(
            client,
            TEST_SETTINGS,
            TEST_TEAM,
            "feature/x",
            "oduflow-feature-x-odoo",
            to_install=[],
            to_upgrade=[],
            do_restart=True,
            changed_files=[".oduflow/odoo.conf"],
            config_changed=True,
        )
        mock_reapply.assert_called_once_with(
            TEST_SETTINGS, TEST_TEAM, "feature/x", container
        )
        container.restart.assert_called_once()
        assert result["action"] == "restart"
        assert "odoo.conf" in result["message"]

    @patch("oduflow.docker_ops.env_ops._reapply_odoo_conf")
    def test_restart_skips_conf_when_only_python(self, mock_reapply):
        client, container = self._client_with_container()
        result = env_ops._apply_actions(
            client,
            TEST_SETTINGS,
            TEST_TEAM,
            "feature/x",
            "oduflow-feature-x-odoo",
            to_install=[],
            to_upgrade=[],
            do_restart=True,
            changed_files=["sale/models/sale.py"],
            config_changed=False,
        )
        mock_reapply.assert_not_called()
        container.restart.assert_called_once()
        assert result["message"] == "Container restarted."

    @patch(
        "oduflow.docker_ops.odoo_ops.upgrade_odoo_modules",
        return_value={"exit_code": 0, "output": "upgraded"},
    )
    @patch("oduflow.docker_ops.env_ops._reapply_odoo_conf", return_value=True)
    def test_upgrade_also_reapplies_conf(self, mock_reapply, mock_upgrade):
        client, container = self._client_with_container()
        result = env_ops._apply_actions(
            client,
            TEST_SETTINGS,
            TEST_TEAM,
            "feature/x",
            "oduflow-feature-x-odoo",
            to_install=[],
            to_upgrade=["sale"],
            do_restart=False,
            changed_files=[".oduflow/odoo.conf", "sale/security/groups.xml"],
            config_changed=True,
        )
        mock_upgrade.assert_called_once()
        mock_reapply.assert_called_once()
        container.restart.assert_called_once()
        assert result["action"] == "upgrade"
        assert "Reapplied odoo.conf." in result["message"]


class TestReapplyOdooConf:
    @patch("oduflow.docker_ops.env_ops._copy_file_to_container")
    @patch(
        "oduflow.extra_addons.generate_odoo_conf",
        return_value="/tmp/flow-test/workspaces/feature-x/odoo.conf",
    )
    @patch("oduflow.docker_ops.env_ops.os.path.isfile", return_value=True)
    def test_uses_repo_conf_and_copies(self, mock_isfile, mock_gen, mock_copy):
        container = MagicMock()
        container.labels = {}
        applied = env_ops._reapply_odoo_conf(
            TEST_SETTINGS, TEST_TEAM, "feature/x", container
        )
        assert applied is True
        base_arg, _generated, extra_arg, main_addons = mock_gen.call_args[0]
        assert base_arg.endswith("/.oduflow/odoo.conf")
        assert extra_arg == []
        assert main_addons == "/mnt/extra-addons"
        mock_copy.assert_called_once()
        assert mock_copy.call_args[0][0] is container
        assert mock_copy.call_args[0][2] == "/etc/odoo"

    @patch("oduflow.docker_ops.env_ops._copy_file_to_container")
    @patch(
        "oduflow.extra_addons.generate_odoo_conf",
        return_value="/tmp/flow-test/workspaces/feature-x/odoo.conf",
    )
    @patch("oduflow.docker_ops.env_ops.os.path.isfile", return_value=True)
    def test_merges_extra_addons_paths(self, mock_isfile, mock_gen, mock_copy):
        container = MagicMock()
        container.labels = {"oduflow.extra_addons": json.dumps({"enterprise": "17.0"})}
        env_ops._reapply_odoo_conf(TEST_SETTINGS, TEST_TEAM, "feature/x", container)
        _base, _generated, extra_arg, _main = mock_gen.call_args[0]
        assert extra_arg == ["/mnt/extra-addons-enterprise"]

    @patch("oduflow.docker_ops.env_ops._copy_file_to_container")
    @patch("oduflow.docker_ops.env_ops._resolve_instance_conf")
    @patch("oduflow.docker_ops.env_ops.os.path.isfile", return_value=False)
    def test_returns_false_when_no_base_conf(
        self, mock_isfile, mock_resolve, mock_copy
    ):
        inst = MagicMock()
        inst.exists.return_value = False
        mock_resolve.return_value = inst
        container = MagicMock()
        container.labels = {}
        applied = env_ops._reapply_odoo_conf(
            TEST_SETTINGS, TEST_TEAM, "feature/x", container
        )
        assert applied is False
        mock_copy.assert_not_called()
