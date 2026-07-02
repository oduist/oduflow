"""Sync template data from an external source (S3 or local path)."""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any

from oduflow.docker_ops import env_ops, system_ops
from oduflow.docker_ops.client import get_client
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")


def sync_template_from_source(
    settings: Settings,
    team: TeamSettings,
    template_name: str,
    source: str,
) -> dict[str, Any]:
    """Sync template files from source, then reload the template DB.

    Source directory is expected to contain dump.pgdump (or dump.sql)
    and optionally filestore/.
    """
    template_dir = team.get_template_dir(template_name)
    os.makedirs(template_dir, exist_ok=True)

    client = get_client()

    # Sync files. The sync overwrites the template filestore (the overlay lower
    # layer), so do it non-destructively: live overlay envs are unmounted
    # (keeping their upper deltas) and remounted against the new lower on exit.
    with env_ops.remount_template_overlays(
        client, settings, team, template_name
    ) as remount:
        if source.startswith("s3://"):
            _run_s3_sync(source, template_dir)
        else:
            _run_local_sync(source, template_dir)

    # Validate dump exists after sync
    dump_path = team.get_template_sql_path(template_name)
    if not os.path.isfile(dump_path):
        raise FileNotFoundError(
            f"No dump file found in template dir after sync: {template_dir}"
        )

    # Reload template DB
    result = system_ops.reload_template(settings, team, template_name=template_name)

    return {
        "sync_source": source,
        "template_dir": template_dir,
        "dump_path": dump_path,
        "affected_envs": remount.affected,
        "remount_failures": remount.failures,
        **result,
    }


def _run_s3_sync(source: str, dest: str) -> None:
    source = source.rstrip("/") + "/"
    logger.info("S3 sync: %s -> %s", source, dest)
    subprocess.run(
        ["aws", "s3", "sync", "--delete", source, dest],
        check=True,
    )


def _run_local_sync(source: str, dest: str) -> None:
    source = source.rstrip("/") + "/"
    dest = dest.rstrip("/") + "/"
    logger.info("Local sync: %s -> %s", source, dest)
    subprocess.run(
        ["rsync", "-a", "--delete", source, dest],
        check=True,
    )
