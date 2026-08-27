"""Render the PostgreSQL HBA rules owned by Oduflow."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

BEGIN_MARKER = "# BEGIN ODUFLOW MANAGED NETWORKS"
END_MARKER = "# END ODUFLOW MANAGED NETWORKS"
SUPPORTED_AUTH_METHODS = frozenset({"md5", "scram-sha-256"})


def normalize_cidrs(cidrs: Iterable[str]) -> list[str]:
    """Return canonical, de-duplicated network CIDRs in stable order."""
    networks = {ipaddress.ip_network(value, strict=False) for value in cidrs}
    return [
        network.with_prefixlen
        for network in sorted(
            networks,
            key=lambda item: (item.version, int(item.network_address), item.prefixlen),
        )
    ]


def render_managed_block(cidrs: Iterable[str], auth_method: str) -> str:
    """Render the HBA block that permits Oduflow-managed Docker networks."""
    auth_method = auth_method.strip().lower()
    if auth_method not in SUPPORTED_AUTH_METHODS:
        raise ValueError(f"Unsupported PostgreSQL host auth method: {auth_method}")

    networks = normalize_cidrs(cidrs)
    if not networks:
        raise ValueError("At least one PostgreSQL client network is required")

    lines = [
        BEGIN_MARKER,
        "# Reconciled from Docker network IPAM by Oduflow; do not edit this block.",
    ]
    lines.extend(f"host all all {cidr} {auth_method}" for cidr in networks)
    lines.append(END_MARKER)
    return "\n".join(lines)


def reconcile_managed_block(
    current: str, cidrs: Iterable[str], auth_method: str
) -> str:
    """Insert or replace Oduflow's block while preserving every other rule."""
    block = render_managed_block(cidrs, auth_method)
    begin_count = current.count(BEGIN_MARKER)
    end_count = current.count(END_MARKER)
    if begin_count != end_count or begin_count > 1:
        raise ValueError("Malformed Oduflow-managed block in pg_hba.conf")

    if begin_count == 0:
        remainder = current.lstrip("\n")
        result = f"{block}\n\n{remainder}" if remainder else f"{block}\n"
    else:
        start = current.index(BEGIN_MARKER)
        end_start = current.find(END_MARKER)
        if end_start < start:
            raise ValueError("Malformed Oduflow-managed block in pg_hba.conf")
        end = end_start + len(END_MARKER)
        result = current[:start] + block + current[end:]

    return result.rstrip("\n") + "\n"
