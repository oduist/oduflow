"""
Integration tests driven by fixtures.yaml.

These tests call real MCP tool functions against a live Docker environment.
Run with:  pytest tests/test_integration.py -m integration -v
Skip with: pytest -m "not integration"
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tool_helpers import call_tool

FIXTURES_PATH = Path(__file__).parent / "fixtures.yaml"


def _load_scenarios():
    with open(FIXTURES_PATH) as f:
        return yaml.safe_load(f)["scenarios"]


SCENARIOS = _load_scenarios()


def _scenario_ids():
    return list(SCENARIOS.keys())


def _scenario_params():
    return list(SCENARIOS.values())


@pytest.mark.integration
@pytest.mark.parametrize("scenario", _scenario_params(), ids=_scenario_ids())
def test_scenario(scenario):
    result = call_tool(scenario["tool"], **scenario.get("args", {}))
    assert isinstance(result, str), f"Expected str, got {type(result)}"
    for expected in scenario.get("expect_contains", []):
        assert expected in result, (
            f"Expected '{expected}' in result.\nGot: {result}"
        )
