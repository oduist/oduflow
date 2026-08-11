"""One host-wide resource plan for Oduflow-managed workloads.

The PostgreSQL and production Odoo renderers deliberately stay separate: they
have different runtime concerns.  Their resource inputs, however, must come
from one deterministic plan so enabling production cannot make every service
size itself as if it owned the whole host.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from typing import Literal

PLANNER_VERSION = 1

ProfileName = Literal["dev", "production"]
TuneStatus = Literal["current", "stale", "legacy", "custom", "missing"]


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class PostgresBudget:
    """Inputs shared by a PostgreSQL profile renderer."""

    cpu_count: int
    shared_buffers_mb: int
    effective_cache_size_mb: int


@dataclass(frozen=True)
class ResourcePlan:
    """Advisory budgets for all resource-tuned services on one host."""

    host_ram_mb: int
    host_cpu_count: int
    production_enabled: bool
    system_reserve_mb: int
    dev_runtime_budget_mb: int
    dev_postgres: PostgresBudget
    production_postgres: PostgresBudget | None
    production_odoo_ram_budget_mb: int
    production_odoo_cpu_count: int


def build_resource_plan(
    total_ram_mb: float,
    cpu_count: int,
    *,
    production_enabled: bool,
) -> ResourcePlan:
    """Build the single resource plan used by every tuning renderer.

    With production disabled, the existing lean dev profile retains its 10%
    shared-buffer target.  With production enabled, the host is planned as a
    whole: 20% is reserved for the OS/other services, 45% is the production
    Odoo worker budget, and PostgreSQL shared buffers target 25% in total
    (5% dev + 20% production).  Floors protect tiny installations and caps
    stop large hosts from pinning excessive RAM in PostgreSQL.

    CPU values are concurrency ceilings rather than Docker reservations.  In
    production mode the two PostgreSQL clusters split the host CPU count while
    Odoo worker sizing uses 75% of host CPUs, allowing the database to burst.
    """
    ram = max(int(total_ram_mb), 0)
    cpu = max(int(cpu_count), 1)
    system_reserve = round(ram * 0.20)

    if not production_enabled:
        dev_shared = int(clamp(round(ram * 0.10), 128, 1024))
        dev_pg = PostgresBudget(
            cpu_count=cpu,
            shared_buffers_mb=dev_shared,
            effective_cache_size_mb=dev_shared * 3,
        )
        return ResourcePlan(
            host_ram_mb=ram,
            host_cpu_count=cpu,
            production_enabled=False,
            system_reserve_mb=system_reserve,
            dev_runtime_budget_mb=max(ram - system_reserve - dev_shared, 0),
            dev_postgres=dev_pg,
            production_postgres=None,
            production_odoo_ram_budget_mb=0,
            production_odoo_cpu_count=0,
        )

    dev_shared = int(clamp(round(ram * 0.05), 128, 512))
    prod_shared = int(clamp(round(ram * 0.20), 512, 8192))
    dev_cpu = max(cpu // 2, 1)
    prod_cpu = max(cpu - dev_cpu, 1)
    prod_odoo_ram = round(ram * 0.45)
    prod_odoo_cpu = max(math.ceil(cpu * 0.75), 1)

    # effective_cache_size is a planner hint, not reserved memory.  Still keep
    # both clusters' hints coordinated: production gets 45% of host RAM and
    # dev gets at most another 10%, rather than each claiming the same cache.
    dev_effective_cache = max(
        dev_shared,
        min(dev_shared * 3, round(ram * 0.10)),
    )
    prod_effective_cache = int(clamp(round(ram * 0.45), 1024, 65536))

    return ResourcePlan(
        host_ram_mb=ram,
        host_cpu_count=cpu,
        production_enabled=True,
        system_reserve_mb=system_reserve,
        dev_runtime_budget_mb=max(
            ram - system_reserve - prod_odoo_ram - dev_shared - prod_shared,
            0,
        ),
        dev_postgres=PostgresBudget(
            cpu_count=dev_cpu,
            shared_buffers_mb=dev_shared,
            effective_cache_size_mb=dev_effective_cache,
        ),
        production_postgres=PostgresBudget(
            cpu_count=prod_cpu,
            shared_buffers_mb=prod_shared,
            effective_cache_size_mb=prod_effective_cache,
        ),
        production_odoo_ram_budget_mb=prod_odoo_ram,
        production_odoo_cpu_count=prod_odoo_cpu,
    )


def plan_fingerprint(plan: ResourcePlan, profile: ProfileName) -> str:
    """Stable short fingerprint for stale-config detection."""
    payload = {
        "planner_version": PLANNER_VERSION,
        "profile": profile,
        "plan": asdict(plan),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def tune_marker(plan: ResourcePlan, profile: ProfileName) -> str:
    mode = "on" if plan.production_enabled else "off"
    return (
        f"# ODUFLOW-TUNE planner={PLANNER_VERSION} profile={profile} "
        f"fingerprint={plan_fingerprint(plan, profile)} production={mode}"
    )


def tune_status(content: str | None, expected_marker: str) -> TuneStatus:
    """Classify an existing config without treating custom files as managed."""
    if content is None:
        return "missing"
    header = "\n".join(content.splitlines()[:12])
    if expected_marker in header:
        return "current"
    if "# ODUFLOW-TUNE " in header:
        return "stale"
    if "PostgreSQL configuration auto-generated by Oduflow" in header:
        return "legacy"
    if (
        "# PostgreSQL Configuration" in header
        and "# Server: 2 vCPU, 4 GB RAM, HDD" in header
    ):
        return "legacy"
    return "custom"


def describe_plan(plan: ResourcePlan) -> list[str]:
    """Human-readable summary used by the retune CLI."""
    lines = [
        f"Host: {plan.host_cpu_count} vCPU, {plan.host_ram_mb} MB RAM",
        "Production: " + ("enabled" if plan.production_enabled else "disabled"),
        (
            "Dev PostgreSQL: "
            f"{plan.dev_postgres.cpu_count} CPU ceiling, "
            f"shared_buffers={plan.dev_postgres.shared_buffers_mb}MB, "
            "effective_cache_size="
            f"{plan.dev_postgres.effective_cache_size_mb}MB"
        ),
    ]
    if plan.production_postgres is not None:
        lines.extend(
            [
                (
                    "Production PostgreSQL: "
                    f"{plan.production_postgres.cpu_count} CPU ceiling, "
                    f"shared_buffers={plan.production_postgres.shared_buffers_mb}MB, "
                    "effective_cache_size="
                    f"{plan.production_postgres.effective_cache_size_mb}MB"
                ),
                (
                    "Production Odoo: "
                    f"{plan.production_odoo_cpu_count} CPU sizing budget, "
                    f"{plan.production_odoo_ram_budget_mb}MB RAM budget"
                ),
            ]
        )
    lines.append(f"OS/other-services reserve: {plan.system_reserve_mb}MB")
    lines.append(f"Dev runtime headroom: {plan.dev_runtime_budget_mb}MB")
    return lines
