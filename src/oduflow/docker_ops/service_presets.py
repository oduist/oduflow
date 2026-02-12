"""Manage service preset configurations (save / restore / list / delete).

Presets are persisted as a single JSON file at ``{settings.home}/service_presets.json``.
"""

import json
import logging
import os

from oduflow.errors import NotFoundError
from oduflow.settings import Settings

logger = logging.getLogger("oduflow")


def _presets_path(settings: Settings) -> str:
    """Return the absolute path to the presets JSON file."""
    return os.path.join(settings.home, "service_presets.json")


def _load_presets(settings: Settings) -> dict:
    """Read and return all presets from disk.

    Returns an empty dict when the file does not exist or contains invalid JSON.
    """
    path = _presets_path(settings)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError:
        logger.warning("Corrupt presets file at %s – returning empty dict", path)
        return {}


def _save_presets(settings: Settings, data: dict) -> None:
    """Persist *data* to the presets JSON file, creating parent dirs if needed."""
    path = _presets_path(settings)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def save_preset(
    settings: Settings,
    name: str,
    image: str,
    port: int,
    hostname: str | None = None,
    env_vars: dict[str, str] | None = None,
) -> dict:
    """Save (or overwrite) a single service preset and return it."""
    preset = {
        "image": image,
        "port": port,
        "hostname": hostname or "",
        "env_vars": env_vars if env_vars is not None else {},
    }
    data = _load_presets(settings)
    data[name] = preset
    _save_presets(settings, data)
    return preset


def list_presets(settings: Settings) -> list[dict]:
    """Return all presets as a sorted list of dicts (each includes a ``name`` key)."""
    data = _load_presets(settings)
    return sorted(
        [{"name": name, **preset} for name, preset in data.items()],
        key=lambda p: p["name"],
    )


def get_preset(settings: Settings, name: str) -> dict:
    """Return a single preset dict (with ``name`` key) or raise :class:`NotFoundError`."""
    data = _load_presets(settings)
    if name not in data:
        raise NotFoundError(f"Service preset '{name}' not found")
    return {"name": name, **data[name]}


def delete_preset(settings: Settings, name: str) -> dict:
    """Remove a preset from disk and return ``{"name": name}``.

    Raises :class:`NotFoundError` if the preset does not exist.
    """
    data = _load_presets(settings)
    if name not in data:
        raise NotFoundError(f"Service preset '{name}' not found")
    del data[name]
    _save_presets(settings, data)
    return {"name": name}
