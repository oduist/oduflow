"""Per-team disk quota enforcement via XFS project quotas.

A team's disk quota (``[team.*] disk_quota_gb``) is enforced by the kernel,
not by walking directories: when ``base_data_dir`` lives on an XFS filesystem
mounted with ``prjquota``, each team gets a filesystem *project* spanning its
two directory trees — ``team_{id}/`` (workspaces, filestores, template dumps)
and ``pg_tablespaces/team_{id}/`` (its PostgreSQL databases, see ADR 0026).
Project quotas attach to the project ID rather than a single subtree, so one
``bhard`` limit covers the client's files and databases together, and writes
beyond it fail instantly with ENOSPC.

When the filesystem does not support project quotas (macOS, ext4 without
project quota, XFS without ``prjquota``), enforcement is simply off and the
reason is logged once at startup — usage stays visible via the dashboard and
``/api/usage``. Numeric project IDs are allocated per team in
``base_data_dir/quota_projects.json``.

Applied idempotently on every server start (``apply_all``).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys

from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")

_PROJECTS_FILE = "quota_projects.json"
_FIRST_PROJECT_ID = 1001


def _read_mounts() -> list[tuple[str, str, str]]:
    """Return (mountpoint, fstype, options) for every mounted filesystem."""
    mounts: list[tuple[str, str, str]] = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4:
                    mounts.append((parts[1], parts[2], parts[3]))
    except OSError:
        pass
    return mounts


def _find_mount(path: str) -> tuple[str, str, str] | None:
    """The mount entry holding ``path`` (longest matching mountpoint)."""
    real = os.path.realpath(path)
    best: tuple[str, str, str] | None = None
    for mountpoint, fstype, options in _read_mounts():
        if real == mountpoint or real.startswith(mountpoint.rstrip("/") + "/"):
            if best is None or len(mountpoint) > len(best[0]):
                best = (mountpoint, fstype, options)
    return best


def quota_support(settings: Settings) -> tuple[bool, str]:
    """Whether project-quota enforcement is available for the data dir."""
    if not sys.platform.startswith("linux"):
        return False, f"platform is {sys.platform}, not linux"
    if shutil.which("xfs_quota") is None:
        return False, "xfs_quota binary not found (install xfsprogs)"
    mount = _find_mount(settings.base_data_dir)
    if mount is None:
        return False, f"no mount found for {settings.base_data_dir}"
    mountpoint, fstype, options = mount
    if fstype != "xfs":
        return False, f"{mountpoint} is {fstype}, not xfs"
    opts = options.split(",")
    if not any(opt in ("prjquota", "pquota") for opt in opts):
        return False, f"{mountpoint} is not mounted with prjquota"
    return True, mountpoint


def _load_project_ids(settings: Settings) -> dict[str, int]:
    path = os.path.join(settings.base_data_dir, _PROJECTS_FILE)
    try:
        with open(path) as f:
            data = json.load(f)
        return {str(k): int(v) for k, v in data.items()}
    except (OSError, ValueError):
        return {}


def _project_id_for(settings: Settings, team_id: str) -> int:
    """Stable numeric project ID for a team (allocated once, persisted)."""
    ids = _load_project_ids(settings)
    if team_id in ids:
        return ids[team_id]
    new_id = max(ids.values(), default=_FIRST_PROJECT_ID - 1) + 1
    ids[team_id] = new_id
    path = os.path.join(settings.base_data_dir, _PROJECTS_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(ids, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    return new_id


def _xfs_quota(mountpoint: str, command: str) -> None:
    result = subprocess.run(
        ["xfs_quota", "-x", "-c", command, mountpoint],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"xfs_quota -c '{command}' failed: {result.stderr.strip()}")


def apply_team_disk_quota(
    settings: Settings, team: TeamSettings, mountpoint: str
) -> None:
    """Attach the team's directories to its project and set the hard limit."""
    project_id = _project_id_for(settings, team.team_id)
    trees = [
        team.data_dir,
        os.path.join(settings.base_data_dir, "pg_tablespaces", f"team_{team.team_id}"),
    ]
    for tree in trees:
        os.makedirs(tree, exist_ok=True)
        # -s: set up the project (recursively stamps the project ID).
        _xfs_quota(mountpoint, f"project -s -p {tree} {project_id}")
    _xfs_quota(mountpoint, f"limit -p bhard={team.disk_quota_gb}g {project_id}")
    logger.info(
        "Disk quota for team '%s': %d GB (project %d)",
        team.team_id,
        team.disk_quota_gb,
        project_id,
    )


def apply_all(settings: Settings) -> None:
    """Apply disk quotas for every team with disk_quota_gb > 0 (idempotent).

    Called on server start. Unsupported filesystems log one warning and skip;
    a failure for one team does not block the others or the startup.
    """
    teams = [t for t in settings.teams.values() if t.disk_quota_gb > 0]
    if not teams:
        return
    supported, detail = quota_support(settings)
    if not supported:
        logger.warning(
            "disk_quota_gb is set but filesystem quota enforcement is "
            "unavailable: %s. Usage stays visible on the dashboard; the "
            "limit is not enforced.",
            detail,
        )
        return
    for team in teams:
        try:
            apply_team_disk_quota(settings, team, detail)
        except Exception as exc:
            logger.error(
                "Failed to apply disk quota for team '%s': %s",
                team.team_id,
                exc,
            )
