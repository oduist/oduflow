"""Tests for oduflow.telemetry — no network or Docker needed."""

from __future__ import annotations

import json
from unittest.mock import patch

from oduflow.telemetry import (
    _get_instance_id,
    _send_event,
    record_env_created,
    record_startup,
)

# ---------------------------------------------------------------------------
# _get_instance_id
# ---------------------------------------------------------------------------


class TestGetInstanceId:
    def test_creates_file_on_first_run(self, tmp_path):
        instance_id, is_first = _get_instance_id(str(tmp_path))
        assert is_first is True
        assert len(instance_id) == 36  # UUID format
        # File should exist now
        assert (tmp_path / ".instance_id").read_text() == instance_id

    def test_reads_existing_file(self, tmp_path):
        (tmp_path / ".instance_id").write_text("existing-uuid")
        instance_id, is_first = _get_instance_id(str(tmp_path))
        assert instance_id == "existing-uuid"
        assert is_first is False

    def test_handles_unwritable_dir(self):
        instance_id, is_first = _get_instance_id("/nonexistent/deeply/nested/path")
        assert instance_id == ""
        assert is_first is False


# ---------------------------------------------------------------------------
# _send_event
# ---------------------------------------------------------------------------


class TestSendEvent:
    @patch("oduflow.telemetry.urlopen")
    def test_posts_correct_json(self, mock_urlopen):
        _send_event("first_run", "1.0.0", "test-uuid")
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        assert body == {
            "event": "first_run",
            "version": "1.0.0",
            "instance_id": "test-uuid",
        }
        assert req.get_header("Content-type") == "application/json"
        # Branded UA so edge bot rules don't block the default urllib signature.
        assert req.get_header("User-agent") == "oduflow/1.0.0"

    @patch("oduflow.telemetry.urlopen", side_effect=OSError("connection refused"))
    def test_swallows_network_errors(self, mock_urlopen):
        # Should not raise
        _send_event("first_run", "1.0.0", "test-uuid")


# ---------------------------------------------------------------------------
# record_startup
# ---------------------------------------------------------------------------


class TestRecordStartup:
    def test_returns_empty_when_disabled(self, tmp_path):
        result = record_startup(str(tmp_path), "1.0.0", disable_telemetry=True)
        assert result == ""
        assert not (tmp_path / ".instance_id").exists()

    @patch("oduflow.telemetry._fire_and_forget")
    def test_sends_first_run_on_new_instance(self, mock_fire, tmp_path):
        result = record_startup(str(tmp_path), "1.0.0", disable_telemetry=False)
        assert len(result) == 36
        mock_fire.assert_called_once_with("first_run", "1.0.0", result)

    @patch("oduflow.telemetry._fire_and_forget")
    def test_does_not_send_on_existing_instance(self, mock_fire, tmp_path):
        (tmp_path / ".instance_id").write_text("existing-uuid")
        result = record_startup(str(tmp_path), "1.0.0", disable_telemetry=False)
        assert result == "existing-uuid"
        mock_fire.assert_not_called()


# ---------------------------------------------------------------------------
# record_env_created
# ---------------------------------------------------------------------------


class TestRecordEnvCreated:
    @patch("oduflow.telemetry._fire_and_forget")
    def test_sends_event(self, mock_fire):
        record_env_created("test-uuid", "1.0.0", disable_telemetry=False)
        mock_fire.assert_called_once_with("env_created", "1.0.0", "test-uuid")

    @patch("oduflow.telemetry._fire_and_forget")
    def test_respects_disable_flag(self, mock_fire):
        record_env_created("test-uuid", "1.0.0", disable_telemetry=True)
        mock_fire.assert_not_called()

    @patch("oduflow.telemetry._fire_and_forget")
    def test_skips_empty_instance_id(self, mock_fire):
        record_env_created("", "1.0.0", disable_telemetry=False)
        mock_fire.assert_not_called()
