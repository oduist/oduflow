from unittest.mock import MagicMock, patch

from oduflow.docker_ops import env_ops, stats
from oduflow.settings import Settings, TeamSettings


def _settings_and_team(tmp_path):
    team = TeamSettings(team_id="1", data_dir=str(tmp_path / "team_1"))
    settings = Settings(teams={"1": team})
    return settings, team


def test_list_environments_ignores_containers_removed_during_listing(tmp_path):
    settings, team = _settings_and_team(tmp_path)
    client = MagicMock()
    client.containers.list.return_value = []

    with patch.object(env_ops, "get_client", return_value=client):
        assert env_ops.list_environments(settings, team) == []

    client.containers.list.assert_called_once_with(
        all=True,
        filters={
            "label": [
                f"{settings.managed_label}=true",
                f"{settings.team_label}={team.team_id}",
            ]
        },
        ignore_removed=True,
    )


def test_container_stats_ignores_containers_removed_during_listing(tmp_path):
    settings, team = _settings_and_team(tmp_path)
    client = MagicMock()
    client.containers.list.return_value = []

    with patch.object(stats, "get_client", return_value=client):
        assert stats.get_container_stats(settings, team) == []

    client.containers.list.assert_called_once_with(
        all=True,
        filters={
            "label": [
                f"{settings.managed_label}=true",
                f"{settings.team_label}={team.team_id}",
            ]
        },
        ignore_removed=True,
    )
