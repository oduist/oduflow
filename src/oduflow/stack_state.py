"""Small atomic per-team state records for declarative stacks."""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Any

from oduflow.settings import TeamSettings

STATE_API_VERSION = "oduflow.dev/state-v1"


def state_path(team: TeamSettings, stack_name: str) -> Path:
    return Path(team.data_dir) / "stacks" / f"{stack_name}.json"


def load_state(team: TeamSettings, stack_name: str) -> dict[str, Any]:
    path = state_path(team, stack_name)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(
    team: TeamSettings,
    stack_name: str,
    manifest_digest: str,
    resources: dict[str, Any],
) -> dict[str, Any]:
    """Persist non-secret apply metadata using fsync + atomic replace."""
    data = {
        "apiVersion": STATE_API_VERSION,
        "stack": stack_name,
        "manifestHash": manifest_digest,
        "appliedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "resources": resources,
    }
    path = state_path(team, stack_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return data
