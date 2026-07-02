import json
import os

import pytest

from oduflow.errors import PrerequisiteNotMetError
from oduflow.migrations import MIGRATIONS, Migration, run_pending
from oduflow.settings import Settings


def _settings(tmp_path) -> Settings:
    return Settings(base_data_dir=str(tmp_path))


def _mig(mig_id: str, calls: list[str], fail: bool = False) -> Migration:
    def apply(settings: Settings) -> None:
        if fail:
            raise RuntimeError("boom")
        calls.append(mig_id)

    return Migration(id=mig_id, description=f"test step {mig_id}", apply=apply)


def _state(tmp_path) -> list[str]:
    with open(os.path.join(str(tmp_path), "migrations.json")) as f:
        return json.load(f)["applied"]


def test_fresh_install_stamps_without_running(tmp_path):
    calls: list[str] = []
    registry = [_mig("0001-a", calls), _mig("0002-b", calls)]

    ran = run_pending(_settings(tmp_path), registry)

    assert ran == []
    assert calls == []
    assert _state(tmp_path) == ["0001-a", "0002-b"]

    # Second start: still a no-op.
    assert run_pending(_settings(tmp_path), registry) == []
    assert calls == []


def test_legacy_install_runs_all_in_order(tmp_path):
    # A pre-migrations-era install is recognized by existing team data.
    os.makedirs(tmp_path / "team_1")
    calls: list[str] = []
    registry = [_mig("0001-a", calls), _mig("0002-b", calls)]

    ran = run_pending(_settings(tmp_path), registry)

    assert ran == ["0001-a", "0002-b"]
    assert calls == ["0001-a", "0002-b"]
    assert _state(tmp_path) == ["0001-a", "0002-b"]


def test_only_pending_steps_run(tmp_path):
    os.makedirs(tmp_path / "team_1")
    calls: list[str] = []
    registry = [_mig("0001-a", calls)]
    run_pending(_settings(tmp_path), registry)
    calls.clear()

    # A new release appends a step: only it runs.
    registry.append(_mig("0002-b", calls))
    ran = run_pending(_settings(tmp_path), registry)

    assert ran == ["0002-b"]
    assert calls == ["0002-b"]
    assert _state(tmp_path) == ["0001-a", "0002-b"]


def test_failure_aborts_and_resumes_at_failed_step(tmp_path):
    os.makedirs(tmp_path / "team_1")
    calls: list[str] = []
    registry = [
        _mig("0001-a", calls),
        _mig("0002-b", calls, fail=True),
        _mig("0003-c", calls),
    ]

    with pytest.raises(PrerequisiteNotMetError, match="0002-b"):
        run_pending(_settings(tmp_path), registry)

    # The successful step is recorded; the failed one and its successor are not.
    assert calls == ["0001-a"]
    assert _state(tmp_path) == ["0001-a"]

    # Next start (cause fixed): resumes at the failed step, skips 0001-a.
    fixed = [_mig("0001-a", calls), _mig("0002-b", calls), _mig("0003-c", calls)]
    ran = run_pending(_settings(tmp_path), fixed)

    assert ran == ["0002-b", "0003-c"]
    assert calls == ["0001-a", "0002-b", "0003-c"]
    assert _state(tmp_path) == ["0001-a", "0002-b", "0003-c"]


def test_duplicate_ids_rejected(tmp_path):
    calls: list[str] = []
    registry = [_mig("0001-a", calls), _mig("0001-a", calls)]

    with pytest.raises(ValueError, match="Duplicate migration ids"):
        run_pending(_settings(tmp_path), registry)


def test_default_registry_ids_unique_and_sorted():
    # Registry order is execution order; keeping ids sorted keeps the file
    # readable and reviewable as it grows.
    ids = [mig.id for mig in MIGRATIONS]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
