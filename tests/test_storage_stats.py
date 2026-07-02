import os
from unittest.mock import patch

from oduflow.docker_ops.stats import (
    read_storage_cache,
    refresh_env_storage,
    refresh_team_storage,
)
from oduflow.settings import Settings, TeamSettings


def _team(tmp_path) -> TeamSettings:
    return TeamSettings(team_id="1", data_dir=str(tmp_path / "team_1"))


def _make_workspace(team: TeamSettings, env: str, overlay: bool) -> str:
    ws = os.path.join(team.workspaces_dir, env)
    os.makedirs(os.path.join(ws, "repo"))
    with open(os.path.join(ws, "repo", "module.py"), "w") as f:
        f.write("x" * 1000)
    merged = os.path.join(ws, "filestore")
    os.makedirs(merged)
    with open(os.path.join(merged, "blob"), "w") as f:
        f.write("y" * 50_000)
    if overlay:
        upper = os.path.join(ws, "filestore_upper")
        os.makedirs(upper)
        with open(os.path.join(upper, "own-blob"), "w") as f:
            f.write("z" * 2000)
    return ws


def test_read_cache_missing(tmp_path):
    assert read_storage_cache(_team(tmp_path)) == {"envs": {}, "team": None}


@patch("oduflow.docker_ops.stats.get_client", return_value=None)
@patch("oduflow.docker_ops.system_ops._exec_sql", return_value="12345")
def test_refresh_env_counts_upper_not_merged(mock_sql, mock_client, tmp_path):
    team = _team(tmp_path)
    _make_workspace(team, "main", overlay=True)

    entry = refresh_env_storage(Settings(), team, "main")

    assert entry["db_bytes"] == 12345
    # repo (1000) + upper (2000); the merged overlay view (50000) holds the
    # template's lower layer and duplicates upper — excluded.
    assert entry["disk_bytes"] == 3000
    assert entry["computed_at"]
    # Persisted and readable back.
    assert read_storage_cache(team)["envs"]["main"] == entry


@patch("oduflow.docker_ops.stats.get_client", return_value=None)
@patch("oduflow.docker_ops.system_ops._exec_sql", return_value="500")
def test_refresh_env_plain_filestore_is_counted(mock_sql, mock_client, tmp_path):
    team = _team(tmp_path)
    _make_workspace(team, "plain", overlay=False)

    entry = refresh_env_storage(Settings(), team, "plain")

    # No overlay upper dir → the filestore is a plain copy owned by the env.
    assert entry["disk_bytes"] == 51_000


@patch("oduflow.docker_ops.stats.get_client", return_value=None)
@patch("oduflow.docker_ops.system_ops.get_team_db_usage_bytes", return_value=777)
@patch("oduflow.docker_ops.system_ops._exec_sql", return_value="100")
@patch("oduflow.docker_ops.env_ops.list_environments")
def test_refresh_team_totals(mock_list, mock_sql, mock_team_db, mock_client, tmp_path):
    team = _team(tmp_path)
    _make_workspace(team, "main", overlay=True)
    os.makedirs(os.path.join(team.data_dir, "templates", "prod"))
    with open(os.path.join(team.data_dir, "templates", "prod", "dump.sql"), "w") as f:
        f.write("d" * 4000)
    mock_list.return_value = [{"env_name": "main"}]

    cache = refresh_team_storage(Settings(), team)

    assert cache["envs"]["main"]["db_bytes"] == 100
    assert cache["envs"]["main"]["disk_bytes"] == 3000
    assert cache["team"]["db_bytes"] == 777
    # Whole team dir minus the merged overlay view: workspace (3000) +
    # template dump (4000) + the storage cache file itself is written after
    # measuring, so it is not counted.
    assert cache["team"]["disk_bytes"] == 7000
    assert read_storage_cache(team)["team"] == cache["team"]
