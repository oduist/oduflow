"""Safely reconcile deployed files with newer bundled versions.

Each managed file keeps the pristine bundled content last seen by Oduflow as
its baseline.  That gives upgrades the three inputs required for a real merge:
the previous bundle, the operator's live file, and the new bundle.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ActionKind = Literal[
    "up-to-date",
    "adopt",
    "create",
    "update",
    "local-only",
    "resolved",
    "merge",
    "overwrite",
    "keep",
    "legacy",
    "legacy-pending",
    "conflict",
    "error",
]


class BundledUpgradeError(RuntimeError):
    """Raised when an upgrade plan can no longer be applied safely."""


@dataclass(frozen=True)
class ManagedFile:
    """One bundled source and its deployed, baseline, and backup locations."""

    label: str
    source: Path
    destination: Path
    state_root: Path
    relative_path: Path

    @property
    def baseline_path(self) -> Path:
        return self.state_root / "baselines" / self.relative_path

    @property
    def backup_path(self) -> Path:
        return self.state_root / "backups" / self.relative_path

    @property
    def pending_path(self) -> Path:
        return self.state_root / "pending" / self.relative_path

    @property
    def new_path(self) -> Path:
        return Path(f"{self.destination}.oduflow-new")

    @property
    def merge_path(self) -> Path:
        return Path(f"{self.destination}.oduflow-merge")

    @property
    def error_path(self) -> Path:
        return Path(f"{self.destination}.oduflow-error-new")


@dataclass(frozen=True)
class ReconcilePlan:
    """A side-effect-free decision for one managed file."""

    managed: ManagedFile
    kind: ActionKind
    source_content: bytes
    local_content: bytes | None = None
    baseline_content: bytes | None = None
    result_content: bytes | None = None
    error: str = ""
    base_path: Path | None = None
    pending_acknowledged: bool = False

    @property
    def needs_confirmation(self) -> bool:
        return self.kind in {
            "create",
            "update",
            "merge",
            "overwrite",
            "legacy",
            "legacy-pending",
            "conflict",
            "error",
        }

    @property
    def needs_attention(self) -> bool:
        return self.kind in {"legacy", "legacy-pending", "conflict", "error"}


def seed_managed_file(managed: ManagedFile) -> bool:
    """Create a missing deployed file and its baseline.

    Existing custom files are never adopted as a baseline.  An existing file
    is safe to adopt only when it is byte-for-byte equal to the current bundle.
    Returns True when the deployed file itself was created.
    """
    source_content = managed.source.read_bytes()
    try:
        local_content = managed.destination.read_bytes()
    except FileNotFoundError:
        _atomic_write(managed.destination, source_content, reference=managed.source)
        _atomic_write(managed.baseline_path, source_content, reference=managed.source)
        return True

    if local_content == source_content:
        try:
            baseline_content = managed.baseline_path.read_bytes()
        except FileNotFoundError:
            baseline_content = None
        if baseline_content != source_content:
            _atomic_write(
                managed.baseline_path, source_content, reference=managed.source
            )
    return False


def plan_reconcile(managed: ManagedFile, *, force: bool = False) -> ReconcilePlan:
    """Classify one file without changing the filesystem.

    With ``force`` the cases that would otherwise stop for a human — legacy
    files, merge conflicts, and merge failures — resolve in favour of the new
    bundle instead: the live file is backed up and overwritten.  Clean merges
    still merge, local-only changes are still left alone, and a first-line
    ``# KEEP`` remains an unconditional opt-out.
    """
    source_content = managed.source.read_bytes()
    try:
        local_content = managed.destination.read_bytes()
    except FileNotFoundError:
        return ReconcilePlan(managed, "create", source_content)

    if _has_keep_marker(local_content):
        return ReconcilePlan(
            managed, "keep", source_content, local_content=local_content
        )

    try:
        baseline_content = managed.baseline_path.read_bytes()
    except FileNotFoundError:
        baseline_content = None

    if local_content == source_content:
        sidecars_exist = (
            managed.new_path.exists()
            or managed.merge_path.exists()
            or managed.error_path.exists()
            or managed.pending_path.exists()
        )
        kind: ActionKind = (
            "up-to-date"
            if baseline_content == source_content and not sidecars_exist
            else "adopt"
        )
        return ReconcilePlan(
            managed,
            kind,
            source_content,
            local_content=local_content,
            baseline_content=baseline_content,
        )

    # Sidecars are explicit gates. Keep refreshing them from the last accepted
    # baseline until the operator reconciles the live file and removes them.
    if managed.new_path.exists():
        if force:
            return _overwrite_plan(managed, source_content, local_content)
        return ReconcilePlan(
            managed,
            "legacy-pending",
            source_content,
            local_content=local_content,
            baseline_content=baseline_content,
            result_content=source_content,
        )

    if managed.merge_path.exists():
        if baseline_content is None:
            if force:
                return _overwrite_plan(managed, source_content, local_content)
            return ReconcilePlan(
                managed,
                "error",
                source_content,
                local_content=local_content,
                result_content=source_content,
                error="conflict sidecar exists but its accepted baseline is missing",
            )
        merged_content, error, conflicted = _merge_file(managed, managed.baseline_path)
        if (error or conflicted) and force:
            return _overwrite_plan(managed, source_content, local_content)
        if error:
            return ReconcilePlan(
                managed,
                "error",
                source_content,
                local_content=local_content,
                baseline_content=baseline_content,
                result_content=source_content,
                error=error,
                base_path=managed.baseline_path,
            )
        return ReconcilePlan(
            managed,
            "conflict" if conflicted else "merge",
            source_content,
            local_content=local_content,
            baseline_content=baseline_content,
            result_content=merged_content,
            base_path=managed.baseline_path,
        )

    pending_acknowledged = False
    base_path = managed.baseline_path
    try:
        pending_content = managed.pending_path.read_bytes()
    except FileNotFoundError:
        pending_content = None
    else:
        # Removing the visible sidecar acknowledges that the operator resolved
        # it. The pending bundle becomes the accepted base for this run.
        baseline_content = pending_content
        base_path = managed.pending_path
        pending_acknowledged = True

    if baseline_content is None:
        if force:
            return _overwrite_plan(managed, source_content, local_content)
        return ReconcilePlan(
            managed,
            "legacy",
            source_content,
            local_content=local_content,
            result_content=source_content,
        )

    if local_content == baseline_content:
        return ReconcilePlan(
            managed,
            "update",
            source_content,
            local_content=local_content,
            baseline_content=baseline_content,
            result_content=source_content,
            base_path=base_path,
            pending_acknowledged=pending_acknowledged,
        )

    if source_content == baseline_content:
        if pending_acknowledged:
            return ReconcilePlan(
                managed,
                "resolved",
                source_content,
                local_content=local_content,
                baseline_content=baseline_content,
                base_path=base_path,
                pending_acknowledged=True,
            )
        return ReconcilePlan(
            managed,
            "local-only",
            source_content,
            local_content=local_content,
            baseline_content=baseline_content,
            base_path=base_path,
        )

    merged_content, error, conflicted = _merge_file(managed, base_path)
    if (error or conflicted) and force:
        return _overwrite_plan(managed, source_content, local_content)
    if error:
        return ReconcilePlan(
            managed,
            "error",
            source_content,
            local_content=local_content,
            baseline_content=baseline_content,
            result_content=source_content,
            error=error,
            base_path=base_path,
            pending_acknowledged=pending_acknowledged,
        )
    return ReconcilePlan(
        managed,
        "conflict" if conflicted else "merge",
        source_content,
        local_content=local_content,
        baseline_content=baseline_content,
        result_content=merged_content,
        base_path=base_path,
        pending_acknowledged=pending_acknowledged,
    )


def _overwrite_plan(
    managed: ManagedFile, source_content: bytes, local_content: bytes
) -> ReconcilePlan:
    """Resolve a would-be sidecar case in favour of the new bundle."""
    return ReconcilePlan(
        managed,
        "overwrite",
        source_content,
        local_content=local_content,
        result_content=source_content,
    )


def apply_reconcile(plan: ReconcilePlan) -> None:
    """Apply a previously computed plan with atomic writes and drift checks."""
    managed = plan.managed
    _assert_unchanged(managed.source, plan.source_content, "bundled source")

    if plan.local_content is None:
        if managed.destination.exists():
            raise BundledUpgradeError(
                f"{managed.destination} appeared after the upgrade was planned"
            )
    else:
        _assert_unchanged(managed.destination, plan.local_content, "deployed file")
    if plan.base_path is not None and plan.baseline_content is not None:
        _assert_unchanged(plan.base_path, plan.baseline_content, "merge baseline")

    if plan.kind in {"up-to-date", "local-only", "keep"}:
        return

    if plan.kind == "adopt":
        _write_baseline(plan)
        _unlink_if_exists(managed.pending_path)
        _remove_generated_sidecars(managed)
        return

    if plan.kind == "resolved":
        if plan.baseline_content is None:
            raise BundledUpgradeError("Resolved plan has no accepted baseline")
        _write_baseline_content(managed, plan.baseline_content)
        _unlink_if_exists(managed.pending_path)
        _remove_generated_sidecars(managed)
        return

    if plan.kind == "create":
        _atomic_write(
            managed.destination,
            plan.source_content,
            reference=managed.source,
        )
        _write_baseline(plan)
        _unlink_if_exists(managed.pending_path)
        _remove_generated_sidecars(managed)
        return

    if plan.kind in {"update", "merge", "overwrite"}:
        if plan.local_content is None or plan.result_content is None:
            raise BundledUpgradeError(f"Incomplete {plan.kind} plan")
        _atomic_write(
            managed.backup_path,
            plan.local_content,
            reference=managed.destination,
        )
        _atomic_write(
            managed.destination,
            plan.result_content,
            reference=managed.destination,
        )
        _write_baseline(plan)
        _unlink_if_exists(managed.pending_path)
        _remove_generated_sidecars(managed)
        return

    if plan.kind in {"legacy", "legacy-pending"}:
        _atomic_write(
            managed.new_path,
            plan.source_content,
            reference=managed.source,
        )
        _atomic_write(
            managed.pending_path,
            plan.source_content,
            reference=managed.source,
        )
        _unlink_if_exists(managed.merge_path)
        _unlink_if_exists(managed.error_path)
        return

    if plan.kind == "conflict":
        if plan.result_content is None:
            raise BundledUpgradeError("Conflict plan has no merge artifact")
        _atomic_write(
            managed.merge_path,
            plan.result_content,
            reference=managed.destination,
        )
        if plan.pending_acknowledged:
            if plan.baseline_content is None:
                raise BundledUpgradeError("Conflict plan has no accepted baseline")
            _write_baseline_content(managed, plan.baseline_content)
        _atomic_write(
            managed.pending_path,
            plan.source_content,
            reference=managed.source,
        )
        _unlink_if_exists(managed.new_path)
        _unlink_if_exists(managed.error_path)
        return

    if plan.kind == "error":
        if plan.pending_acknowledged:
            if plan.baseline_content is None:
                raise BundledUpgradeError("Error plan has no accepted baseline")
            _write_baseline_content(managed, plan.baseline_content)
            _unlink_if_exists(managed.pending_path)
        _atomic_write(
            managed.error_path,
            plan.source_content,
            reference=managed.source,
        )
        return

    raise BundledUpgradeError(f"Unknown reconcile action: {plan.kind}")


def _merge_file(
    managed: ManagedFile, base_path: Path
) -> tuple[bytes | None, str, bool]:
    """Run git's diff3 merge without modifying any input file."""
    try:
        result = subprocess.run(
            [
                "git",
                "merge-file",
                "-p",
                "-L",
                str(managed.destination),
                "-L",
                "previous bundled version",
                "-L",
                "new bundled version",
                str(managed.destination),
                str(base_path),
                str(managed.source),
            ],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return None, f"git merge-file could not run: {exc}", False

    if result.returncode == 0:
        return result.stdout, "", False
    if result.returncode == 1:
        return result.stdout, "", True
    stderr = result.stderr.decode("utf-8", errors="replace").strip()
    detail = stderr or f"exit code {result.returncode}"
    return None, f"git merge-file failed: {detail}", False


def _has_keep_marker(content: bytes) -> bool:
    lines = content.splitlines()
    return bool(lines and lines[0].strip() == b"# KEEP")


def _write_baseline(plan: ReconcilePlan) -> None:
    _write_baseline_content(plan.managed, plan.source_content)


def _write_baseline_content(managed: ManagedFile, content: bytes) -> None:
    _atomic_write(
        managed.baseline_path,
        content,
        reference=managed.source,
    )


def _assert_unchanged(path: Path, expected: bytes, description: str) -> None:
    try:
        current = path.read_bytes()
    except FileNotFoundError as exc:
        raise BundledUpgradeError(
            f"{description} disappeared after the upgrade was planned: {path}"
        ) from exc
    if current != expected:
        raise BundledUpgradeError(
            f"{description} changed after the upgrade was planned: {path}"
        )


def _remove_generated_sidecars(managed: ManagedFile) -> None:
    _unlink_if_exists(managed.new_path)
    _unlink_if_exists(managed.merge_path)
    _unlink_if_exists(managed.error_path)


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _atomic_write(path: Path, content: bytes, *, reference: Path | None) -> None:
    """Write bytes with fsync + replace while retaining useful metadata."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())

        mode = 0o644
        owner: tuple[int, int] | None = None
        if reference is not None:
            try:
                reference_stat = reference.stat()
            except FileNotFoundError:
                pass
            else:
                mode = stat.S_IMODE(reference_stat.st_mode)
                owner = (reference_stat.st_uid, reference_stat.st_gid)
        os.chmod(tmp_path, mode)
        if owner is not None:
            try:
                os.chown(tmp_path, *owner)
            except PermissionError:
                pass

        os.replace(tmp_path, path)
        _fsync_directory(path.parent)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
