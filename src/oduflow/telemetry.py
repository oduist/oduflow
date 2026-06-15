"""Anonymous usage telemetry — stdlib only, fire-and-forget."""

from __future__ import annotations

import json
import os
import threading
import uuid
from urllib.request import Request, urlopen

_ENDPOINT = "https://oduflow.dev/usage"
_TIMEOUT = 5
# Optional shared marker. Must match the server's USAGE_TELEMETRY_TOKEN. Only a
# speed bump against random scanners — this code is open source. Empty = disabled.
_TELEMETRY_TOKEN = "5ae286c3f9a162c178577659db75f78259b01942f12ec357929f73e0a222c707"


def _get_instance_id(etc_dir: str) -> tuple[str, bool]:
    """Read or create ``{etc_dir}/.instance_id``. Returns (id, is_first_run)."""
    try:
        path = os.path.join(etc_dir, ".instance_id")
        if os.path.isfile(path):
            with open(path) as f:
                return f.read().strip(), False
        os.makedirs(etc_dir, exist_ok=True)
        new_id = str(uuid.uuid4())
        with open(path, "w") as f:
            f.write(new_id)
        return new_id, True
    except Exception:
        return "", False


def _send_event(event: str, version: str, instance_id: str) -> None:
    """POST a telemetry event. Silently swallows all errors."""
    try:
        payload = json.dumps(
            {"event": event, "version": version, "instance_id": instance_id}
        ).encode()
        headers = {
            "Content-Type": "application/json",
            # A branded UA avoids edge bot-protection rules that block the
            # default ``Python-urllib/x.y`` signature (Cloudflare error 1010).
            "User-Agent": f"oduflow/{version}" if version else "oduflow",
        }
        if _TELEMETRY_TOKEN:
            headers["X-Oduflow-Telemetry"] = _TELEMETRY_TOKEN
        req = Request(
            _ENDPOINT,
            data=payload,
            headers=headers,
            method="POST",
        )
        urlopen(req, timeout=_TIMEOUT)
    except Exception:
        pass


def _fire_and_forget(event: str, version: str, instance_id: str) -> None:
    """Send a telemetry event in a daemon thread."""
    t = threading.Thread(
        target=_send_event, args=(event, version, instance_id), daemon=True
    )
    t.start()


def record_startup(etc_dir: str, version: str, disable_telemetry: bool) -> str:
    """Record a startup event. Returns the instance ID."""
    if disable_telemetry:
        return ""
    instance_id, is_first_run = _get_instance_id(etc_dir)
    if not instance_id:
        return ""
    if is_first_run:
        _fire_and_forget("first_run", version, instance_id)
    return instance_id


def record_env_created(
    instance_id: str, version: str, disable_telemetry: bool
) -> None:
    """Record an env_created event."""
    if disable_telemetry or not instance_id:
        return
    _fire_and_forget("env_created", version, instance_id)
