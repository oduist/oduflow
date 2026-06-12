from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from oduflow.docker_ops.client import get_client
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")


def _calc_cpu_percent(stats: dict) -> float:
    """Calculate CPU usage % from a single stats snapshot."""
    cpu = stats.get("cpu_stats", {})
    precpu = stats.get("precpu_stats", {})

    cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - precpu.get(
        "cpu_usage", {}
    ).get("total_usage", 0)
    system_delta = cpu.get("system_cpu_usage", 0) - precpu.get("system_cpu_usage", 0)

    if system_delta <= 0 or cpu_delta < 0:
        return 0.0

    num_cpus = cpu.get("online_cpus") or len(
        cpu.get("cpu_usage", {}).get("percpu_usage", []) or [1]
    )
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
            "mem_percent": round((actual_mem / mem_limit) * 100.0, 1)
            if mem_limit > 0
            else 0.0,
        }
    except Exception:
        logger.debug("Failed to get stats for %s", container.name, exc_info=True)
        return None


def get_container_stats(settings: Settings, team: TeamSettings) -> list[dict[str, Any]]:
    """Collect CPU/RAM stats for all managed containers in parallel."""
    client = get_client()
    containers = [
        c
        for c in client.containers.list(
            all=True,
            filters={
                "label": [
                    f"{settings.managed_label}=true",
                    f"{settings.team_label}={team.team_id}",
                ]
            },
        )
        if c.name.startswith(settings.prefix)
    ]
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


def _read_memory_linux() -> tuple[float, float] | None:
    """Return (total_mb, used_mb) from /proc/meminfo, or None if unavailable."""
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
        if total <= 0:
            return None
        used = total - available
        return total / 1024, used / 1024
    except Exception:
        logger.debug("Failed to read /proc/meminfo", exc_info=True)
        return None


def _read_memory_macos() -> tuple[float, float] | None:
    """Return (total_mb, used_mb) via vm_stat, or None if unavailable.

    "Used" approximates Activity Monitor: active + wired + compressed pages.
    """
    try:
        out = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=5, check=True
        ).stdout
    except Exception:
        logger.debug("Failed to run vm_stat", exc_info=True)
        return None
    try:
        size_match = re.search(r"page size of (\d+) bytes", out)
        page_size = int(size_match.group(1)) if size_match else 4096
        pages: dict[str, int] = {}
        for line in out.splitlines():
            m = re.match(r'^"?([A-Za-z -]+?)"?:\s+(\d+)\.?\s*$', line)
            if m:
                pages[m.group(1)] = int(m.group(2))
        used_pages = (
            pages.get("Pages active", 0)
            + pages.get("Pages wired down", 0)
            + pages.get("Pages occupied by compressor", 0)
        )
        try:
            total_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        except (ValueError, OSError):
            total_bytes = int(
                subprocess.run(
                    ["sysctl", "-n", "hw.memsize"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=True,
                ).stdout.strip()
            )
        if total_bytes <= 0 or used_pages <= 0:
            return None
        return total_bytes / (1024 * 1024), used_pages * page_size / (1024 * 1024)
    except Exception:
        logger.debug("Failed to parse vm_stat output", exc_info=True)
        return None


def _read_cpu_ticks_linux() -> tuple[float, float] | None:
    """Return cumulative (total, idle) CPU ticks from /proc/stat."""
    try:
        with open("/proc/stat") as f:
            first = f.readline()
        parts = first.split()
        if parts[0] != "cpu" or len(parts) < 5:
            return None
        vals = [float(v) for v in parts[1:9]]
        total = sum(vals)
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0.0)  # idle + iowait
        return total, idle
    except Exception:
        logger.debug("Failed to read /proc/stat", exc_info=True)
        return None


def _read_cpu_ticks_macos() -> tuple[float, float] | None:
    """Return cumulative (total, idle) CPU ticks via mach host_statistics()."""
    try:
        import ctypes

        class _HostCpuLoadInfo(ctypes.Structure):
            # CPU_STATE_USER, CPU_STATE_SYSTEM, CPU_STATE_IDLE, CPU_STATE_NICE
            _fields_ = [("cpu_ticks", ctypes.c_uint32 * 4)]

        host_cpu_load_info = 3  # HOST_CPU_LOAD_INFO flavor
        libsystem = ctypes.CDLL("/usr/lib/libSystem.dylib")
        info = _HostCpuLoadInfo()
        count = ctypes.c_uint32(4)
        ret = libsystem.host_statistics(
            libsystem.mach_host_self(),
            host_cpu_load_info,
            ctypes.byref(info),
            ctypes.byref(count),
        )
        if ret != 0:
            return None
        user, system, idle, nice = (float(t) for t in info.cpu_ticks)
        return user + system + idle + nice, idle
    except Exception:
        logger.debug("Failed to read host_statistics", exc_info=True)
        return None


def _read_cpu_ticks() -> tuple[float, float] | None:
    if sys.platform == "darwin":
        return _read_cpu_ticks_macos()
    return _read_cpu_ticks_linux()


_cpu_ticks_prev: tuple[float, float] | None = None


def _cpu_percent_interval() -> float | None:
    """Actual CPU utilization % measured between calls.

    The first call takes a short 0.25s sample; afterwards each call measures
    the interval since the previous one (the dashboard's polling period),
    which smooths the value naturally. The 32-bit mach tick counters can
    wrap; a non-positive delta resets the baseline.
    """
    global _cpu_ticks_prev
    now = _read_cpu_ticks()
    if now is None:
        return None
    if _cpu_ticks_prev is None:
        _cpu_ticks_prev = now
        time.sleep(0.25)
        now = _read_cpu_ticks()
        if now is None:
            return None
    d_total = now[0] - _cpu_ticks_prev[0]
    d_idle = now[1] - _cpu_ticks_prev[1]
    _cpu_ticks_prev = now
    if d_total <= 0:
        return None
    return round(min(max((1.0 - d_idle / d_total) * 100.0, 0.0), 100.0), 1)


def get_system_stats() -> dict[str, Any]:
    """Read system CPU and memory stats (Linux via /proc, macOS via mach/vm_stat)."""
    result: dict[str, Any] = {
        "mem_total_mb": 0.0,
        "mem_used_mb": 0.0,
        "mem_percent": 0.0,
        "cpu_percent": 0.0,
        "load_avg": [0.0, 0.0, 0.0],
    }

    mem = _read_memory_linux()
    if mem is None and sys.platform == "darwin":
        mem = _read_memory_macos()
    if mem is not None:
        total_mb, used_mb = mem
        result["mem_total_mb"] = round(total_mb, 1)
        result["mem_used_mb"] = round(used_mb, 1)
        result["mem_percent"] = (
            round((used_mb / total_mb) * 100.0, 1) if total_mb > 0 else 0.0
        )

    # Load average
    try:
        load = os.getloadavg()
        result["load_avg"] = [round(v, 2) for v in load]
        result["cpu_count"] = os.cpu_count() or 1
    except Exception:
        logger.debug("Failed to get load average", exc_info=True)

    # CPU utilization: measured from kernel tick counters; fall back to the
    # old load-average approximation only if tick reading is unavailable.
    cpu = _cpu_percent_interval()
    if cpu is not None:
        result["cpu_percent"] = cpu
    elif result["load_avg"][0] and result.get("cpu_count"):
        result["cpu_percent"] = round(
            (result["load_avg"][0] / result["cpu_count"]) * 100.0, 1
        )

    return result
