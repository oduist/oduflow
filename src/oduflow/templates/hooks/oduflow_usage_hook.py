#!/usr/bin/env python3
"""Claude Code Stop hook: report LLM usage to an Oduflow environment.

On each Stop event Claude Code runs this script and passes the session info on
stdin. The script reads the session transcript, sums token usage per model and
the wall-clock duration, maps the current git branch to a per-environment
capability token (UID), and POSTs the result to the Oduflow dashboard's
``/api/llm-usage`` endpoint.

The model itself cannot measure its own token consumption, so the real numbers
come from the transcript here, not from the agent.

Everything is best-effort: any error exits 0 so the hook never disrupts the
Claude Code session.

Configuration lives in a JSON map at ``<repo>/.oduflow/usage-tokens.json``::

    {
      "dashboard_url": "https://oduflow.example.com",
      "tokens": { "<git-branch>": "<usage-uid>" }
    }

Environment overrides: ODUFLOW_USAGE_MAP (map file path),
ODUFLOW_DASHBOARD_URL (dashboard base URL), ODUFLOW_ENV (branch key to use
instead of the detected git branch).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import datetime

TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
)


def read_event() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}


def load_map(cwd: str) -> dict:
    path = os.environ.get("ODUFLOW_USAGE_MAP") or os.path.join(
        cwd, ".oduflow", "usage-tokens.json"
    )
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def slugify(branch: str) -> str:
    slug = branch.replace("/", "-")
    slug = re.sub(r"[^a-zA-Z0-9_-]", "", slug).lower()
    return slug[:63]


def current_branch(cwd: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def resolve_uid(tokens: dict, branch: str) -> str:
    if not isinstance(tokens, dict):
        return ""
    for key in (os.environ.get("ODUFLOW_ENV", ""), branch, slugify(branch)):
        if key and key in tokens:
            return str(tokens[key])
    return ""


def parse_timestamp(value) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def parse_transcript(path: str) -> tuple[dict, float]:
    """Sum usage per model and derive duration from message timestamps."""
    models: dict = {}
    first_ts = None
    last_ts = None
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                ts = parse_timestamp(event.get("timestamp"))
                if ts is not None:
                    first_ts = ts if first_ts is None else min(first_ts, ts)
                    last_ts = ts if last_ts is None else max(last_ts, ts)
                message = event.get("message") or {}
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                model = message.get("model") or "unknown"
                agg = models.setdefault(model, {k: 0 for k in TOKEN_KEYS})
                agg["input_tokens"] += int(usage.get("input_tokens") or 0)
                agg["output_tokens"] += int(usage.get("output_tokens") or 0)
                agg["cache_read_tokens"] += int(
                    usage.get("cache_read_input_tokens") or 0
                )
                agg["cache_creation_tokens"] += int(
                    usage.get("cache_creation_input_tokens") or 0
                )
    except Exception:
        return {}, 0.0
    duration = 0.0
    if first_ts is not None and last_ts is not None and last_ts >= first_ts:
        duration = round(last_ts - first_ts, 1)
    return models, duration


def main() -> None:
    event = read_event()
    cwd = event.get("cwd") or os.getcwd()
    session_id = event.get("session_id") or ""
    transcript = event.get("transcript_path") or ""
    if not session_id or not transcript:
        return

    cfg = load_map(cwd)
    tokens = cfg.get("tokens") or {}
    base_url = (
        os.environ.get("ODUFLOW_DASHBOARD_URL") or cfg.get("dashboard_url") or ""
    ).rstrip("/")
    branch = os.environ.get("ODUFLOW_ENV") or current_branch(cwd)
    uid = resolve_uid(tokens, branch)
    if not base_url or not uid:
        return

    models, duration = parse_transcript(transcript)
    if not models:
        return

    payload = json.dumps(
        {
            "session_id": session_id,
            "duration_seconds": duration,
            "models": models,
        }
    ).encode()
    request = urllib.request.Request(
        base_url + "/api/llm-usage",
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Oduflow-Env-Uid": uid,
        },
    )
    try:
        urllib.request.urlopen(request, timeout=10).read()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
