"""Unit tests for per-environment LLM usage accounting (no Docker)."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
from pathlib import Path

from oduflow import usage
from oduflow.naming import get_workspace_path, slugify_branch
from oduflow.settings import Settings, TeamSettings


def _setup(tmp_path) -> tuple[Settings, TeamSettings]:
    base = str(tmp_path)
    team = TeamSettings(team_id="1", data_dir=os.path.join(base, "team_1"))
    settings = Settings(base_data_dir=base, teams={"1": team})
    return settings, team


def _make_workspace(team: TeamSettings, env_name: str) -> str:
    ws = get_workspace_path(env_name, team.workspaces_dir)
    os.makedirs(ws, exist_ok=True)
    return ws


class TestRecordAndAggregate:
    def test_record_and_totals(self, tmp_path):
        _, team = _setup(tmp_path)
        _make_workspace(team, "feature/x")
        usage.record(
            team,
            "feature/x",
            session_id="A",
            models={"opus": {"input_tokens": 100, "output_tokens": 50}},
            duration_seconds=60,
        )
        agg = usage.get_env_usage(team, "feature/x")
        assert agg["sessions"] == 1
        assert agg["totals"]["input_tokens"] == 100
        assert agg["totals"]["output_tokens"] == 50
        assert agg["duration_seconds"] == 60
        assert "opus" in agg["models"]

    def test_idempotent_same_session(self, tmp_path):
        _, team = _setup(tmp_path)
        _make_workspace(team, "x")
        for _ in range(3):
            usage.record(
                team,
                "x",
                session_id="A",
                models={"opus": {"input_tokens": 100, "output_tokens": 50}},
                duration_seconds=90,
            )
        agg = usage.get_env_usage(team, "x")
        assert agg["sessions"] == 1
        assert agg["totals"]["input_tokens"] == 100  # not 300
        assert agg["duration_seconds"] == 90

    def test_multiple_sessions_accumulate(self, tmp_path):
        _, team = _setup(tmp_path)
        _make_workspace(team, "x")
        usage.record(
            team,
            "x",
            session_id="A",
            models={"opus": {"input_tokens": 100}},
            duration_seconds=60,
        )
        usage.record(
            team,
            "x",
            session_id="B",
            models={"opus": {"input_tokens": 1, "output_tokens": 2}},
            duration_seconds=30,
        )
        agg = usage.get_env_usage(team, "x")
        assert agg["sessions"] == 2
        assert agg["totals"]["input_tokens"] == 101
        assert agg["totals"]["output_tokens"] == 2
        assert agg["duration_seconds"] == 90

    def test_model_switch_within_session(self, tmp_path):
        _, team = _setup(tmp_path)
        _make_workspace(team, "x")
        usage.record(
            team,
            "x",
            session_id="A",
            models={"opus": {"input_tokens": 10}, "haiku": {"input_tokens": 5}},
        )
        agg = usage.get_env_usage(team, "x")
        assert set(agg["models"]) == {"opus", "haiku"}
        assert agg["totals"]["input_tokens"] == 15

    def test_no_session_id_is_noop(self, tmp_path):
        _, team = _setup(tmp_path)
        _make_workspace(team, "x")
        usage.record(team, "x", session_id="", models={"opus": {"input_tokens": 9}})
        assert usage.get_env_usage(team, "x")["sessions"] == 0

    def test_missing_workspace_skipped(self, tmp_path):
        _, team = _setup(tmp_path)
        usage.record(
            team, "ghost", session_id="A", models={"opus": {"input_tokens": 9}}
        )
        assert usage.get_env_usage(team, "ghost")["sessions"] == 0

    def test_empty_env_usage(self, tmp_path):
        _, team = _setup(tmp_path)
        _make_workspace(team, "x")
        agg = usage.get_env_usage(team, "x")
        assert agg["sessions"] == 0
        assert agg["totals"] == {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }


class TestArchive:
    def test_archive_on_delete(self, tmp_path):
        _, team = _setup(tmp_path)
        _make_workspace(team, "x")
        usage.record(
            team,
            "x",
            session_id="A",
            models={"opus": {"input_tokens": 100, "output_tokens": 50}},
            duration_seconds=60,
        )
        usage.archive_on_delete(team, "x")
        arch = usage.get_archive(team)
        assert "x" in arch
        assert arch["x"]["sessions"] == 1
        assert arch["x"]["models"]["opus"]["input_tokens"] == 100
        assert "archived_at" in arch["x"]

    def test_archive_accumulates_across_recreate(self, tmp_path):
        _, team = _setup(tmp_path)
        ws = _make_workspace(team, "x")
        usage.record(
            team,
            "x",
            session_id="A",
            models={"opus": {"input_tokens": 100}},
            duration_seconds=60,
        )
        usage.archive_on_delete(team, "x")
        shutil.rmtree(ws)  # simulate delete_environment wiping the workspace

        _make_workspace(team, "x")  # env recreated under the same name
        usage.record(
            team,
            "x",
            session_id="B",
            models={"opus": {"input_tokens": 5}},
            duration_seconds=10,
        )
        usage.archive_on_delete(team, "x")

        arch = usage.get_archive(team)
        assert arch["x"]["models"]["opus"]["input_tokens"] == 105
        assert arch["x"]["sessions"] == 2
        assert arch["x"]["duration_seconds"] == 70

    def test_archive_empty_is_noop(self, tmp_path):
        _, team = _setup(tmp_path)
        _make_workspace(team, "x")
        usage.archive_on_delete(team, "x")
        assert usage.get_archive(team) == {}


class TestTokens:
    def test_register_and_resolve(self, tmp_path):
        settings, team = _setup(tmp_path)
        uid = usage.register_token(settings, team, "feature/x")
        assert uid
        resolved = usage.resolve_token(settings, uid)
        assert resolved is not None
        rteam, renv = resolved
        assert rteam.team_id == "1"
        assert renv == "feature/x"

    def test_get_token(self, tmp_path):
        settings, team = _setup(tmp_path)
        uid = usage.register_token(settings, team, "x")
        assert usage.get_token(settings, team, "x") == uid
        assert usage.get_token(settings, team, "other") == ""

    def test_register_replaces_stale_token(self, tmp_path):
        settings, team = _setup(tmp_path)
        uid1 = usage.register_token(settings, team, "x")
        uid2 = usage.register_token(settings, team, "x")
        assert uid1 != uid2
        assert usage.resolve_token(settings, uid1) is None
        assert usage.resolve_token(settings, uid2) is not None

    def test_get_or_create_is_stable(self, tmp_path):
        settings, team = _setup(tmp_path)
        uid1 = usage.get_or_create_token(settings, team, "x")
        uid2 = usage.get_or_create_token(settings, team, "x")
        assert uid1 == uid2

    def test_revoke(self, tmp_path):
        settings, team = _setup(tmp_path)
        uid = usage.register_token(settings, team, "x")
        usage.revoke_token(settings, team, "x")
        assert usage.resolve_token(settings, uid) is None
        assert usage.get_token(settings, team, "x") == ""

    def test_resolve_garbage(self, tmp_path):
        settings, _ = _setup(tmp_path)
        assert usage.resolve_token(settings, "") is None
        assert usage.resolve_token(settings, "nope") is None


def _load_hook():
    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "oduflow"
        / "templates"
        / "hooks"
        / "oduflow_usage_hook.py"
    )
    spec = importlib.util.spec_from_file_location("oduflow_usage_hook", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestHookScript:
    def test_parse_transcript_sums_per_model_and_duration(self, tmp_path):
        hook = _load_hook()
        transcript = tmp_path / "t.jsonl"
        events = [
            {
                "type": "assistant",
                "timestamp": "2026-06-17T10:00:00Z",
                "message": {
                    "model": "claude-opus-4-8",
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cache_read_input_tokens": 10,
                        "cache_creation_input_tokens": 5,
                    },
                },
            },
            {
                "type": "user",
                "timestamp": "2026-06-17T10:01:00Z",
                "message": {"role": "user"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-06-17T10:05:00Z",
                "message": {
                    "model": "claude-opus-4-8",
                    "usage": {"input_tokens": 200, "output_tokens": 80},
                },
            },
        ]
        transcript.write_text("\n".join(json.dumps(e) for e in events))
        models, duration = hook.parse_transcript(str(transcript))
        opus = models["claude-opus-4-8"]
        assert opus["input_tokens"] == 300
        assert opus["output_tokens"] == 130
        assert opus["cache_read_tokens"] == 10
        assert opus["cache_creation_tokens"] == 5
        assert duration == 300.0  # 10:00 → 10:05

    def test_parse_transcript_missing_file(self):
        hook = _load_hook()
        assert hook.parse_transcript("/no/such/file.jsonl") == ({}, 0.0)

    def test_slugify_matches_oduflow(self):
        hook = _load_hook()
        for branch in ["feature/x", "Main", "a/b/c", "weird@name!"]:
            assert hook.slugify(branch) == slugify_branch(branch)

    def test_resolve_uid_by_branch_then_slug(self, monkeypatch):
        monkeypatch.delenv("ODUFLOW_ENV", raising=False)
        hook = _load_hook()
        tokens = {"feature/x": "raw-uid", "feature-y": "slug-uid"}
        assert hook.resolve_uid(tokens, "feature/x") == "raw-uid"
        assert hook.resolve_uid(tokens, "feature/y") == "slug-uid"
        assert hook.resolve_uid(tokens, "missing") == ""
