"""Startup data migrations.

Odoo-style upgrade mechanism for the on-disk/Docker state Oduflow manages:
the code ships an ordered, append-only registry of one-shot migration steps,
and the data directory records which steps were already applied
(``migrations.json`` under ``base_data_dir``). On server start Oduflow diffs
the registry against the recorded state and applies only the missing steps,
oldest first.

A fresh install (no state file and no ``team_*`` data yet) is stamped as
fully applied without running anything — the same way Odoo skips migration
scripts when a module is installed from scratch rather than upgraded.

Adding a migration:

- append a :class:`Migration` to ``MIGRATIONS`` with the next sequence number
  in its id (``"0001-team-scoped-container-names"``);
- never reorder, rename, or remove existing entries — recorded ids are what
  keeps reruns idempotent on existing installs;
- make the step itself idempotent where possible: state is persisted after
  each successful step, so a step that crashed halfway is retried on the next
  start.
"""

import fcntl
import glob
import json
import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from oduflow.errors import PrerequisiteNotMetError
from oduflow.settings import Settings

logger = logging.getLogger("oduflow")

_STATE_FILENAME = "migrations.json"


@dataclass(frozen=True)
class Migration:
    """One irreversible upgrade step, identified by a stable sequence id."""

    id: str
    description: str
    apply: Callable[[Settings], None]


def _migrate_team_scoped_names(settings: Settings) -> None:
    """Rename managed containers to the team-scoped naming scheme.

    ``oduflow-{env}-{type}`` → ``oduflow-{team}-{env}-{type}`` and
    ``oduflow-svc-{name}`` → ``oduflow-{team}-svc-{name}``. Docker rename
    keeps volumes, network attachment, and restart policy; Traefik routing is
    label-based (labels are immutable but reference no container names), so
    nothing else changes. Idempotent: a container whose name no longer
    matches the old scheme is skipped, so a partially-applied run resumes
    cleanly.
    """
    import docker

    from oduflow.docker_ops.client import get_client
    from oduflow.naming import get_resource_name, get_service_container_name

    client = get_client()
    for team_id in settings.teams:
        containers = client.containers.list(
            all=True,
            filters={
                "label": [
                    f"{settings.managed_label}=true",
                    f"{settings.team_label}={team_id}",
                ]
            },
        )
        for container in containers:
            name = container.name
            if not name.startswith(settings.prefix):
                continue
            svc_name = container.labels.get("oduflow.service")
            branch = container.labels.get(settings.branch_label)
            if svc_name:
                if not name.startswith(f"{settings.prefix}svc-"):
                    continue  # already team-scoped
                new_name = get_service_container_name(
                    svc_name, settings.prefix, team_id
                )
            elif branch:
                old_prefix = f"{settings.prefix}{branch.replace('/', '-')}-"
                if not name.startswith(old_prefix):
                    continue  # already team-scoped
                resource_type = name[len(old_prefix) :]
                new_name = get_resource_name(
                    branch, resource_type, settings.prefix, team_id
                )
            else:
                continue
            if name == new_name:
                continue
            try:
                container.rename(new_name)
            except docker.errors.APIError as exc:
                raise RuntimeError(
                    f"Failed to rename container {name} -> {new_name}: {exc}"
                ) from exc
            logger.info("Renamed container %s -> %s", name, new_name)


# Append-only registry, executed in list order. Ids are recorded in
# migrations.json once applied; reordering or renaming entries would re-run
# or skip steps on existing installs.
MIGRATIONS: list[Migration] = [
    Migration(
        id="0001-team-scoped-container-names",
        description=(
            "Rename managed containers to team-scoped names "
            "(oduflow-{team}-{env}-{type}, oduflow-{team}-svc-{name})"
        ),
        apply=_migrate_team_scoped_names,
    ),
]


@contextmanager
def _state_lock(state_path: str) -> Iterator[None]:
    """Serialize state read-modify-write across processes (and threads:
    flock on two separate fds of the same file contends within one process
    too)."""
    fd = os.open(state_path + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _load_state(state_path: str) -> list[str] | None:
    """Return the applied-id list, or None if no state was ever recorded."""
    if not os.path.isfile(state_path):
        return None
    with open(state_path) as f:
        data = json.load(f)
    applied = data.get("applied", [])
    return [str(mig_id) for mig_id in applied]


def _write_state(state_path: str, applied: list[str]) -> None:
    tmp_path = state_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump({"applied": applied}, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, state_path)


def _is_fresh_install(settings: Settings) -> bool:
    """No prior per-team data means there is nothing to migrate."""
    pattern = os.path.join(settings.base_data_dir, "team_*")
    return not any(os.path.isdir(path) for path in glob.glob(pattern))


def run_pending(
    settings: Settings, registry: list[Migration] | None = None
) -> list[str]:
    """Apply not-yet-applied migrations, oldest first; return the ids run.

    Called once at server start, *before* shared-infrastructure init, so a
    migration sees the data dir and Docker resources exactly as the previous
    version left them. A failing step aborts startup (already-applied steps
    stay recorded and are not re-run on the next attempt).
    """
    migs = MIGRATIONS if registry is None else registry
    ids = [mig.id for mig in migs]
    if len(set(ids)) != len(ids):
        raise ValueError(f"Duplicate migration ids in registry: {ids}")

    os.makedirs(settings.base_data_dir, exist_ok=True)
    state_path = os.path.join(settings.base_data_dir, _STATE_FILENAME)
    ran: list[str] = []
    with _state_lock(state_path):
        applied = _load_state(state_path)
        if applied is None:
            if _is_fresh_install(settings):
                # Fresh install: current code lays down current-shape data,
                # so historical steps have nothing to act on.
                _write_state(state_path, ids)
                if ids:
                    logger.info(
                        "Fresh install: stamped %d migration(s) as applied",
                        len(ids),
                    )
                return []
            applied = []  # pre-migrations-era install: everything is pending

        for mig in migs:
            if mig.id in applied:
                continue
            logger.info("Applying migration %s: %s", mig.id, mig.description)
            try:
                mig.apply(settings)
            except Exception as exc:
                raise PrerequisiteNotMetError(
                    f"Startup migration '{mig.id}' failed: {exc}. Fix the "
                    "cause and restart — already-applied steps will not "
                    "re-run."
                ) from exc
            applied.append(mig.id)
            # Persist after every step so a crash resumes at the failed one.
            _write_state(state_path, applied)
            ran.append(mig.id)

    if ran:
        logger.info("Applied %d migration(s): %s", len(ran), ", ".join(ran))
    return ran
