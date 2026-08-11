"""GitHub push webhooks → automatic production deploys.

``POST /api/webhooks/github`` (public path, no UI session): the request is
authenticated by its ``X-Hub-Signature-256`` HMAC instead. Each team's
secret is auto-generated with its first production and doubles as the team
resolver — the signature is verified against every team's secret and the
first match wins (team counts are single digits; the cost is nil).

On a ``push`` event the matching team's productions with the same
(normalized) repo URL + branch AND ``auto_update`` enabled are deployed as
durable JetStream operations via :func:`production_ops.update_production` —
with its automatic code rollback. Dev environments are NEVER touched by
webhooks.

Coalescing: at most one pending deploy per production is retained behind a
running deploy. The pending deploy pulls the latest desired state, so N rapid
pushes collapse into at most one running + one queued operation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any
from urllib.parse import urlparse

from oduflow import production_registry
from oduflow.locking import LockManager
from oduflow.operations import (
    get_operation_manager,
    register_operation,
    static_resource,
)
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")


def normalize_repo_url(url: str) -> str:
    """Canonical form for repo matching: lowercase host, no scheme, no
    credentials, no trailing .git, no trailing slash.

    Handles https URLs and scp-like ssh syntax (git@host:owner/repo.git) —
    GitHub payloads carry both forms.
    """
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url and ":" in url and "@" in url.split(":", 1)[0]:
        # scp-like: git@github.com:owner/repo.git
        userhost, _, path = url.partition(":")
        host = userhost.rpartition("@")[2]
        url = f"https://{host}/{path}"
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return f"{host}{path}"


def verify_signature(secret: str, body: bytes, signature_header: str) -> bool:
    """Validate GitHub's ``X-Hub-Signature-256: sha256=<hex>`` header."""
    if not secret or not signature_header:
        return False
    scheme, _, digest = signature_header.partition("=")
    if scheme.lower() != "sha256" or not digest:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, digest.strip().lower())


def resolve_team(
    settings: Settings, body: bytes, signature_header: str
) -> TeamSettings | None:
    """The team whose webhook secret signed this request (None = no match)."""
    for team in settings.teams.values():
        try:
            secret = production_registry.get_webhook_secret(team)
        except Exception:
            continue
        if secret and verify_signature(secret, body, signature_header):
            return team
    return None


def match_productions(team: TeamSettings, payload: dict[str, Any]) -> list[str]:
    """Production names to auto-deploy for a push payload.

    Match rule: same branch (payload ref ``refs/heads/{branch}``), same
    normalized repo URL (any of clone_url / html_url / ssh_url / url), and
    ``auto_update`` enabled.
    """
    ref = str(payload.get("ref", ""))
    if not ref.startswith("refs/heads/"):
        return []
    branch = ref[len("refs/heads/") :]
    repo = payload.get("repository") or {}
    push_urls = {
        normalize_repo_url(str(repo.get(key, "")))
        for key in ("clone_url", "html_url", "ssh_url", "url", "git_url")
    }
    push_urls.discard("")
    if not push_urls:
        return []

    names = []
    for name, record in production_registry.list_productions(team).items():
        if not record.get("auto_update"):
            continue
        if record.get("branch") != branch:
            continue
        if normalize_repo_url(str(record.get("repo_url", ""))) in push_urls:
            names.append(name)
    return sorted(names)


def _deploy_operation(name: str) -> dict[str, Any]:
    from oduflow.docker_ops import production_ops
    from oduflow.server import _get_settings, _resolve_team

    result = production_ops.update_production(
        _get_settings(), _resolve_team(None), name, trigger="webhook"
    )
    logger.info(
        "Webhook deploy of production '%s': %s",
        name,
        result.get("action"),
    )
    return result


register_operation(
    "webhook.production_deploy",
    _deploy_operation,
    static_resource("production", "name"),
)


def dispatch_push(
    settings: Settings,
    team: TeamSettings,
    locks: LockManager,
    payload: dict[str, Any],
) -> list[str]:
    """Queue durable deploys for a verified push; returns queued names."""
    del locks  # compatibility with the existing webhook call surface
    manager = get_operation_manager(settings)
    queued = []
    for name in match_productions(team, payload):
        ticket = manager.submit(
            "webhook.production_deploy",
            team.team_id,
            {"name": name},
            [f"production:{team.team_id}:{name}"],
            wait=False,
            coalesce_key=f"production:{name}",
        )
        if ticket["state"] in {"submitting", "queued"} and not ticket.get(
            "coalesced", False
        ):
            queued.append(name)
    return queued


def handle_github_event(
    settings: Settings,
    locks: LockManager,
    *,
    event: str,
    body: bytes,
    signature_header: str,
) -> tuple[int, dict[str, Any]]:
    """Process a GitHub webhook request; returns (http_status, json_body)."""
    if not settings.prod_enabled:
        return 404, {"ok": False, "error": "production hosting disabled"}
    team = resolve_team(settings, body, signature_header)
    if team is None:
        return 401, {"ok": False, "error": "invalid signature"}

    if event == "ping":
        return 200, {"ok": True, "pong": True}
    if event != "push":
        return 200, {"ok": True, "ignored": event}

    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 400, {"ok": False, "error": "malformed payload"}

    queued = dispatch_push(settings, team, locks, payload)
    return 202, {"ok": True, "queued": queued}
