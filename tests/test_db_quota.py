from unittest.mock import patch

import pytest

from oduflow.docker_ops.system_ops import check_db_quota, get_team_db_usage_bytes
from oduflow.errors import PrerequisiteNotMetError
from oduflow.settings import Settings, TeamSettings

GB = 1024**3

# pg_database rows as psql -tAc returns them: "datname|size" per line.
_ROWS = "\n".join(
    [
        f"postgres|{10 * 1024}",
        f"oduflow_1_main|{2 * GB}",
        f"oduflow_1_feature-x|{1 * GB}",
        f"oduflow_template_1_prod|{4 * GB}",
        f"oduflow_service_1_events|{1 * GB}",
        f"oduflow_2_main|{30 * GB}",  # another team: not counted
        f"oduflow_template_2_prod|{30 * GB}",
    ]
)


def _team(quota_gb: int = 0) -> TeamSettings:
    return TeamSettings(team_id="1", db_quota_gb=quota_gb)


def test_usage_sums_only_the_teams_databases():
    with (
        patch(
            "oduflow.docker_ops.system_ops._exec_sql", return_value=_ROWS
        ) as exec_sql,
        patch(
            "oduflow.service_database_credentials.list_names", return_value=["events"]
        ),
    ):
        used = get_team_db_usage_bytes(None, Settings(), _team())
    assert used == 8 * GB  # envs + template + service DB; team 2/postgres excluded
    assert exec_sql.call_count == 1  # one catalog query, no per-DB roundtrips


def test_service_databases_are_matched_by_exact_name_not_by_prefix():
    """Team ids are unvalidated, so ``oduflow_service_1_`` as a *prefix* would
    also swallow the environment databases of a team literally named
    ``service_1`` and bill them to team 1."""
    rows = "\n".join(
        [
            f"oduflow_service_1_events|{1 * GB}",  # team 1 owns this one
            f"oduflow_service_1_main|{30 * GB}",  # env DB of team "service_1"
        ]
    )
    with (
        patch("oduflow.docker_ops.system_ops._exec_sql", return_value=rows),
        patch(
            "oduflow.service_database_credentials.list_names", return_value=["events"]
        ),
    ):
        used = get_team_db_usage_bytes(None, Settings(), _team())

    assert used == 1 * GB


def test_under_quota_passes():
    with (
        patch("oduflow.docker_ops.system_ops._exec_sql", return_value=_ROWS),
        patch(
            "oduflow.service_database_credentials.list_names", return_value=["events"]
        ),
    ):
        check_db_quota(None, Settings(), _team(quota_gb=9))


def test_over_quota_raises():
    with (
        patch("oduflow.docker_ops.system_ops._exec_sql", return_value=_ROWS),
        patch(
            "oduflow.service_database_credentials.list_names", return_value=["events"]
        ),
    ):
        with pytest.raises(PrerequisiteNotMetError, match="database quota exceeded"):
            check_db_quota(None, Settings(), _team(quota_gb=8))


def test_zero_disables_without_querying():
    with patch("oduflow.docker_ops.system_ops._exec_sql") as exec_sql:
        check_db_quota(None, Settings(), _team(quota_gb=0))
    exec_sql.assert_not_called()
