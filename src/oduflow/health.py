"""System health checks: dev PG, prod PG, Traefik, S3, disk, productions.

Backs the public ``GET /healthz`` endpoint (uptime-monitor friendly:
200 all-ok / 503 degraded) and the dashboard's status-bar chips. Results
are cached briefly so a polling dashboard doesn't hammer Docker/S3.

Check states: ``ok`` | ``warn`` | ``error`` | ``off`` (not applicable /
not configured). Only ``error`` on a critical check degrades /healthz.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from typing import Any

from oduflow.settings import Settings

logger = logging.getLogger("oduflow")

_CACHE_TTL_SECONDS = 15
DISK_WARN_PERCENT = 85

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "result": None}


def _check_pg(client: Any, settings: Settings, container_name: str) -> dict[str, Any]:
    import docker

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        return {"status": "off", "detail": "not provisioned"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200]}
    if container.status != "running":
        return {"status": "error", "detail": f"container {container.status}"}
    try:
        exit_code, _ = container.exec_run(["pg_isready", "-U", settings.db_user])
        if exit_code == 0:
            return {"status": "ok", "detail": ""}
        return {"status": "error", "detail": "pg_isready failed"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200]}


def _check_traefik(client: Any, settings: Settings) -> dict[str, Any]:
    import docker

    if settings.routing_mode != "traefik":
        return {"status": "off", "detail": "port routing mode"}
    try:
        container = client.containers.get(settings.traefik_container)
    except docker.errors.NotFound:
        return {"status": "error", "detail": "container missing"}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200]}
    if container.status != "running":
        return {"status": "error", "detail": f"container {container.status}"}
    return {"status": "ok", "detail": ""}


def _check_s3(settings: Settings) -> dict[str, Any]:
    if not settings.prod_enabled:
        return {"status": "off", "detail": "production hosting disabled"}
    if settings.backup is None:
        return {"status": "off", "detail": "backups not configured"}
    from oduflow.s3_client import check_s3

    probe = check_s3(settings.backup)
    if probe["ok"]:
        return {"status": "ok", "detail": ""}
    return {"status": "error", "detail": probe["error"][:200]}


def _check_disk(settings: Settings) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(settings.base_data_dir or "/")
    except OSError as exc:
        return {"status": "error", "detail": str(exc)[:200], "percent": 0}
    percent = round(usage.used / usage.total * 100)
    status = "warn" if percent >= DISK_WARN_PERCENT else "ok"
    return {
        "status": status,
        "detail": f"{percent}% used",
        "percent": percent,
        "free_gb": round(usage.free / 1024**3, 1),
    }


def _unhealthy_productions(settings: Settings) -> list[str]:
    from oduflow import production_registry

    names = []
    for team in settings.teams.values():
        try:
            for name, record in production_registry.list_productions(team).items():
                if record.get("unhealthy"):
                    names.append(f"{team.team_id}/{name}")
        except Exception:
            continue
    return names


def _disabled_production_status(client: Any, settings: Settings) -> dict[str, Any]:
    """Confirm disabled production workloads are actually stopped."""
    try:
        containers = client.containers.list(
            all=True,
            filters={
                "label": [
                    f"{settings.managed_label}=true",
                    "oduflow.prod=true",
                ]
            },
        )
    except Exception as exc:
        return {"status": "error", "detail": str(exc)[:200], "running": []}
    running = sorted(c.name for c in containers if c.status == "running")
    if running:
        return {
            "status": "error",
            "detail": "production disabled but containers still running: "
            + ", ".join(running),
            "running": running,
        }
    return {
        "status": "off",
        "detail": "production hosting disabled",
        "running": [],
    }


def collect_health(settings: Settings, *, force: bool = False) -> dict[str, Any]:
    """All checks with a short cache. ``ok`` is the /healthz verdict."""
    now = time.monotonic()
    with _cache_lock:
        cached = _cache["result"]
        if not force and cached is not None and now - _cache["at"] < _CACHE_TTL_SECONDS:
            return cached  # type: ignore[no-any-return]

    checks: dict[str, Any] = {}
    try:
        from oduflow.docker_ops.client import get_client

        client = get_client()
    except Exception as exc:
        client = None
        checks["docker"] = {"status": "error", "detail": str(exc)[:200]}

    if client is not None:
        checks["dev_pg"] = _check_pg(client, settings, settings.shared_db_container)
        checks["prod_pg"] = (
            _check_pg(client, settings, settings.prod_db_container)
            if settings.prod_enabled
            else {"status": "off", "detail": "production hosting disabled"}
        )
        checks["traefik"] = _check_traefik(client, settings)
    checks["s3"] = _check_s3(settings)
    checks["disk"] = _check_disk(settings)

    if not settings.prod_enabled and client is not None:
        checks["productions"] = _disabled_production_status(client, settings)
    else:
        unhealthy = _unhealthy_productions(settings) if settings.prod_enabled else []
        checks["productions"] = {
            "status": "error" if unhealthy else "ok",
            "detail": ", ".join(unhealthy),
            "unhealthy": unhealthy,
        }

    # Critical checks: dev PG always; prod PG only when provisioned; traefik
    # when in traefik mode; S3 when configured; unhealthy productions.
    critical = ["dev_pg", "prod_pg", "traefik", "s3", "productions", "docker"]
    ok = all(
        checks.get(name, {}).get("status") in ("ok", "warn", "off", None)
        for name in critical
    )
    result = {"ok": ok, "checks": checks}
    with _cache_lock:
        _cache["at"] = now
        _cache["result"] = result
    return result
