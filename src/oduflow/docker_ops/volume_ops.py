"""Docker volume management for services.

Volumes are named Docker volumes with oduflow labels for tracking.
They persist independently of services and can be mounted to any service.
"""

from __future__ import annotations

import logging
import re

import docker

from oduflow.docker_ops.client import get_client
from oduflow.errors import ConflictError, NotFoundError
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")

_VOLUME_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


def _vol_labels(vol) -> dict[str, str]:
    """Return labels dict from a Docker Volume object.

    The Docker SDK Volume object stores labels in ``attrs['Labels']``
    (not as a direct ``.labels`` property like containers do).
    """
    return vol.attrs.get("Labels") or {}


def _docker_volume_name(team: TeamSettings, name: str) -> str:
    """Return the Docker volume name: ``oduflow-vol-{team_id}-{name}``."""
    return f"oduflow-vol-{team.team_id}-{name}"


def create_volume(
    settings: Settings,
    team: TeamSettings,
    name: str,
    description: str = "",
) -> dict[str, str]:
    """Create a named Docker volume with oduflow labels."""
    if not name or not _VOLUME_NAME_RE.match(name):
        raise ConflictError(
            f"Invalid volume name '{name}'. "
            "Use only alphanumeric characters, dots, hyphens and underscores."
        )

    client = get_client()
    docker_name = _docker_volume_name(team, name)

    # Check for existing volume
    try:
        client.volumes.get(docker_name)
        raise ConflictError(f"Volume '{name}' already exists.")
    except docker.errors.NotFound:
        pass

    labels = {
        settings.managed_label: "true",
        settings.team_label: team.team_id,
        "oduflow.volume": name,
        "oduflow.description": description,
    }

    client.volumes.create(name=docker_name, labels=labels)
    logger.info("Created volume %s", docker_name)

    return {
        "name": name,
        "docker_name": docker_name,
        "description": description,
    }


def list_volumes(settings: Settings, team: TeamSettings) -> list[dict]:
    """List all managed volumes for a team, including usage info."""
    client = get_client()
    volumes = client.volumes.list(
        filters={
            "label": [
                f"{settings.managed_label}=true",
                f"{settings.team_label}={team.team_id}",
                "oduflow.volume",
            ]
        }
    )

    usage = _find_all_volume_usage(settings, team)

    result = []
    for vol in volumes:
        labels = _vol_labels(vol)
        vol_name = labels.get("oduflow.volume", "")
        if not vol_name:
            continue
        docker_name = vol.name
        used_by = usage.get(docker_name, [])
        result.append(
            {
                "name": vol_name,
                "docker_name": docker_name,
                "description": labels.get("oduflow.description", ""),
                "created_at": vol.attrs.get("CreatedAt", ""),
                "used_by": used_by,
            }
        )

    result.sort(key=lambda v: v["name"])
    return result


def inspect_volume(settings: Settings, team: TeamSettings, name: str) -> dict:
    """Return details for a single volume including usage."""
    client = get_client()
    docker_name = _docker_volume_name(team, name)

    try:
        vol = client.volumes.get(docker_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Volume '{name}' not found.")

    labels = _vol_labels(vol)
    usage = _find_all_volume_usage(settings, team)
    used_by = usage.get(docker_name, [])

    return {
        "name": name,
        "docker_name": docker_name,
        "description": labels.get("oduflow.description", ""),
        "created_at": vol.attrs.get("CreatedAt", ""),
        "driver": vol.attrs.get("Driver", ""),
        "mountpoint": vol.attrs.get("Mountpoint", ""),
        "used_by": used_by,
    }


def delete_volume(settings: Settings, team: TeamSettings, name: str) -> dict[str, str]:
    """Delete a volume. Raises ConflictError if it is mounted by a service."""
    client = get_client()
    docker_name = _docker_volume_name(team, name)

    try:
        vol = client.volumes.get(docker_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Volume '{name}' not found.")

    usage = _find_all_volume_usage(settings, team)
    used_by = usage.get(docker_name, [])
    if used_by:
        svc_list = ", ".join(used_by)
        raise ConflictError(
            f"Volume '{name}' is in use by: {svc_list}. "
            "Remove the volume from those services first."
        )

    vol.remove()
    logger.info("Deleted volume %s", docker_name)
    return {"name": name, "docker_name": docker_name}


def _find_all_volume_usage(
    settings: Settings, team: TeamSettings
) -> dict[str, list[str]]:
    """Inspect all managed service containers to find volume mounts.

    Returns a mapping from Docker volume name to list of service names.
    """
    client = get_client()
    containers = client.containers.list(
        all=True,
        filters={
            "label": [
                f"{settings.managed_label}=true",
                f"{settings.team_label}={team.team_id}",
            ]
        },
    )

    usage: dict[str, list[str]] = {}
    for container in containers:
        svc_name = container.labels.get("oduflow.service")
        if not svc_name:
            continue
        mounts = container.attrs.get("Mounts", [])
        for mount in mounts:
            if mount.get("Type") == "volume":
                vol_docker_name = mount.get("Name", "")
                if vol_docker_name:
                    usage.setdefault(vol_docker_name, []).append(svc_name)

    return usage


def parse_volume_mounts(raw: str) -> list[dict[str, str]]:
    """Parse a volume mount specification string.

    Accepts comma or newline-separated entries in the format::

        volume_name:/container/path
        volume_name:/container/path:ro

    Returns a list of dicts: ``{"volume": ..., "mount_path": ..., "mode": ...}``.
    """
    if not raw or not raw.strip():
        return []

    result = []
    for entry in re.split(r"[\n,]+", raw):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) < 2:
            raise ValueError(
                f"Invalid volume mount '{entry}'. "
                "Expected format: volume_name:/container/path[:ro|rw]"
            )
        vol_name = parts[0].strip()
        mount_path = parts[1].strip()
        if len(parts) >= 3 and parts[1].strip() == "":
            # Handle Windows-style paths or empty segments — skip
            mount_path = ":".join(p.strip() for p in parts[1:-1]) or parts[1].strip()
        mode = "rw"
        if len(parts) >= 3:
            last = parts[-1].strip().lower()
            if last in ("ro", "rw"):
                mode = last
                # Re-join the path parts excluding first (volume) and last (mode)
                mount_path = ":".join(parts[1:-1]).strip()
            else:
                mount_path = ":".join(parts[1:]).strip()

        if not vol_name or not mount_path:
            raise ValueError(
                f"Invalid volume mount '{entry}'. "
                "Expected format: volume_name:/container/path[:ro|rw]"
            )
        result.append({"volume": vol_name, "mount_path": mount_path, "mode": mode})

    return result


def resolve_volume_binds(
    team: TeamSettings,
    volume_mounts: list[dict[str, str]],
) -> dict[str, dict[str, str]]:
    """Convert parsed volume mounts to Docker SDK volume binds dict.

    Validates that each referenced volume exists.
    Returns a dict suitable for ``container.run(volumes=...)``.
    """
    if not volume_mounts:
        return {}

    client = get_client()
    binds: dict[str, dict[str, str]] = {}

    for mount in volume_mounts:
        vol_name = mount["volume"]
        prefix = f"oduflow-vol-{team.team_id}-"
        if vol_name.startswith(prefix):
            vol_name = vol_name[len(prefix):]
        docker_name = _docker_volume_name(team, vol_name)
        try:
            client.volumes.get(docker_name)
        except docker.errors.NotFound:
            raise NotFoundError(
                f"Volume '{mount['volume']}' not found. Create it first."
            )
        binds[docker_name] = {"bind": mount["mount_path"], "mode": mount["mode"]}

    return binds
