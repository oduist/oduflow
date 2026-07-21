import json
import os
import stat

from oduflow import agent_sessions
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import _acp_adapter_cmd, _codex_cli_cmd, _wire_codex_acp_mcp


def _team(tmp_path) -> TeamSettings:
    return TeamSettings(team_id="1", data_dir=str(tmp_path))


def test_get_none_when_unset(tmp_path):
    assert agent_sessions.get_session(_team(tmp_path), "feature/x", "claude") is None


def test_set_get_roundtrip_per_branch_and_agent(tmp_path):
    t = _team(tmp_path)
    agent_sessions.set_session(t, "feature/x", "claude", "sid-1")
    agent_sessions.set_session(t, "feature/x", "codex", "sid-2")
    agent_sessions.set_session(t, "main", "claude", "sid-3")
    assert agent_sessions.get_session(t, "feature/x", "claude") == "sid-1"
    assert agent_sessions.get_session(t, "feature/x", "codex") == "sid-2"
    assert agent_sessions.get_session(t, "main", "claude") == "sid-3"


def test_set_overwrites(tmp_path):
    t = _team(tmp_path)
    agent_sessions.set_session(t, "b", "claude", "old")
    agent_sessions.set_session(t, "b", "claude", "new")
    assert agent_sessions.get_session(t, "b", "claude") == "new"


def test_clear_one_agent_keeps_the_other(tmp_path):
    t = _team(tmp_path)
    agent_sessions.set_session(t, "b", "claude", "c1")
    agent_sessions.set_session(t, "b", "codex", "c2")
    agent_sessions.clear_session(t, "b", "claude")
    assert agent_sessions.get_session(t, "b", "claude") is None
    assert agent_sessions.get_session(t, "b", "codex") == "c2"


def test_clear_whole_branch(tmp_path):
    t = _team(tmp_path)
    agent_sessions.set_session(t, "b", "claude", "c1")
    agent_sessions.set_session(t, "b", "codex", "c2")
    agent_sessions.clear_session(t, "b")
    assert agent_sessions.get_session(t, "b", "claude") is None
    assert agent_sessions.get_session(t, "b", "codex") is None


def test_clear_missing_is_noop(tmp_path):
    t = _team(tmp_path)
    agent_sessions.clear_session(t, "nope")
    agent_sessions.clear_session(t, "nope", "claude")


def test_per_team_isolation(tmp_path):
    t1 = TeamSettings(team_id="1", data_dir=str(tmp_path / "t1"))
    t2 = TeamSettings(team_id="2", data_dir=str(tmp_path / "t2"))
    agent_sessions.set_session(t1, "b", "claude", "sid-1")
    assert agent_sessions.get_session(t2, "b", "claude") is None


def test_file_permissions_are_restrictive(tmp_path):
    t = _team(tmp_path)
    agent_sessions.set_session(t, "b", "claude", "c1")
    mode = stat.S_IMODE(os.stat(agent_sessions._path(t)).st_mode)
    assert mode == 0o600


def test_acp_adapter_cmd():
    assert _acp_adapter_cmd("claude") == ["claude-code-acp"]
    assert _acp_adapter_cmd("codex") == ["codex-acp"]
    # Unknown agent falls back to Claude (the configured default agent).
    assert _acp_adapter_cmd("something-else") == ["claude-code-acp"]


def test_codex_cli_cmd_uses_docker_as_the_sandbox():
    assert _codex_cli_cmd("http://scoped/mcp/env", "gpt-test") == [
        "codex",
        "--dangerously-bypass-approvals-and-sandbox",
        "-c",
        'mcp_servers.oduflow.url="http://scoped/mcp/env"',
        "-c",
        'mcp_servers.oduflow.bearer_token_env_var="ODUFLOW_MCP_TOKEN"',
        "--model",
        "gpt-test",
    ]


def test_wire_codex_acp_mcp_adds_scoped_server_and_preserves_others():
    frame = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/new",
            "params": {
                "cwd": "/workspace/x",
                "mcpServers": [
                    {"name": "custom", "command": "custom-mcp", "args": [], "env": []},
                    {"name": "agent_browser", "command": "wrong-browser"},
                    {
                        "type": "http",
                        "name": "oduflow",
                        "url": "http://wrong",
                        "headers": [],
                    },
                ],
            },
        }
    )
    wired = json.loads(
        _wire_codex_acp_mcp(frame, "http://scoped/mcp/env", "scoped-token")
    )
    assert wired["params"]["mcpServers"] == [
        {"name": "custom", "command": "custom-mcp", "args": [], "env": []},
        {
            "name": "agent_browser",
            "command": "agent-browser",
            "args": ["mcp", "--tools", "all"],
            "env": [],
        },
        {
            "type": "http",
            "name": "oduflow",
            "url": "http://scoped/mcp/env",
            "headers": [{"name": "Authorization", "value": "Bearer scoped-token"}],
        },
    ]


def test_wire_codex_acp_mcp_handles_load_but_not_other_frames():
    load = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/load",
            "params": {"sessionId": "sid", "cwd": "/workspace", "mcpServers": []},
        }
    )
    assert (
        json.loads(_wire_codex_acp_mcp(load, "http://scoped", "token"))["params"][
            "mcpServers"
        ][0]["name"]
        == "agent_browser"
    )
    load_servers = json.loads(_wire_codex_acp_mcp(load, "http://scoped", "token"))[
        "params"
    ]["mcpServers"]
    assert [server["name"] for server in load_servers] == [
        "agent_browser",
        "oduflow",
    ]

    prompt = '{"jsonrpc":"2.0","method":"session/prompt","params":{}}'
    assert _wire_codex_acp_mcp(prompt, "http://scoped", "token") == prompt
    no_token = json.loads(_wire_codex_acp_mcp(load, "http://scoped", ""))
    assert [server["name"] for server in no_token["params"]["mcpServers"]] == [
        "agent_browser"
    ]
    assert _wire_codex_acp_mcp("not-json", "http://scoped", "token") == "not-json"


def test_delete_environment_clears_chat_sessions(tmp_path, monkeypatch):
    """A deleted environment must not leave a stale session that a later env of
    the same name would resume."""
    import docker as _docker

    from oduflow.docker_ops import env_ops
    from oduflow.naming import get_workspace_path

    data = tmp_path / "data"
    data.mkdir()
    team = TeamSettings(
        team_id="1",
        data_dir=str(data),
        port_registry_path=str(data / "ports.json"),
    )
    settings = Settings(base_data_dir=str(data), teams={"1": team})

    agent_sessions.set_session(team, "feature/x", "claude", "sid-old")
    agent_sessions.set_session(team, "feature/x", "codex", "sid-old-2")
    # A second env's sessions must survive the first env's deletion.
    agent_sessions.set_session(team, "other", "claude", "keep-me")

    # Make the env look present on disk with no live container.
    os.makedirs(get_workspace_path("feature/x", team.workspaces_dir), exist_ok=True)

    class _FakeContainers:
        def get(self, name):
            raise _docker.errors.NotFound(name)

    class _FakeClient:
        containers = _FakeContainers()

    monkeypatch.setattr(env_ops, "is_protected", lambda *a, **k: False)
    monkeypatch.setattr(env_ops, "get_client", lambda: _FakeClient())
    monkeypatch.setattr(env_ops, "_agent_remove_env", lambda *a, **k: None)
    monkeypatch.setattr(env_ops, "release_port", lambda *a, **k: None)
    monkeypatch.setattr(env_ops, "_exec_sql", lambda *a, **k: None)
    monkeypatch.setattr(env_ops, "load_credentials", lambda *a, **k: {"pg_user": "x"})
    monkeypatch.setattr(env_ops, "_drop_pg_role", lambda *a, **k: None)
    monkeypatch.setattr(env_ops, "_unmount_filestore", lambda *a, **k: None)

    env_ops.delete_environment(settings, team, "feature/x")

    assert agent_sessions.get_session(team, "feature/x", "claude") is None
    assert agent_sessions.get_session(team, "feature/x", "codex") is None
    assert agent_sessions.get_session(team, "other", "claude") == "keep-me"
