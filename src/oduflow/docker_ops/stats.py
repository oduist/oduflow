import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from oduflow.docker_ops.client import get_client
from oduflow.settings import Settings

logger = logging.getLogger("oduflow")


def _calc_cpu_percent(stats: dict) -> float:
    """Calculate CPU usage % from a single stats snapshot."""
    cpu = stats.get("cpu_stats", {})
    precpu = stats.get("precpu_stats", {})

    cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - precpu.get("cpu_usage", {}).get("total_usage", 0)
    system_delta = cpu.get("system_cpu_usage", 0) - precpu.get("system_cpu_usage", 0)

    if system_delta <= 0 or cpu_delta < 0:
        return 0.0

    num_cpus = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage", []) or [1])
    return round((cpu_delta / system_delta) * num_cpus * 100.0, 1)


def _get_one_container_stats(container) -> dict[str, Any] | None:
    """Get stats for a single container. Returns None on error."""
    try:
        if container.status != "running":
            return {
                "name": container.name,
                "cpu_percent": 0.0,
                "mem_usage_mb": 0.0,
                "mem_limit_mb": 0.0,
                "mem_percent": 0.0,
            }
        stats = container.stats(stream=False)
        mem = stats.get("memory_stats", {})
        mem_usage = mem.get("usage", 0)
        # Subtract cache if available (actual process memory)
        cache = mem.get("stats", {}).get("cache", 0) if mem.get("stats") else 0
        actual_mem = mem_usage - cache
        mem_limit = mem.get("limit", 0)
        return {
            "name": container.name,
            "cpu_percent": _calc_cpu_percent(stats),
            "mem_usage_mb": round(actual_mem / (1024 * 1024), 1),
            "mem_limit_mb": round(mem_limit / (1024 * 1024), 1),
            "mem_percent": round((actual_mem / mem_limit) * 100.0, 1) if mem_limit > 0 else 0.0,
        }
    except Exception:
        logger.debug("Failed to get stats for %s", container.name, exc_info=True)
        return None


def get_container_stats(settings: Settings) -> list[dict[str, Any]]:
    """Collect CPU/RAM stats for all managed containers in parallel."""
    client = get_client()
    containers = client.containers.list(
        all=True,
        filters={
            "label": [
                f"{settings.managed_label}=true",
                f"{settings.instance_label}={settings.instance_id}"
            ]
        },
    )
    if not containers:
        return []

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(len(containers), 8)) as pool:
        futures = {pool.submit(_get_one_container_stats, c): c for c in containers}
        for future in as_completed(futures):
            stat = future.result()
            if stat is not None:
                results.append(stat)

    results.sort(key=lambda s: s["name"])
    return results


def get_system_stats() -> dict[str, Any]:
    """Read system CPU and memory stats from /proc (Linux only)."""
    result: dict[str, Any] = {
        "mem_total_mb": 0.0,
        "mem_used_mb": 0.0,
        "mem_percent": 0.0,
        "cpu_percent": 0.0,
        "load_avg": [0.0, 0.0, 0.0],
    }

    # Memory from /proc/meminfo
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    key = parts[0].rstrip(":")
                    info[key] = int(parts[1])  # in kB
        total = info.get("MemTotal", 0)
        available = info.get("MemAvailable", 0)
        used = total - available
        result["mem_total_mb"] = round(total / 1024, 1)
        result["mem_used_mb"] = round(used / 1024, 1)
        result["mem_percent"] = round((used / total) * 100.0, 1) if total > 0 else 0.0
    except Exception:
        logger.debug("Failed to read /proc/meminfo", exc_info=True)

    # Load average
    try:
        load = os.getloadavg()
        result["load_avg"] = [round(v, 2) for v in load]
        result["cpu_count"] = os.cpu_count() or 1
        # Approximate CPU % from 1-minute load average
        result["cpu_percent"] = round((load[0] / result["cpu_count"]) * 100.0, 1)
    except Exception:
        logger.debug("Failed to get load average", exc_info=True)

    return result
