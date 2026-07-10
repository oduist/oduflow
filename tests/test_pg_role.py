"""Regression test for issue #40: env DB roles must not be superuser members."""

from unittest.mock import MagicMock, patch

from oduflow.docker_ops import system_ops
from oduflow.settings import Settings, TeamSettings


def test_create_pg_role_revokes_superuser_membership():
    settings = Settings(teams={"1": TeamSettings(team_id="1")})
    client = MagicMock()

    issued: list[str] = []

    def _fake_exec(_client, _settings, sql, db=None, container_name=None):
        issued.append(sql)
        return ""

    with patch.object(system_ops, "_exec_sql", side_effect=_fake_exec):
        system_ops._create_pg_role(
            client, settings, "u_1_main", "secretpw", "oduflow_1_main"
        )

    # Must REVOKE superuser-role membership and must NOT grant it.
    assert any(
        s.strip().startswith("REVOKE") and settings.db_user in s for s in issued
    ), issued
    assert not any("GRANT" in s for s in issued), issued
