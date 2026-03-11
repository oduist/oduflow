from __future__ import annotations

import json
import os
import logging

logger = logging.getLogger("oduflow")


def _load_registry(registry_path: str) -> dict[str, int]:
    """Load port registry from disk. Returns empty dict if file doesn't exist or is corrupt."""
    if not os.path.isfile(registry_path):
        return {}
    try:
        with open(registry_path) as f:
            data = json.load(f)
        return {k: int(v) for k, v in data.items()}
    except (json.JSONDecodeError, ValueError, OSError) as e:
        logger.warning("Could not load port registry %s: %s", registry_path, e)
        return {}


def _save_registry(registry_path: str, registry: dict[str, int]) -> None:
    """Atomically save port registry to disk."""
    tmp_path = registry_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(registry, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, registry_path)


def allocate_port(
    registry_path: str,
    env_name: str,
    port_range_start: int,
    port_range_end: int,
    used_ports: set[int] | None = None,
) -> int:
    """Allocate a stable port for an environment.

    If the environment already has a port in the registry AND it's not used by another
    container (checked via used_ports), reuse it. Otherwise allocate the next free port.

    Args:
        registry_path: Path to ports.json
        env_name: Environment to allocate port for
        port_range_start: Start of port range (inclusive)
        port_range_end: End of port range (exclusive)
        used_ports: Set of ports currently in use by OTHER environments' Docker containers.
                    If None, no conflict checking against Docker is done.

    Returns:
        The allocated port number.

    Raises:
        FlowError if no free ports available.
    """
    from oduflow.errors import FlowError

    if used_ports is None:
        used_ports = set()

    registry = _load_registry(registry_path)

    if env_name in registry:
        existing_port = registry[env_name]
        if (
            port_range_start <= existing_port < port_range_end
            and existing_port not in used_ports
        ):
            return existing_port

    occupied = set(registry.values()) | used_ports

    for port in range(port_range_start, port_range_end):
        if port not in occupied:
            registry[env_name] = port
            _save_registry(registry_path, registry)
            logger.info("Allocated port %d for environment '%s'", port, env_name)
            return port

    raise FlowError(
        f"No free ports in range {port_range_start}-{port_range_end}. "
        f"Delete unused environments to free ports."
    )


def release_port(registry_path: str, env_name: str) -> None:
    """Remove port assignment for an environment."""
    registry = _load_registry(registry_path)
    if env_name in registry:
        port = registry.pop(env_name)
        _save_registry(registry_path, registry)
        logger.info("Released port %d for environment '%s'", port, env_name)


def get_port(registry_path: str, env_name: str) -> int | None:
    """Get the assigned port for an environment, or None if not assigned."""
    registry = _load_registry(registry_path)
    return registry.get(env_name)
