from unittest.mock import MagicMock, patch

import pytest

from oduflow import pg_hba
from oduflow.docker_ops import system_ops
from oduflow.errors import PrerequisiteNotMetError
from oduflow.settings import Settings, TeamSettings


def test_normalize_cidrs_is_canonical_stable_and_deduplicated():
    assert pg_hba.normalize_cidrs(
        ["fd00::1/64", "172.21.4.8/16", "172.20.0.0/16", "fd00::/64"]
    ) == ["172.20.0.0/16", "172.21.0.0/16", "fd00::/64"]


def test_reconcile_prepends_managed_rules_before_existing_reject():
    current = "# standard rules\nhost all all all reject\n"

    result = pg_hba.reconcile_managed_block(current, ["172.20.0.0/16"], "scram-sha-256")

    assert result.startswith(pg_hba.BEGIN_MARKER)
    assert "host all all 172.20.0.0/16 scram-sha-256" in result
    assert result.index(pg_hba.END_MARKER) < result.index("host all all all reject")


def test_reconcile_replaces_only_the_managed_block():
    current = pg_hba.reconcile_managed_block(
        "local all all trust\n", ["172.20.0.0/16"], "md5"
    )

    result = pg_hba.reconcile_managed_block(
        current, ["172.22.0.0/16", "fd00::/64"], "scram-sha-256"
    )

    assert "172.20.0.0/16" not in result
    assert "host all all 172.22.0.0/16 scram-sha-256" in result
    assert "host all all fd00::/64 scram-sha-256" in result
    assert result.endswith("local all all trust\n")


def test_reconcile_is_idempotent():
    first = pg_hba.reconcile_managed_block(
        "local all all trust\n", ["172.20.0.0/16"], "scram-sha-256"
    )
    assert (
        pg_hba.reconcile_managed_block(first, ["172.20.0.3/16"], "scram-sha-256")
        == first
    )


@pytest.mark.parametrize(
    "content",
    [
        f"{pg_hba.BEGIN_MARKER}\nhost all all all md5\n",
        f"{pg_hba.END_MARKER}\n{pg_hba.BEGIN_MARKER}\n",
        f"{pg_hba.BEGIN_MARKER}\n{pg_hba.END_MARKER}\n{pg_hba.BEGIN_MARKER}\n{pg_hba.END_MARKER}\n",
    ],
)
def test_reconcile_refuses_malformed_managed_blocks(content):
    with pytest.raises(ValueError, match="Malformed"):
        pg_hba.reconcile_managed_block(content, ["172.20.0.0/16"], "md5")


def test_render_refuses_insecure_or_unknown_auth_methods():
    with pytest.raises(ValueError, match="Unsupported"):
        pg_hba.render_managed_block(["172.20.0.0/16"], "trust")


def test_render_refuses_missing_or_invalid_networks():
    with pytest.raises(ValueError, match="At least one"):
        pg_hba.render_managed_block([], "scram-sha-256")
    with pytest.raises(ValueError, match="does not appear"):
        pg_hba.render_managed_block(["not-a-network"], "scram-sha-256")


def test_managed_pg_network_cidrs_come_from_real_docker_ipam():
    settings = Settings(
        teams={
            "1": TeamSettings(team_id="1"),
            "2": TeamSettings(team_id="2"),
        }
    )
    networks = {
        "oduflow-net": {"IPAM": {"Config": [{"Subnet": "172.19.0.0/16"}]}},
        "oduflow-1-net": {"IPAM": {"Config": [{"Subnet": "172.20.1.2/16"}]}},
        "oduflow-2-net": {"IPAM": {"Config": [{"Subnet": "fd00:2::/64"}]}},
    }
    client = MagicMock()
    client.networks.get.side_effect = lambda name: MagicMock(attrs=networks[name])

    assert system_ops._managed_pg_network_cidrs(client, settings) == [
        "172.19.0.0/16",
        "172.20.0.0/16",
        "fd00:2::/64",
    ]


def _reconcile_fixture(current: str):
    settings = Settings(teams={"1": TeamSettings(team_id="1")})
    client = MagicMock()
    container = MagicMock()
    container.exec_run.return_value = (0, current.encode())
    client.containers.get.return_value = container
    return settings, client, container


def test_reconcile_pg_hba_installs_reloads_and_validates():
    current = "local all all trust\n"
    settings, client, container = _reconcile_fixture(current)
    installed: list[str] = []

    def exec_sql(_client, _settings, sql, **_kwargs):
        if sql.startswith("SHOW hba_file"):
            return "/var/lib/postgresql/data/pg_hba.conf"
        if "pg_authid" in sql:
            return "f"
        if sql.startswith("SHOW password_encryption"):
            return "scram-sha-256"
        if "pg_hba_file_rules" in sql:
            return ""
        if "pg_reload_conf" in sql:
            return "t"
        raise AssertionError(sql)

    with (
        patch.object(
            system_ops,
            "_managed_pg_network_cidrs",
            return_value=["172.20.0.0/16"],
        ),
        patch.object(system_ops, "_exec_sql", side_effect=exec_sql),
        patch.object(
            system_ops,
            "_install_pg_hba_content",
            side_effect=lambda _container, _path, content: installed.append(content),
        ),
    ):
        changed = system_ops._reconcile_pg_hba(
            client, settings, container_name=settings.shared_db_container
        )

    assert changed is True
    assert len(installed) == 1
    assert "host all all 172.20.0.0/16 scram-sha-256" in installed[0]
    container.exec_run.assert_called_once_with(
        ["cat", "/var/lib/postgresql/data/pg_hba.conf"]
    )


def test_reconcile_pg_hba_keeps_md5_for_legacy_role_verifiers():
    # A data volume initialized before PostgreSQL 14 still holds md5 verifiers
    # while password_encryption already reports scram-sha-256; a strict
    # scram-sha-256 rule would lock those roles out.
    current = "host all all all md5\n"
    settings, client, _container = _reconcile_fixture(current)
    installed: list[str] = []

    def exec_sql(_client, _settings, sql, **_kwargs):
        if sql.startswith("SHOW hba_file"):
            return "/var/lib/postgresql/data/pg_hba.conf"
        if "pg_authid" in sql:
            return "t"
        if sql.startswith("SHOW password_encryption"):
            return "scram-sha-256"
        if "pg_hba_file_rules" in sql:
            return ""
        if "pg_reload_conf" in sql:
            return "t"
        raise AssertionError(sql)

    with (
        patch.object(
            system_ops,
            "_managed_pg_network_cidrs",
            return_value=["172.20.0.0/16"],
        ),
        patch.object(system_ops, "_exec_sql", side_effect=exec_sql),
        patch.object(
            system_ops,
            "_install_pg_hba_content",
            side_effect=lambda _container, _path, content: installed.append(content),
        ),
    ):
        assert (
            system_ops._reconcile_pg_hba(
                client, settings, container_name=settings.shared_db_container
            )
            is True
        )

    assert "host all all 172.20.0.0/16 md5" in installed[0]
    assert "scram-sha-256" not in installed[0]


def test_reconcile_pg_hba_skips_an_unchanged_file():
    current = pg_hba.reconcile_managed_block(
        "local all all trust\n", ["172.20.0.0/16"], "scram-sha-256"
    )
    settings, client, _container = _reconcile_fixture(current)

    def exec_sql(_client, _settings, sql, **_kwargs):
        if sql.startswith("SHOW hba_file"):
            return "/var/lib/postgresql/data/pg_hba.conf"
        if "pg_authid" in sql:
            return "f"
        if sql.startswith("SHOW password_encryption"):
            return "scram-sha-256"
        raise AssertionError(sql)

    with (
        patch.object(
            system_ops,
            "_managed_pg_network_cidrs",
            return_value=["172.20.0.0/16"],
        ),
        patch.object(system_ops, "_exec_sql", side_effect=exec_sql),
        patch.object(system_ops, "_install_pg_hba_content") as install,
    ):
        changed = system_ops._reconcile_pg_hba(
            client, settings, container_name=settings.shared_db_container
        )

    assert changed is False
    install.assert_not_called()


def test_reconcile_pg_hba_rolls_back_when_generated_file_is_invalid():
    current = "local all all trust\n"
    settings, client, _container = _reconcile_fixture(current)
    installed: list[str] = []
    error_results = iter(["", "9: invalid authentication method"])

    def exec_sql(_client, _settings, sql, **_kwargs):
        if sql.startswith("SHOW hba_file"):
            return "/var/lib/postgresql/data/pg_hba.conf"
        if "pg_authid" in sql:
            return "f"
        if sql.startswith("SHOW password_encryption"):
            return "scram-sha-256"
        if "pg_hba_file_rules" in sql:
            return next(error_results)
        if "pg_reload_conf" in sql:
            return "t"
        raise AssertionError(sql)

    with (
        patch.object(
            system_ops,
            "_managed_pg_network_cidrs",
            return_value=["172.20.0.0/16"],
        ),
        patch.object(system_ops, "_exec_sql", side_effect=exec_sql),
        patch.object(
            system_ops,
            "_install_pg_hba_content",
            side_effect=lambda _container, _path, content: installed.append(content),
        ),
    ):
        with pytest.raises(PrerequisiteNotMetError, match="parse errors"):
            system_ops._reconcile_pg_hba(
                client, settings, container_name=settings.shared_db_container
            )

    assert len(installed) == 2
    assert installed[1] == current


def test_reconcile_pg_hba_refuses_to_touch_an_already_invalid_file():
    current = "local all all trust\n"
    settings, client, _container = _reconcile_fixture(current)

    def exec_sql(_client, _settings, sql, **_kwargs):
        if sql.startswith("SHOW hba_file"):
            return "/var/lib/postgresql/data/pg_hba.conf"
        if "pg_authid" in sql:
            return "f"
        if sql.startswith("SHOW password_encryption"):
            return "md5"
        if "pg_hba_file_rules" in sql:
            return "4: invalid connection type"
        raise AssertionError(sql)

    with (
        patch.object(
            system_ops,
            "_managed_pg_network_cidrs",
            return_value=["172.20.0.0/16"],
        ),
        patch.object(system_ops, "_exec_sql", side_effect=exec_sql),
        patch.object(system_ops, "_install_pg_hba_content") as install,
    ):
        with pytest.raises(PrerequisiteNotMetError, match="existing pg_hba"):
            system_ops._reconcile_pg_hba(
                client, settings, container_name=settings.shared_db_container
            )

    install.assert_not_called()
