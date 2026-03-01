"""
Integration tests driven by fixtures.yaml.

These tests call real MCP tool functions or CLI commands against a live Docker
environment.
Run with:  pytest tests/test_integration.py -m integration -v
Skip with: pytest -m "not integration"

Heavyweight scenarios (init/destroy/create/delete env) are skipped by default.
Run them explicitly:  pytest tests/test_integration.py -m heavyweight -v
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from tool_helpers import call_cli, call_tool

FIXTURES_PATH = Path(__file__).parent / "fixtures.yaml"

MARK_MAP = {
    "heavyweight": pytest.mark.heavyweight,
}


def _load_scenarios():
    with open(FIXTURES_PATH) as f:
        return yaml.safe_load(f)["scenarios"]


SCENARIOS = _load_scenarios()


def _build_params():
    ids = []
    params = []
    for name, scenario in SCENARIOS.items():
        marks = [MARK_MAP[m] for m in scenario.get("marks", []) if m in MARK_MAP]
        ids.append(name)
        params.append(pytest.param(scenario, marks=marks, id=name))
    return params


@pytest.mark.integration
@pytest.mark.parametrize("scenario", _build_params())
def test_scenario(scenario, live_environment):
    settings, team = live_environment
    with (
        patch("oduflow.server._get_settings", return_value=settings),
        patch("oduflow.server._resolve_team", return_value=team),
    ):
        if "cli" in scenario:
            result = call_cli(
                scenario["cli"], settings=settings, **scenario.get("args", {})
            )
        else:
            result = call_tool(scenario["tool"], **scenario.get("args", {}))
    assert isinstance(result, str), f"Expected str, got {type(result)}"
    for expected in scenario.get("expect_contains", []):
        assert expected in result, f"Expected '{expected}' in result.\nGot: {result}"
