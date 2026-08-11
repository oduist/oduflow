import pytest

from oduflow import agent_config
from oduflow.settings import TeamSettings


def _team(default="claude") -> TeamSettings:
    return TeamSettings(team_id="1", agent_default=default)


def test_effective_uses_team_config():
    assert agent_config.effective_agent_default(_team("codex")) == "codex"
    assert agent_config.effective_agent_default(_team("claude")) == "claude"
    assert agent_config.effective_agent_default(_team("opencode")) == "opencode"


def test_effective_falls_back_on_invalid_config():
    assert agent_config.effective_agent_default(_team("nonsense")) == "claude"
    assert agent_config.effective_agent_default(_team("")) == "claude"


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("codex", "codex"),
        (" Codex ", "codex"),
        ("CODEX", "codex"),
        ("opencode", "opencode"),
        (" OpenCode ", "opencode"),
    ],
)
def test_resolve_uses_valid_request(requested, expected):
    assert agent_config.resolve_agent_type(requested, _team("claude")) == expected


@pytest.mark.parametrize("requested", [None, "", "  ", "gpt", "claude-code"])
def test_resolve_falls_back_to_team_default(requested):
    # An absent/invalid request resolves to the team default, not a hardcoded one.
    assert agent_config.resolve_agent_type(requested, _team("codex")) == "codex"


def test_resolve_always_returns_valid_agent():
    # Even a bogus request against a bogus config lands on the fallback.
    assert (
        agent_config.resolve_agent_type("bogus", _team("also-bogus"))
        == agent_config.FALLBACK_AGENT
    )
