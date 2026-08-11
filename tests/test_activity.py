"""Per-environment activity records that drive the reaper.

The module had no test file: it is exercised incidentally by other suites but
nothing asserted its behaviour. What matters here is that the timestamps the
reaper compares are correct — a naive timestamp read as host-local time
shifts every auto-stop/auto-delete decision by the UTC offset — and that
``mark_stopped`` does not restart the delete clock on a re-mark.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from oduflow import activity
from oduflow.settings import TeamSettings


def _team(tmp_path) -> TeamSettings:
    return TeamSettings(team_id="1", data_dir=str(tmp_path))


def _read(tmp_path) -> dict:
    with open(tmp_path / "activity.json") as f:
        return json.load(f)


class TestParseTs:
    def test_round_trips_an_aware_timestamp(self):
        moment = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)

        assert activity.parse_ts(moment.isoformat()) == moment.timestamp()

    def test_a_naive_timestamp_is_read_as_utc(self):
        # Reading it as host-local time would move every reaper deadline by
        # the machine's UTC offset.
        naive = "2026-03-01T12:00:00"
        expected = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc).timestamp()

        assert activity.parse_ts(naive) == expected

    def test_a_non_utc_offset_is_honoured(self):
        assert activity.parse_ts("2026-03-01T14:00:00+02:00") == (
            datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc).timestamp()
        )

    def test_missing_or_unparsable_values_yield_none(self):
        assert activity.parse_ts(None) is None
        assert activity.parse_ts("") is None
        assert activity.parse_ts("last tuesday") is None


class TestRecords:
    def test_touch_creates_a_record(self, tmp_path):
        activity.touch(_team(tmp_path), "main")

        assert "last_activity" in _read(tmp_path)["main"]

    def test_get_all_returns_what_was_written(self, tmp_path):
        team = _team(tmp_path)
        activity.touch(team, "main")

        assert set(activity.get_all(team)) == {"main"}

    def test_mark_stopped_records_when_and_how(self, tmp_path):
        activity.mark_stopped(_team(tmp_path), "main", by="auto")

        rec = _read(tmp_path)["main"]
        assert rec["stopped_by"] == "auto"
        assert activity.parse_ts(rec["stopped_at"]) is not None

    def test_re_marking_keeps_the_original_stop_time(self, tmp_path):
        # Otherwise every sweep would push the auto-delete deadline forward
        # and a stopped environment would never be reaped.
        team = _team(tmp_path)
        activity.mark_stopped(team, "main", by="observed")
        first = _read(tmp_path)["main"]["stopped_at"]

        activity.mark_stopped(team, "main", by="auto")

        rec = _read(tmp_path)["main"]
        assert rec["stopped_at"] == first
        assert rec["stopped_by"] == "auto"  # attribution still upgrades

    def test_mark_started_clears_the_stop_clock(self, tmp_path):
        team = _team(tmp_path)
        activity.mark_stopped(team, "main")

        activity.mark_started(team, "main")

        rec = _read(tmp_path)["main"]
        assert "stopped_at" not in rec
        assert "stopped_by" not in rec
        assert "last_activity" in rec

    def test_remove_drops_only_the_named_environment(self, tmp_path):
        team = _team(tmp_path)
        activity.touch(team, "main")
        activity.touch(team, "other")

        activity.remove(team, "main")

        assert set(_read(tmp_path)) == {"other"}

    def test_prune_drops_records_of_vanished_environments(self, tmp_path):
        team = _team(tmp_path)
        activity.touch(team, "main")
        activity.touch(team, "gone")

        activity.prune(team, {"main"})

        assert set(_read(tmp_path)) == {"main"}

    def test_prune_keeps_everything_that_still_exists(self, tmp_path):
        team = _team(tmp_path)
        activity.touch(team, "main")

        activity.prune(team, {"main", "not-yet-recorded"})

        assert set(_read(tmp_path)) == {"main"}


class TestResilience:
    def test_a_team_without_a_data_dir_is_a_no_op(self):
        team = TeamSettings(team_id="1", data_dir="")

        activity.touch(team, "main")  # must not raise

        assert activity.get_all(team) == {}

    def test_a_corrupt_file_reads_as_empty(self, tmp_path):
        (tmp_path / "activity.json").write_text("{not json")

        assert activity.get_all(_team(tmp_path)) == {}

    def test_a_corrupt_file_is_overwritten_by_the_next_write(self, tmp_path):
        team = _team(tmp_path)
        (tmp_path / "activity.json").write_text("{not json")

        activity.touch(team, "main")

        assert set(_read(tmp_path)) == {"main"}

    @pytest.mark.parametrize("doc", ["null", "[1, 2]", '"a string"', "42"])
    def test_a_json_document_of_the_wrong_shape_reads_as_empty(self, tmp_path, doc):
        # A truncated write can leave a valid-JSON `null` behind. Activity
        # tracking rides on stop/start/delete, so it must degrade instead of
        # raising an AttributeError out of the operation.
        (tmp_path / "activity.json").write_text(doc)

        assert activity.get_all(_team(tmp_path)) == {}

    def test_a_wrong_shaped_file_does_not_break_a_write(self, tmp_path):
        team = _team(tmp_path)
        (tmp_path / "activity.json").write_text("null")

        activity.mark_stopped(team, "main")  # must not raise

        assert set(_read(tmp_path)) == {"main"}

    def test_non_dict_entries_are_dropped_on_load(self, tmp_path):
        (tmp_path / "activity.json").write_text(
            json.dumps({"main": {"last_activity": "x"}, "junk": "not-a-dict"})
        )

        assert set(activity.get_all(_team(tmp_path))) == {"main"}


class TestTimestampsAreUsable:
    def test_a_written_timestamp_parses_back_to_roughly_now(self, tmp_path):
        team = _team(tmp_path)
        activity.touch(team, "main")

        written = activity.parse_ts(_read(tmp_path)["main"]["last_activity"])
        now = datetime.now(timezone.utc).timestamp()

        assert abs(now - written) < timedelta(minutes=1).total_seconds()
