from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from oduflow import bundled_upgrade


def _managed(
    tmp_path: Path,
    *,
    source: bytes,
    local: bytes | None,
    baseline: bytes | None,
) -> bundled_upgrade.ManagedFile:
    source_path = tmp_path / "package" / "guide.md"
    source_path.parent.mkdir(parents=True)
    source_path.write_bytes(source)

    destination = tmp_path / "team" / "agent_guides" / "guide.md"
    if local is not None:
        destination.parent.mkdir(parents=True)
        destination.write_bytes(local)

    managed = bundled_upgrade.ManagedFile(
        label="[team.1]",
        source=source_path,
        destination=destination,
        state_root=tmp_path / "team" / ".bundled_upgrade",
        relative_path=Path("agent_guides/guide.md"),
    )
    if baseline is not None:
        managed.baseline_path.parent.mkdir(parents=True)
        managed.baseline_path.write_bytes(baseline)
    return managed


def test_seed_creates_deployed_file_and_baseline(tmp_path: Path):
    managed = _managed(tmp_path, source=b"bundled\n", local=None, baseline=None)

    assert bundled_upgrade.seed_managed_file(managed) is True

    assert managed.destination.read_bytes() == b"bundled\n"
    assert managed.baseline_path.read_bytes() == b"bundled\n"


def test_seed_adopts_only_an_identical_existing_file(tmp_path: Path):
    identical = _managed(
        tmp_path / "identical",
        source=b"bundled\n",
        local=b"bundled\n",
        baseline=None,
    )
    custom = _managed(
        tmp_path / "custom",
        source=b"bundled\n",
        local=b"custom\n",
        baseline=None,
    )

    assert bundled_upgrade.seed_managed_file(identical) is False
    assert identical.baseline_path.read_bytes() == b"bundled\n"
    assert bundled_upgrade.seed_managed_file(custom) is False
    assert not custom.baseline_path.exists()


def test_same_size_local_edit_is_preserved(tmp_path: Path):
    managed = _managed(
        tmp_path,
        source=b"value=old\n",
        local=b"value=new\n",
        baseline=b"value=old\n",
    )

    plan = bundled_upgrade.plan_reconcile(managed)
    bundled_upgrade.apply_reconcile(plan)

    assert plan.kind == "local-only"
    assert managed.destination.read_bytes() == b"value=new\n"


def test_upstream_only_change_updates_and_backs_up(tmp_path: Path):
    managed = _managed(
        tmp_path,
        source=b"bundled v2\n",
        local=b"bundled v1\n",
        baseline=b"bundled v1\n",
    )

    plan = bundled_upgrade.plan_reconcile(managed)
    bundled_upgrade.apply_reconcile(plan)

    assert plan.kind == "update"
    assert managed.destination.read_bytes() == b"bundled v2\n"
    assert managed.baseline_path.read_bytes() == b"bundled v2\n"
    assert managed.backup_path.read_bytes() == b"bundled v1\n"


def test_disjoint_changes_merge_cleanly(tmp_path: Path):
    managed = _managed(
        tmp_path,
        source=b"alpha=1\nunchanged one\nunchanged two\nbeta=2\n",
        local=b"alpha=2\nunchanged one\nunchanged two\nbeta=1\n",
        baseline=b"alpha=1\nunchanged one\nunchanged two\nbeta=1\n",
    )

    plan = bundled_upgrade.plan_reconcile(managed)
    bundled_upgrade.apply_reconcile(plan)

    assert plan.kind == "merge"
    assert managed.destination.read_bytes() == (
        b"alpha=2\nunchanged one\nunchanged two\nbeta=2\n"
    )
    assert managed.baseline_path.read_bytes() == (
        b"alpha=1\nunchanged one\nunchanged two\nbeta=2\n"
    )
    assert managed.backup_path.read_bytes() == (
        b"alpha=2\nunchanged one\nunchanged two\nbeta=1\n"
    )


def test_conflict_keeps_live_file_and_baseline(tmp_path: Path):
    managed = _managed(
        tmp_path,
        source=b"value=upstream\n",
        local=b"value=local\n",
        baseline=b"value=base\n",
    )

    plan = bundled_upgrade.plan_reconcile(managed)
    bundled_upgrade.apply_reconcile(plan)

    assert plan.kind == "conflict"
    assert managed.destination.read_bytes() == b"value=local\n"
    assert managed.baseline_path.read_bytes() == b"value=base\n"
    assert managed.pending_path.read_bytes() == b"value=upstream\n"
    artifact = managed.merge_path.read_text(encoding="utf-8")
    assert "<<<<<<<" in artifact
    assert "value=local" in artifact
    assert "value=upstream" in artifact


def test_keep_marker_is_an_unconditional_opt_out(tmp_path: Path):
    local = b"# KEEP\nvalue=local\n"
    managed = _managed(
        tmp_path,
        source=b"value=upstream\n",
        local=local,
        baseline=b"value=base\n",
    )

    plan = bundled_upgrade.plan_reconcile(managed)
    bundled_upgrade.apply_reconcile(plan)

    assert plan.kind == "keep"
    assert managed.destination.read_bytes() == local
    assert managed.baseline_path.read_bytes() == b"value=base\n"


def test_legacy_file_is_preserved_behind_review_sidecar(tmp_path: Path):
    managed = _managed(
        tmp_path,
        source=b"bundled v2\n",
        local=b"custom legacy\n",
        baseline=None,
    )

    plan = bundled_upgrade.plan_reconcile(managed)
    bundled_upgrade.apply_reconcile(plan)

    assert plan.kind == "legacy"
    assert plan.needs_attention is True
    assert managed.destination.read_bytes() == b"custom legacy\n"
    assert managed.new_path.read_bytes() == b"bundled v2\n"
    assert not managed.baseline_path.exists()
    assert managed.pending_path.read_bytes() == b"bundled v2\n"
    assert bundled_upgrade.plan_reconcile(managed).kind == "legacy-pending"


def test_pending_legacy_sidecar_tracks_latest_bundle(tmp_path: Path):
    managed = _managed(
        tmp_path,
        source=b"bundled v2\n",
        local=b"custom legacy\n",
        baseline=None,
    )
    bundled_upgrade.apply_reconcile(bundled_upgrade.plan_reconcile(managed))
    managed.source.write_bytes(b"bundled v3\n")

    pending = bundled_upgrade.plan_reconcile(managed)
    bundled_upgrade.apply_reconcile(pending)

    assert pending.kind == "legacy-pending"
    assert managed.destination.read_bytes() == b"custom legacy\n"
    assert managed.new_path.read_bytes() == b"bundled v3\n"
    assert not managed.baseline_path.exists()
    assert managed.pending_path.read_bytes() == b"bundled v3\n"


def test_deleting_legacy_sidecar_accepts_pending_bundle_as_baseline(tmp_path: Path):
    managed = _managed(
        tmp_path,
        source=b"bundled v2\n",
        local=b"custom plus bundled v2\n",
        baseline=None,
    )
    bundled_upgrade.apply_reconcile(bundled_upgrade.plan_reconcile(managed))
    managed.new_path.unlink()

    resolved = bundled_upgrade.plan_reconcile(managed)
    bundled_upgrade.apply_reconcile(resolved)

    assert resolved.kind == "resolved"
    assert managed.destination.read_bytes() == b"custom plus bundled v2\n"
    assert managed.baseline_path.read_bytes() == b"bundled v2\n"
    assert not managed.pending_path.exists()


def test_resolved_conflict_advances_baseline_after_sidecar_is_removed(tmp_path: Path):
    managed = _managed(
        tmp_path,
        source=b"value=upstream\n",
        local=b"value=local\n",
        baseline=b"value=base\n",
    )
    bundled_upgrade.apply_reconcile(bundled_upgrade.plan_reconcile(managed))
    managed.destination.write_bytes(b"value=manually-resolved\n")
    managed.merge_path.unlink()

    resolved = bundled_upgrade.plan_reconcile(managed)
    bundled_upgrade.apply_reconcile(resolved)

    assert resolved.kind == "resolved"
    assert managed.destination.read_bytes() == b"value=manually-resolved\n"
    assert managed.baseline_path.read_bytes() == b"value=upstream\n"
    assert not managed.pending_path.exists()


def test_missing_git_fails_safe_and_exposes_new_bundle(tmp_path: Path):
    managed = _managed(
        tmp_path,
        source=b"alpha=1\nbeta=2\n",
        local=b"alpha=2\nbeta=1\n",
        baseline=b"alpha=1\nbeta=1\n",
    )

    with patch(
        "oduflow.bundled_upgrade.subprocess.run",
        side_effect=FileNotFoundError("git not found"),
    ):
        plan = bundled_upgrade.plan_reconcile(managed)
    bundled_upgrade.apply_reconcile(plan)

    assert plan.kind == "error"
    assert managed.destination.read_bytes() == b"alpha=2\nbeta=1\n"
    assert managed.baseline_path.read_bytes() == b"alpha=1\nbeta=1\n"
    assert managed.error_path.read_bytes() == b"alpha=1\nbeta=2\n"


def test_force_overwrites_a_conflict_and_backs_up_the_live_file(tmp_path: Path):
    managed = _managed(
        tmp_path,
        source=b"value=upstream\n",
        local=b"value=local\n",
        baseline=b"value=base\n",
    )

    plan = bundled_upgrade.plan_reconcile(managed, force=True)
    bundled_upgrade.apply_reconcile(plan)

    assert plan.kind == "overwrite"
    assert plan.needs_attention is False
    assert managed.destination.read_bytes() == b"value=upstream\n"
    assert managed.baseline_path.read_bytes() == b"value=upstream\n"
    assert managed.backup_path.read_bytes() == b"value=local\n"
    assert not managed.merge_path.exists()
    assert not managed.pending_path.exists()
    assert bundled_upgrade.plan_reconcile(managed).kind == "up-to-date"


def test_force_overwrites_a_legacy_file(tmp_path: Path):
    managed = _managed(
        tmp_path,
        source=b"bundled v2\n",
        local=b"custom legacy\n",
        baseline=None,
    )

    plan = bundled_upgrade.plan_reconcile(managed, force=True)
    bundled_upgrade.apply_reconcile(plan)

    assert plan.kind == "overwrite"
    assert managed.destination.read_bytes() == b"bundled v2\n"
    assert managed.baseline_path.read_bytes() == b"bundled v2\n"
    assert managed.backup_path.read_bytes() == b"custom legacy\n"
    assert not managed.new_path.exists()
    assert not managed.pending_path.exists()
    assert bundled_upgrade.plan_reconcile(managed).kind == "up-to-date"


def test_force_clears_sidecars_left_by_an_earlier_run(tmp_path: Path):
    managed = _managed(
        tmp_path,
        source=b"bundled v2\n",
        local=b"custom legacy\n",
        baseline=None,
    )
    bundled_upgrade.apply_reconcile(bundled_upgrade.plan_reconcile(managed))
    assert managed.new_path.exists()

    forced = bundled_upgrade.plan_reconcile(managed, force=True)
    bundled_upgrade.apply_reconcile(forced)

    assert forced.kind == "overwrite"
    assert managed.destination.read_bytes() == b"bundled v2\n"
    assert managed.baseline_path.read_bytes() == b"bundled v2\n"
    assert not managed.new_path.exists()
    assert not managed.pending_path.exists()
    assert bundled_upgrade.plan_reconcile(managed).kind == "up-to-date"


def test_force_clears_a_conflict_sidecar_left_by_an_earlier_run(tmp_path: Path):
    managed = _managed(
        tmp_path,
        source=b"value=upstream\n",
        local=b"value=local\n",
        baseline=b"value=base\n",
    )
    bundled_upgrade.apply_reconcile(bundled_upgrade.plan_reconcile(managed))
    assert managed.merge_path.exists()
    assert managed.pending_path.exists()

    forced = bundled_upgrade.plan_reconcile(managed, force=True)
    bundled_upgrade.apply_reconcile(forced)

    assert forced.kind == "overwrite"
    assert managed.destination.read_bytes() == b"value=upstream\n"
    assert managed.baseline_path.read_bytes() == b"value=upstream\n"
    assert managed.backup_path.read_bytes() == b"value=local\n"
    assert not managed.merge_path.exists()
    assert not managed.pending_path.exists()
    assert not managed.new_path.exists()
    assert not managed.error_path.exists()
    assert bundled_upgrade.plan_reconcile(managed).kind == "up-to-date"


def test_force_overwrites_a_conflict_sidecar_whose_baseline_is_missing(tmp_path: Path):
    managed = _managed(
        tmp_path,
        source=b"value=upstream\n",
        local=b"value=local\n",
        baseline=None,
    )
    managed.merge_path.write_bytes(b"stale merge artifact\n")

    assert bundled_upgrade.plan_reconcile(managed).kind == "error"

    forced = bundled_upgrade.plan_reconcile(managed, force=True)
    bundled_upgrade.apply_reconcile(forced)

    assert forced.kind == "overwrite"
    assert managed.destination.read_bytes() == b"value=upstream\n"
    assert managed.baseline_path.read_bytes() == b"value=upstream\n"
    assert managed.backup_path.read_bytes() == b"value=local\n"
    assert not managed.merge_path.exists()
    assert not managed.pending_path.exists()
    assert bundled_upgrade.plan_reconcile(managed).kind == "up-to-date"


def test_force_overwrites_when_merging_is_unavailable(tmp_path: Path):
    managed = _managed(
        tmp_path,
        source=b"alpha=1\nbeta=2\n",
        local=b"alpha=2\nbeta=1\n",
        baseline=b"alpha=1\nbeta=1\n",
    )

    with patch(
        "oduflow.bundled_upgrade.subprocess.run",
        side_effect=FileNotFoundError("git not found"),
    ):
        plan = bundled_upgrade.plan_reconcile(managed, force=True)
    bundled_upgrade.apply_reconcile(plan)

    assert plan.kind == "overwrite"
    assert managed.destination.read_bytes() == b"alpha=1\nbeta=2\n"
    assert managed.backup_path.read_bytes() == b"alpha=2\nbeta=1\n"
    assert not managed.error_path.exists()


def test_force_still_merges_disjoint_changes(tmp_path: Path):
    managed = _managed(
        tmp_path,
        source=b"alpha=1\nunchanged one\nunchanged two\nbeta=2\n",
        local=b"alpha=2\nunchanged one\nunchanged two\nbeta=1\n",
        baseline=b"alpha=1\nunchanged one\nunchanged two\nbeta=1\n",
    )

    plan = bundled_upgrade.plan_reconcile(managed, force=True)
    bundled_upgrade.apply_reconcile(plan)

    assert plan.kind == "merge"
    assert managed.destination.read_bytes() == (
        b"alpha=2\nunchanged one\nunchanged two\nbeta=2\n"
    )


def test_force_leaves_local_only_changes_and_keep_files_alone(tmp_path: Path):
    local_only = _managed(
        tmp_path / "local-only",
        source=b"value=old\n",
        local=b"value=new\n",
        baseline=b"value=old\n",
    )
    kept_content = b"# KEEP\nvalue=local\n"
    kept = _managed(
        tmp_path / "kept",
        source=b"value=upstream\n",
        local=kept_content,
        baseline=b"value=base\n",
    )

    local_plan = bundled_upgrade.plan_reconcile(local_only, force=True)
    bundled_upgrade.apply_reconcile(local_plan)
    kept_plan = bundled_upgrade.plan_reconcile(kept, force=True)
    bundled_upgrade.apply_reconcile(kept_plan)

    assert local_plan.kind == "local-only"
    assert local_only.destination.read_bytes() == b"value=new\n"
    assert kept_plan.kind == "keep"
    assert kept.destination.read_bytes() == kept_content


def test_apply_refuses_a_file_changed_after_planning(tmp_path: Path):
    managed = _managed(
        tmp_path,
        source=b"bundled v2\n",
        local=b"bundled v1\n",
        baseline=b"bundled v1\n",
    )
    plan = bundled_upgrade.plan_reconcile(managed)
    managed.destination.write_bytes(b"changed during prompt\n")

    with pytest.raises(bundled_upgrade.BundledUpgradeError):
        bundled_upgrade.apply_reconcile(plan)

    assert managed.destination.read_bytes() == b"changed during prompt\n"
