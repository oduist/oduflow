"""Tests for template-provided environment variables.

A template records ``env_vars`` in its metadata; creating an environment from
it injects them into the Odoo container. Values passed at creation time merge
per key over the template's, so a caller can bump one variable without having
to restate the whole set.
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest
from fastmcp.exceptions import ToolError
from tool_helpers import call_tool as _call_tool  # noqa: E402

from oduflow.docker_ops.system_ops import (
    _source_env_metadata,
    _template_env_vars,
    update_template_metadata,
)
from oduflow.naming import normalize_env_vars
from oduflow.settings import Settings, TeamSettings


def _make_env(tmp_path):
    team = TeamSettings(
        team_id="1",
        data_dir=str(tmp_path / "team"),
        port_registry_path=str(tmp_path / "ports.json"),
    )
    settings = Settings(
        base_data_dir=str(tmp_path),
        disable_telemetry=True,
        teams={"1": team},
    )
    return settings, team


@pytest.fixture
def tool_env(tmp_path):
    """Inject per-test settings/team into the MCP server module."""
    import oduflow.server as server

    settings, team = _make_env(tmp_path)
    old = server._settings
    server._settings = settings
    with patch("oduflow.server._resolve_team", return_value=team):
        yield settings, team
    server._settings = old


def _write_template(team: TeamSettings, name: str, metadata: dict) -> None:
    path = team.get_template_metadata_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(metadata, f)


_TEMPLATE = {
    "odoo_image": "odoo:19.0",
    "repo_url": "https://github.com/x/y.git",
    "env_vars": {"WORKERS": "2", "LIMIT_TIME_CPU": "600"},
}

_CREATE_RESULT = {
    "url": "http://localhost:50000",
    "odoo_container": "oduflow-t-odoo",
    "database": "oduflow_1_t",
    "workspace": "/tmp/ws",
}


# --- normalize_env_vars ----------------------------------------------------


class TestNormalizeEnvVars:
    def test_mapping_is_kept_and_values_stringified(self):
        # JSON round-trips numbers that Docker would refuse.
        assert normalize_env_vars({"WORKERS": 2, "DEBUG": True}) == {
            "WORKERS": "2",
            "DEBUG": "True",
        }

    def test_text_form_keeps_commas_inside_values(self):
        assert normalize_env_vars("A=1,B=x,y") == {"A": "1", "B": "x,y"}

    def test_none_and_empty_names_drop_out(self):
        assert normalize_env_vars(None) == {}
        assert normalize_env_vars({"  ": "x", "OK": None}) == {"OK": ""}

    @pytest.mark.parametrize("name", ["2WORKERS", "WITH-DASH", "with space", "A.B"])
    def test_invalid_name_is_rejected(self, name):
        with pytest.raises(ValueError, match="Invalid environment variable name"):
            normalize_env_vars({name: "x"})

    def test_non_mapping_is_rejected(self):
        with pytest.raises(TypeError):
            normalize_env_vars(["A=1"])


# --- save_as_template metadata --------------------------------------------


class TestSourceEnvMetadata:
    SETTINGS = Settings(teams={})

    def _labels(self, **extra):
        return {
            self.SETTINGS.image_label: "odoo:19.0",
            self.SETTINGS.repo_label: "https://github.com/x/y.git",
            **extra,
        }

    def test_env_vars_are_carried_over_from_the_container(self):
        labels = self._labels(**{"oduflow.env_vars": json.dumps({"WORKERS": "2"})})
        assert _source_env_metadata(self.SETTINGS, labels)["env_vars"] == {
            "WORKERS": "2"
        }

    def test_env_without_vars_records_no_key(self):
        assert "env_vars" not in _source_env_metadata(self.SETTINGS, self._labels())

    def test_unreadable_label_is_skipped_not_fatal(self):
        labels = self._labels(**{"oduflow.env_vars": "{not json"})
        assert "env_vars" not in _source_env_metadata(self.SETTINGS, labels)


# --- reading env vars back off a template ---------------------------------


class TestTemplateEnvVars:
    def test_reads_and_normalizes(self):
        assert _template_env_vars({"env_vars": {"WORKERS": 2}}, "tpl") == {
            "WORKERS": "2"
        }

    def test_missing_or_empty_is_an_empty_dict(self):
        assert _template_env_vars({}, "tpl") == {}
        assert _template_env_vars({"env_vars": {}}, "tpl") == {}

    def test_hand_edited_garbage_is_dropped_with_a_warning(self, caplog):
        # A template broken by a filesystem edit must still provision.
        assert _template_env_vars({"env_vars": {"2BAD": "x"}}, "tpl") == {}
        assert "Ignoring invalid env_vars" in caplog.text


# --- create_environment (MCP tool) ----------------------------------------


class TestCreateFromTemplate:
    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_template_env_vars_reach_the_container(
        self, mock_create, tool_env, tmp_path
    ):
        _settings, team = tool_env
        _write_template(team, "tpl", _TEMPLATE)
        mock_create.return_value = dict(_CREATE_RESULT)

        _call_tool("create_environment", branch="t", template_name="tpl")

        assert mock_create.call_args.kwargs["env_vars"] == {
            "WORKERS": "2",
            "LIMIT_TIME_CPU": "600",
        }

    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_argument_merges_per_key_over_the_template(
        self, mock_create, tool_env, tmp_path
    ):
        _settings, team = tool_env
        _write_template(team, "tpl", _TEMPLATE)
        mock_create.return_value = dict(_CREATE_RESULT)

        _call_tool(
            "create_environment",
            branch="t",
            template_name="tpl",
            env_vars="WORKERS=8\nEXTRA=on",
        )

        # WORKERS overridden, LIMIT_TIME_CPU inherited, EXTRA added.
        assert mock_create.call_args.kwargs["env_vars"] == {
            "WORKERS": "8",
            "LIMIT_TIME_CPU": "600",
            "EXTRA": "on",
        }

    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_no_template_env_vars_leaves_the_argument_alone(
        self, mock_create, tool_env, tmp_path
    ):
        _settings, team = tool_env
        _write_template(team, "tpl", {**_TEMPLATE, "env_vars": {}})
        mock_create.return_value = dict(_CREATE_RESULT)

        _call_tool(
            "create_environment", branch="t", template_name="tpl", env_vars="A=1"
        )

        assert mock_create.call_args.kwargs["env_vars"] == {"A": "1"}

    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_invalid_argument_name_is_reported(self, mock_create, tool_env, tmp_path):
        _settings, team = tool_env
        _write_template(team, "tpl", _TEMPLATE)

        with pytest.raises(ToolError, match="Invalid environment variable name"):
            _call_tool(
                "create_environment",
                branch="t",
                template_name="tpl",
                env_vars="2BAD=x",
            )
        mock_create.assert_not_called()


# --- create from template (Web UI) ----------------------------------------


def _web_app(settings):
    from starlette.applications import Starlette

    from oduflow.locking import LockManager
    from oduflow.web_ui import mount_web_ui

    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return app


class TestWebUICreateFromTemplate:
    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_request_env_vars_merge_over_the_template(self, mock_create, tmp_path):
        from starlette.testclient import TestClient

        settings, team = _make_env(tmp_path)
        _write_template(team, "tpl", _TEMPLATE)
        mock_create.return_value = dict(_CREATE_RESULT)

        client = TestClient(_web_app(settings))
        resp = client.post(
            "/api/environments/create",
            json={
                "env_name": "t",
                "template_name": "tpl",
                "env_vars": {"WORKERS": "8"},
            },
        )

        assert resp.status_code == 200
        assert mock_create.call_args.kwargs["env_vars"] == {
            "WORKERS": "8",
            "LIMIT_TIME_CPU": "600",
        }

    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_template_env_vars_apply_without_a_request_override(
        self, mock_create, tmp_path
    ):
        from starlette.testclient import TestClient

        settings, team = _make_env(tmp_path)
        _write_template(team, "tpl", _TEMPLATE)
        mock_create.return_value = dict(_CREATE_RESULT)

        client = TestClient(_web_app(settings))
        resp = client.post(
            "/api/environments/create",
            json={"env_name": "t", "template_name": "tpl"},
        )

        assert resp.status_code == 200
        assert mock_create.call_args.kwargs["env_vars"] == {
            "WORKERS": "2",
            "LIMIT_TIME_CPU": "600",
        }

    @patch("oduflow.docker_ops.env_ops.create_environment")
    def test_without_a_template_env_vars_stay_none(self, mock_create, tmp_path):
        from starlette.testclient import TestClient

        settings, _team = _make_env(tmp_path)
        mock_create.return_value = dict(_CREATE_RESULT)

        client = TestClient(_web_app(settings))
        resp = client.post(
            "/api/environments/create",
            json={
                "env_name": "t",
                "template_name": "none",
                "repo_url": "https://github.com/x/y.git",
                "odoo_image": "odoo:19.0",
            },
        )

        assert resp.status_code == 200
        assert mock_create.call_args.kwargs["env_vars"] is None


# --- editing a template's env vars ----------------------------------------


class TestUpdateTemplateMetadata:
    def _template(self, tmp_path, metadata):
        _settings, team = _make_env(tmp_path)
        _write_template(team, "tpl", metadata)
        return team

    def _revision(self, team):
        from oduflow.docker_ops.system_ops import get_template_metadata

        return get_template_metadata(team, "tpl")["revision"]

    def test_values_are_stringified_on_save(self, tmp_path):
        team = self._template(tmp_path, _TEMPLATE)
        result = update_template_metadata(
            team,
            "tpl",
            json.dumps({**_TEMPLATE, "env_vars": {"WORKERS": 4}}),
            self._revision(team),
        )
        assert json.loads(result["content"])["env_vars"] == {"WORKERS": "4"}

    def test_invalid_name_is_rejected_while_the_editor_is_open(self, tmp_path):
        team = self._template(tmp_path, _TEMPLATE)
        with pytest.raises(ValueError, match="Invalid environment variable name"):
            update_template_metadata(
                team,
                "tpl",
                json.dumps({**_TEMPLATE, "env_vars": {"BAD NAME": "x"}}),
                self._revision(team),
            )
        # The file on disk is untouched, so the editor can be corrected and retried.
        with open(team.get_template_metadata_path("tpl")) as f:
            assert json.load(f)["env_vars"] == _TEMPLATE["env_vars"]
