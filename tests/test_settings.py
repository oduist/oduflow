import pytest
from oduflow.settings import Settings, TeamSettings


class TestSettings:
    def test_lifecycle_defaults(self):
        s = Settings(teams={"1": TeamSettings(team_id="1")})
        assert s.auto_stop_hours == 48
        # auto-delete is destructive, so it is opt-in (disabled by default).
        assert s.auto_delete_hours == 0

    def test_malformed_port_range_raises(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text('[team.1]\nhostname = "localhost"\nport_range = [50000]\n')
        with pytest.raises(ValueError, match="port_range"):
            Settings.from_toml(str(toml))

    def test_lifecycle_from_toml(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            "[lifecycle]\nauto_stop_hours = 12\nauto_delete_hours = 0\n"
            '[team.1]\nhostname = "localhost"\n'
        )
        s = Settings.from_toml(str(toml))
        assert s.auto_stop_hours == 12
        assert s.auto_delete_hours == 0

    def test_defaults(self):
        s = Settings()
        assert s.db_user == "odoo"
        assert s.routing_mode == "port"

    def test_routing_hostname_fallback_port_mode(self, tmp_path):
        # In port mode a team with no hostname inherits [routing].hostname.
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[routing]\nmode = "port"\nhostname = "shared.example.com"\n[team.1]\n'
        )
        s = Settings.from_toml(str(toml))
        assert s.get_team("1").hostname == "shared.example.com"

    def test_routing_hostname_ignored_in_traefik_mode(self, tmp_path):
        # In traefik mode the shared default is NOT inherited; a team without
        # its own hostname is left empty so validate() flags the misconfig
        # instead of silently colliding two teams on one host.
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[routing]\nmode = "traefik"\nacme_email = "a@b.co"\n'
            'hostname = "shared.example.com"\n[team.1]\n'
        )
        s = Settings.from_toml(str(toml))
        assert s.get_team("1").hostname == ""
        with pytest.raises(ValueError, match="hostname must be set"):
            s.validate()

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

    def test_validate_overlapping_port_ranges(self):
        # Two teams left on the default (identical) range would draw host ports
        # from the same pool — issue #46.
        t1 = TeamSettings(team_id="1", port_range_start=50000, port_range_end=50100)
        t2 = TeamSettings(team_id="2", port_range_start=50050, port_range_end=50150)
        s = Settings(teams={"1": t1, "2": t2})
        with pytest.raises(ValueError, match="overlapping port ranges"):
            s.validate()

    def test_validate_adjacent_port_ranges_ok(self):
        # Half-open ranges that merely touch at the boundary do not overlap.
        t1 = TeamSettings(team_id="1", port_range_start=50000, port_range_end=50100)
        t2 = TeamSettings(team_id="2", port_range_start=50100, port_range_end=50200)
        s = Settings(teams={"1": t1, "2": t2})
        s.validate()

    def test_frozen(self):
        s = Settings()
        with pytest.raises(AttributeError):
            s.routing_mode = "changed"  # type: ignore[misc]

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

    def test_routing_tls_default(self):
        # Traefik terminates TLS by default.
        assert Settings().routing_tls is True

    def test_routing_tls_from_toml(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[routing]\nmode = "traefik"\ntls = false\n'
            '[team.1]\nhostname = "dev.example.com"\n'
        )
        s = Settings.from_toml(str(toml))
        assert s.routing_tls is False

    def test_validate_traefik_tls_requires_acme_email(self):
        # tls = true (default) still requires an ACME e-mail.
        team = TeamSettings(team_id="1", hostname="dev.example.com")
        s = Settings(routing_mode="traefik", acme_email="", teams={"1": team})
        with pytest.raises(ValueError, match="acme_email"):
            s.validate()

    def test_validate_traefik_no_tls_acme_email_optional(self):
        # Behind a TLS-terminating upstream (tls = false) ACME is unused, so
        # acme_email is not required.
        team = TeamSettings(team_id="1", hostname="dev.example.com")
        s = Settings(
            routing_mode="traefik", routing_tls=False, acme_email="", teams={"1": team}
        )
        s.validate()


class TestOAuthSettings:
    def _team(self, **kw):
        defaults = {"team_id": "1", "port_range_start": 50000, "port_range_end": 50100}
        defaults.update(kw)
        return TeamSettings(**defaults)

    def test_oauth_defaults(self):
        s = Settings()
        assert s.oauth_base_url == ""
        assert not s.oauth_enabled

    def test_oauth_enabled(self):
        s = Settings(
            oauth_base_url="https://example.com",
            teams={"1": self._team(auth_token="tok-a")},
        )
        s.validate()
        assert s.oauth_enabled

    def test_oauth_without_auth_token_raises(self):
        s = Settings(
            oauth_base_url="https://example.com",
            teams={"1": self._team()},
        )
        with pytest.raises(ValueError, match="auth_token"):
            s.validate()


class TestTeamSettings:
    def test_defaults(self):
        t = TeamSettings(team_id="1")
        assert t.team_id == "1"
        assert t.hostname == "localhost"
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


class TestQuotas:
    def test_defaults(self):
        t = TeamSettings(team_id="1")
        assert t.db_quota_gb == 50
        assert t.disk_quota_gb == 0  # off until FS quota enforcement lands

    def test_from_toml(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[team.1]\nhostname = "localhost"\ndb_quota_gb = 10\ndisk_quota_gb = 20\n'
        )
        s = Settings.from_toml(str(toml))
        assert s.teams["1"].db_quota_gb == 10
        assert s.teams["1"].disk_quota_gb == 20

    def test_negative_quota_rejected(self):
        team = TeamSettings(team_id="1", db_quota_gb=-1)
        s = Settings(teams={"1": team})
        with pytest.raises(ValueError, match="quotas must be >= 0"):
            s.validate()


class TestAgentSettings:
    def test_global_defaults(self):
        s = Settings()
        assert s.agent_image == "oduist/oduflow-coder:latest"
        assert s.agent_claude_model == ""
        assert s.agent_codex_model == ""

    def test_team_defaults_agent_off(self):
        # The agent is a hosting feature; a team must opt in explicitly.
        t = TeamSettings(team_id="1")
        assert t.agent_enabled is False
        assert t.agent_default == "claude"
        assert t.agent_env == {}

    def test_from_toml(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            "[agent]\n"
            'image = "oduist/oduflow-coder:dev"\n'
            'claude_model = "claude-sonnet-4-6"\n'
            "\n"
            '[team.1]\nhostname = "localhost"\n'
            "agent_enabled = true\n"
            'agent_default = "Codex"\n'
            "[team.1.agent_env]\n"
            'OPENAI_API_KEY = "sk-oai"\n'
            "MY_FLAG = 1\n"
        )
        s = Settings.from_toml(str(toml))
        assert s.agent_image == "oduist/oduflow-coder:dev"
        assert s.agent_claude_model == "claude-sonnet-4-6"
        assert s.agent_codex_model == ""
        team = s.teams["1"]
        assert team.agent_enabled is True
        assert team.agent_default == "codex"  # normalised to lowercase
        # Values are coerced to strings (env vars).
        assert team.agent_env == {"OPENAI_API_KEY": "sk-oai", "MY_FLAG": "1"}

    def test_missing_keys_use_defaults(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text('[team.1]\nhostname = "localhost"\n')
        s = Settings.from_toml(str(toml))
        assert s.agent_image == "oduist/oduflow-coder:latest"
        assert s.teams["1"].agent_enabled is False
        assert s.teams["1"].agent_env == {}

    def test_agent_env_must_be_table(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text('[team.1]\nhostname = "localhost"\nagent_env = "oops"\n')
        with pytest.raises(ValueError, match="agent_env must be a table"):
            Settings.from_toml(str(toml))


class TestExtraRoutes:
    def _traefik_toml(self, tmp_path, body):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[routing]\nmode = "traefik"\nacme_email = "a@b.co"\n'
            '[team.1]\nhostname = "dev.example.com"\n' + body
        )
        return toml

    def test_parse_route(self, tmp_path):
        toml = self._traefik_toml(
            tmp_path,
            '[route.legacy-api]\nhost = "api.example.com"\nurl = "http://127.0.0.1:3000"\n',
        )
        s = Settings.from_toml(str(toml))
        s.validate()
        assert len(s.extra_routes) == 1
        route = s.extra_routes[0]
        assert route.name == "legacy-api"
        assert route.host == "api.example.com"
        assert route.url == "http://127.0.0.1:3000"

    def test_host_scheme_is_stripped(self, tmp_path):
        toml = self._traefik_toml(
            tmp_path,
            '[route.r]\nhost = "https://api.example.com/"\nurl = "http://10.0.0.5:80"\n',
        )
        s = Settings.from_toml(str(toml))
        assert s.extra_routes[0].host == "api.example.com"

    def test_route_requires_traefik_mode(self, tmp_path):
        toml = tmp_path / "oduflow.toml"
        toml.write_text(
            '[routing]\nmode = "port"\n[team.1]\nhostname = "localhost"\n'
            '[route.r]\nhost = "api.example.com"\nurl = "http://127.0.0.1:3000"\n'
        )
        with pytest.raises(ValueError, match="require routing.mode = 'traefik'"):
            Settings.from_toml(str(toml)).validate()

    def test_route_empty_url_rejected(self, tmp_path):
        toml = self._traefik_toml(tmp_path, '[route.r]\nhost = "api.example.com"\n')
        with pytest.raises(ValueError, match="url must be set"):
            Settings.from_toml(str(toml)).validate()

    def test_route_bad_url_scheme_rejected(self, tmp_path):
        toml = self._traefik_toml(
            tmp_path,
            '[route.r]\nhost = "api.example.com"\nurl = "ftp://x/1"\n',
        )
        with pytest.raises(ValueError, match="url must start with http"):
            Settings.from_toml(str(toml)).validate()

    def test_route_host_collision_with_team_rejected(self, tmp_path):
        toml = self._traefik_toml(
            tmp_path,
            '[route.r]\nhost = "dev.example.com"\nurl = "http://127.0.0.1:3000"\n',
        )
        with pytest.raises(ValueError, match="collides"):
            Settings.from_toml(str(toml)).validate()

    def test_route_host_collision_between_routes_rejected(self, tmp_path):
        toml = self._traefik_toml(
            tmp_path,
            '[route.a]\nhost = "api.example.com"\nurl = "http://127.0.0.1:3000"\n'
            '[route.b]\nhost = "api.example.com"\nurl = "http://127.0.0.1:4000"\n',
        )
        with pytest.raises(ValueError, match="collides"):
            Settings.from_toml(str(toml)).validate()
