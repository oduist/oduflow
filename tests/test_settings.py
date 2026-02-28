import pytest
from oduflow.settings import Settings, TeamSettings


class TestSettings:
    def test_defaults(self):
        s = Settings()
        assert s.external_host == "localhost"
        assert s.db_user == "odoo"
        assert s.routing_mode == "port"

    def test_validate_no_teams(self):
        s = Settings()
        with pytest.raises(ValueError, match="No \\[team\\.\\*\\] sections"):
            s.validate()

    def test_validate_ok(self):
        team = TeamSettings(team_id="1", port_range_start=50000, port_range_end=50100)
        s = Settings(teams={"1": team})
        s.validate()

    def test_validate_port_range(self):
        team = TeamSettings(team_id="1", port_range_start=50100, port_range_end=50000)
        s = Settings(teams={"1": team})
        with pytest.raises(ValueError, match="invalid port range"):
            s.validate()

    def test_frozen(self):
        s = Settings()
        with pytest.raises(AttributeError):
            s.external_host = "changed"  # type: ignore[misc]

    def test_get_team(self):
        team = TeamSettings(team_id="1")
        s = Settings(teams={"1": team})
        assert s.get_team("1") is team

    def test_get_team_not_found(self):
        s = Settings(teams={})
        with pytest.raises(ValueError, match="Team '99' not found"):
            s.get_team("99")

    def test_get_team_by_token(self):
        team = TeamSettings(team_id="1", auth_token="secret-token")
        s = Settings(teams={"1": team})
        assert s.get_team_by_token("secret-token") is team
        assert s.get_team_by_token("wrong") is None

    def test_validate_duplicate_tokens(self):
        t1 = TeamSettings(
            team_id="1", auth_token="same", port_range_start=50000, port_range_end=50100
        )
        t2 = TeamSettings(
            team_id="2", auth_token="same", port_range_start=50100, port_range_end=50200
        )
        s = Settings(teams={"1": t1, "2": t2})
        with pytest.raises(ValueError, match="Duplicate auth_token"):
            s.validate()


class TestTeamSettings:
    def test_defaults(self):
        t = TeamSettings(team_id="1")
        assert t.team_id == "1"
        assert t.port_range_start == 50000
        assert t.port_range_end == 50100

    def test_workspaces_dir(self):
        t = TeamSettings(team_id="1", data_dir="/srv/data")
        assert t.workspaces_dir == "/srv/data/workspaces"

    def test_shared_repos_dir(self):
        t = TeamSettings(team_id="1", data_dir="/srv/data")
        assert t.shared_repos_dir == "/srv/data/shared_repos"

    def test_frozen(self):
        t = TeamSettings(team_id="1")
        with pytest.raises(AttributeError):
            t.team_id = "2"  # type: ignore[misc]


class TestTeamTemplatePaths:
    def test_get_template_dir(self):
        t = TeamSettings(team_id="1", data_dir="/srv/data")
        assert t.get_template_dir("default") == "/srv/data/templates/default"

    def test_get_template_dir_named(self):
        t = TeamSettings(team_id="1", data_dir="/srv/data")
        assert t.get_template_dir("myproject") == "/srv/data/templates/myproject"

    def test_get_template_sql_path(self):
        t = TeamSettings(team_id="1", data_dir="/srv/data")
        assert t.get_template_sql_path("v17") == "/srv/data/templates/v17/dump.pgdump"

    def test_get_template_filestore_path(self):
        t = TeamSettings(team_id="1", data_dir="/srv/data")
        assert (
            t.get_template_filestore_path("v17") == "/srv/data/templates/v17/filestore"
        )

    def test_dump_methods_delegate_to_template(self):
        t = TeamSettings(team_id="1", data_dir="/srv/data")
        assert (
            t.get_template_sql_path("default")
            == "/srv/data/templates/default/dump.pgdump"
        )
        assert (
            t.get_template_filestore_path("default")
            == "/srv/data/templates/default/filestore"
        )

    def test_list_templates_empty(self, tmp_path):
        t = TeamSettings(team_id="1", data_dir=str(tmp_path))
        assert t.list_templates() == []

    def test_list_templates(self, tmp_path):
        templates_dir = tmp_path / "templates"
        (templates_dir / "alpha").mkdir(parents=True)
        (templates_dir / "beta").mkdir(parents=True)
        (templates_dir / "not-a-dir").touch()  # should be ignored
        t = TeamSettings(team_id="1", data_dir=str(tmp_path))
        assert t.list_templates() == ["alpha", "beta"]

    def test_git_credentials_file(self):
        t = TeamSettings(team_id="1", data_dir="/srv/data")
        assert t.git_credentials_file() == "/srv/data/.git-credentials"
