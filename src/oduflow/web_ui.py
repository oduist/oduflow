from __future__ import annotations
from typing import Any

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import pathlib
import re
import secrets
import socket
import threading
import time
from urllib.parse import quote, urlsplit

from itsdangerous import BadData, URLSafeTimedSerializer
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.requests import HTTPConnection, Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from starlette.applications import Starlette
from starlette.routing import BaseRoute, Route, WebSocketRoute
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

from collections.abc import Callable

from oduflow.docker_ops import (
    env_ops,
    odoo_ops,
    production_ops,
    service_ops,
    service_presets,
    system_ops,
    volume_ops,
)
from oduflow import activity
from oduflow import agent_config
from oduflow import connect_tokens
from oduflow import git_ops
from oduflow import import_tokens
from oduflow import production_registry
from oduflow.docker_ops.odoo_ops import get_environment_logs
from oduflow.docker_ops.stats import (
    get_container_stats,
    get_system_stats,
    read_storage_cache,
    refresh_env_storage,
    refresh_team_storage,
)
from oduflow.errors import BusyError, FlowError, NotFoundError
from oduflow.locking import LockManager
from oduflow.naming import validate_template_name
from oduflow.settings import Settings, TeamSettings
from oduflow.licensing import get_license_info, install_license_from_text

logger = logging.getLogger("oduflow")

_AUTH_USER = "admin"
_AUTH_COOKIE = "oduflow_ui_auth"
# Reachable without authentication: the login flow and static brand assets
# (so the login page can render its logo/favicon/fonts). /static/ serves only
# vetted extensions from the packaged assets dir (fonts, icons, xterm).
# /import-odoo.sh (the Odoo.sh client script) and the five import ingest
# endpoints authenticate with a short-lived import token, not the UI password,
# so they bypass Basic auth. They are listed as EXACT paths (never a prefix):
# a prefix like "/api/templates/import/" would also expose sibling routes such
# as /api/templates/{name}/delete with name="import" to unauthenticated calls.
# NOTE: /api/templates/import-token (which mints the token) is deliberately NOT
# public — it stays behind the UI login.
_PUBLIC_PATHS = frozenset(
    {
        "/login",
        "/logout",
        "/favicon.ico",
        "/logo.png",
        "/import-odoo.sh",
        "/api/templates/import/status",
        "/api/templates/import/manifest",
        "/api/templates/import/dump",
        "/api/templates/import/filestore",
        "/api/templates/import/addon",
        "/api/templates/import/addon-remote",
        "/api/templates/import/finalize",
        # Uptime-monitor endpoint: no auth, no secrets in the response.
        "/healthz",
        # GitHub can't carry a UI session; the handler verifies its own
        # X-Hub-Signature-256 HMAC against per-team webhook secrets.
        "/api/webhooks/github",
        # Cross-subdomain Connect As landing: reached on an ENV host (no
        # dashboard session there), authenticated by its own one-time token.
        "/oduflow-connect",
    }
)
_PUBLIC_PREFIXES = ("/static/",)
_SESSION_SALT = "oduflow.ui-auth.v1"
_SESSION_MAX_AGE = 7 * 24 * 3600  # 7 days
_SECRET_FILENAME = ".ui_session_secret"

# Cached per data dir (process-wide; the server runs against a single one).
_secrets_cache: dict[str, str] = {}
_signers: dict[str, URLSafeTimedSerializer] = {}


def _load_or_create_secret(data_dir: str) -> str:
    """Load the persistent UI-session signing secret from the data dir, creating
    it on first use. Persisting it means a server restart does not invalidate
    live sessions."""
    path = os.path.join(data_dir, _SECRET_FILENAME)
    try:
        existing = pathlib.Path(path).read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    os.makedirs(data_dir, exist_ok=True)
    secret = secrets.token_hex(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another worker created it between our read and write; use theirs.
        return pathlib.Path(path).read_text(encoding="utf-8").strip()
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(secret)
    return secret


def _get_secret(settings: Settings) -> str:
    """Return the cached persistent server-side signing secret for this server."""
    key = settings.base_data_dir or ""
    secret = _secrets_cache.get(key)
    if secret is None:
        secret = _load_or_create_secret(key) if key else secrets.token_hex(32)
        _secrets_cache[key] = secret
    return secret


def _get_signer(settings: Settings) -> URLSafeTimedSerializer:
    """Return the cached itsdangerous signer for this server's session cookies.

    The signing key is a persistent server-side secret (NOT the team password),
    so a leaked cookie reveals nothing about the password and cannot be
    brute-forced offline.
    """
    key = settings.base_data_dir or ""
    signer = _signers.get(key)
    if signer is None:
        signer = URLSafeTimedSerializer(_get_secret(settings), salt=_SESSION_SALT)
        _signers[key] = signer
    return signer


def _password_fingerprint(secret: str, ui_password: str) -> str:
    """A fingerprint of the team password, keyed by the server secret. Embedded
    in the token so that changing ui_password invalidates outstanding cookies
    immediately, while a leaked cookie still cannot be used to brute-force the
    password offline (the key is the server secret, not public)."""
    return hmac.new(
        secret.encode("utf-8"), ui_password.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _make_ui_token(team: "TeamSettings", settings: Settings) -> str:
    """Signed, timestamped session token for a team, stored as a cookie so
    WebSocket handshakes (which cannot send an Authorization header) can
    authenticate. Expires after `_SESSION_MAX_AGE`; carries a password
    fingerprint so a password change revokes it at once."""
    fingerprint = _password_fingerprint(_get_secret(settings), team.ui_password)
    return _get_signer(settings).dumps([team.team_id, fingerprint])


def _check_cookie_token(token: str, settings: Settings) -> "TeamSettings | None":
    """Validate a cookie token from `_make_ui_token`: verify its signature, that
    it is within `_SESSION_MAX_AGE`, and that the embedded password fingerprint
    still matches the team's current ui_password, then return its team."""
    if not token:
        return None
    try:
        data = _get_signer(settings).loads(token, max_age=_SESSION_MAX_AGE)
    except BadData:
        return None
    if not (isinstance(data, list) and len(data) == 2):
        return None
    team_id, fingerprint = data
    if not isinstance(team_id, str) or not isinstance(fingerprint, str):
        return None
    team = settings.teams.get(team_id)
    if not team or not team.ui_password:
        return None
    expected = _password_fingerprint(_get_secret(settings), team.ui_password)
    if not hmac.compare_digest(fingerprint, expected):
        return None
    return team


def _is_cross_origin(headers: Headers) -> bool:
    """Whether Origin/Referer mark this as a cross-site request (CSRF).

    Compares the Origin (or, absent that, Referer) host:port against the
    request's own Host header. Returns False when neither header is present —
    non-browser clients (curl, the import shell script) carry no ambient cookie
    and cannot be driven cross-site by a victim's browser, so there is nothing
    to forge."""
    host = headers.get("host", "")
    source = headers.get("origin", "") or headers.get("referer", "")
    if not source:
        return False
    try:
        netloc = urlsplit(source).netloc
    except Exception:
        return True
    return netloc != host


class BasicAuthMiddleware:
    def __init__(self, app: ASGIApp, get_settings: Callable[[], Settings]) -> None:
        self._app = app
        self._get_settings = get_settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        path = scope.get("path", "")
        if scope["type"] == "http" and (
            path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES)
        ):
            await self._app(scope, receive, send)
            return

        conn = HTTPConnection(scope)
        team = self._check_credentials(conn.headers.get("authorization", ""))
        if not team:
            token = conn.cookies.get(_AUTH_COOKIE)
            if token:
                team = _check_cookie_token(token, self._get_settings())
        if team:
            # CSRF: browsers authenticate with an ambient cookie, so reject
            # cross-site state-changing requests (unsafe HTTP methods and every
            # WebSocket handshake — cross-site WS hijacking targets the shell/SQL
            # terminals). SameSite=Strict is the first line; this is the
            # server-side backstop. Non-browser clients send no Origin/Referer
            # and are unaffected.
            method = scope.get("method", "")
            is_unsafe = scope["type"] == "websocket" or method in (
                "POST",
                "PUT",
                "PATCH",
                "DELETE",
            )
            if is_unsafe and _is_cross_origin(conn.headers):
                if scope["type"] == "websocket":
                    await WebSocket(scope, receive, send).close(code=1008)
                else:
                    forbidden: Response = JSONResponse(
                        {"ok": False, "error": "Cross-origin request blocked"},
                        status_code=403,
                    )
                    await forbidden(scope, receive, send)
                return
            scope.setdefault("state", {})["team"] = team
            await self._app(scope, receive, send)
            return

        # Unauthenticated. Browsers get a login page (no Basic dialog); API
        # clients and WebSocket handshakes get a machine-readable rejection.
        if scope["type"] == "websocket":
            ws = WebSocket(scope, receive, send)
            await ws.close(code=1008)
        elif path.startswith("/api/"):
            response: Response = JSONResponse(
                {"ok": False, "error": "Unauthorized"}, status_code=401
            )
            await response(scope, receive, send)
        else:
            response = RedirectResponse("/login", status_code=302)
            await response(scope, receive, send)

    def _check_credentials(self, auth_header: str) -> "TeamSettings | None":
        if not auth_header.startswith("Basic "):
            return None
        try:
            decoded = base64.b64decode(auth_header[6:]).decode()
            user, password = decoded.split(":", 1)
        except Exception:
            return None
        if user != _AUTH_USER:
            return None
        return self._get_settings().get_team_by_ui_password(password)


def _is_secure_request(request: Request) -> bool:
    """Whether the browser sees this connection as HTTPS, honouring a single
    X-Forwarded-Proto hop (first value, case-insensitive). Drives the cookie
    Secure attribute without guessing from routing_mode."""
    if request.url.scheme == "https":
        return True
    forwarded = request.headers.get("x-forwarded-proto", "")
    return forwarded.split(",")[0].strip().lower() == "https"


_TEMPLATE_DIR = pathlib.Path(__file__).resolve().parent / "templates"


def _render_login(error: str = "") -> str:
    """Render the login page, optionally with a server-controlled error banner."""
    page = (_TEMPLATE_DIR / "login.html").read_text(encoding="utf-8")
    # Escape the message so the banner stays safe even if a future caller passes
    # user-influenced text (today's callers pass static literals).
    banner = f'<div class="error">{html.escape(error)}</div>' if error else ""
    return page.replace("<!--ERROR-->", banner)


async def _read_login_password(request: Request) -> str:
    """Extract the password from a login POST, accepting either an HTML form
    (application/x-www-form-urlencoded) or a JSON body. Parsed directly so the
    UI needs no python-multipart dependency."""
    import urllib.parse

    body = await request.body()
    if "application/json" in request.headers.get("content-type", ""):
        try:
            data = json.loads(body or b"{}")
        except (ValueError, TypeError):
            return ""
        return str((data or {}).get("password") or "").strip()
    parsed = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
    return (parsed.get("password", [""])[0]).strip()


def _error_response(e: FlowError) -> JSONResponse:
    if isinstance(e, NotFoundError):
        status = 404
    elif isinstance(e, BusyError):
        status = 409
    else:
        status = 400
    return JSONResponse({"ok": False, "error": str(e)}, status_code=status)


def _normalize_extra_addons(raw_addons: object) -> dict[str, str]:
    if isinstance(raw_addons, dict):
        return raw_addons
    if isinstance(raw_addons, list):
        logger.warning(
            "Legacy list format for extra_addons (no branch info), skipping: %s",
            raw_addons,
        )
        return {}
    return {}


def _parse_extra_addons(raw: str) -> dict[str, str]:
    result = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            name, branch = item.split(":", 1)
            result[name.strip()] = branch.strip()
        else:
            raise ValueError(
                f"Extra addon '{item}' must include a branch (e.g. '{item}:19.0')."
            )
    return result


def _guide_title(filepath: str) -> str:
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("# "):
                    return line[2:].strip()
    except Exception:
        pass
    return os.path.basename(filepath).replace("_", " ").replace(".md", "").title()


def _acp_adapter_cmd(agent_type: str) -> list[str]:
    """Command that starts the ACP (Agent Client Protocol) adapter for the given
    agent inside the coder container. These speak JSON-RPC over stdio (unlike the
    interactive CLIs the terminal console runs), which the ``ws_agent_acp`` relay
    bridges to the browser chat. Bin names come from the packages installed in
    ``docker/agent/Dockerfile``."""
    if agent_type == "codex":
        return ["codex-acp"]
    return ["claude-code-acp"]


class _LoginRateLimiter:
    """Best-effort in-memory throttle for failed logins (issue #56).

    Tracks failed attempts per client IP in a sliding window; once the threshold
    is reached the IP is locked out for the rest of the window. A successful
    login clears the IP. The dashboard runs in a single uvicorn process, so an
    in-memory store is sufficient.
    """

    def __init__(self, max_attempts: int = 10, window_seconds: int = 300) -> None:
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> None:
        cutoff = now - self.window
        kept = [t for t in self._failures.get(key, []) if t > cutoff]
        if kept:
            self._failures[key] = kept
        else:
            self._failures.pop(key, None)

    def is_limited(self, key: str) -> bool:
        with self._lock:
            now = time.monotonic()
            self._prune(key, now)
            return len(self._failures.get(key, [])) >= self.max_attempts

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._failures.setdefault(key, []).append(now)
            self._prune(key, now)

    def clear(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


def _build_routes(
    get_settings: Callable[[], Settings],
    locks: LockManager,
) -> list[BaseRoute]:
    # Per-app so test apps and real deployments don't share failure counters.
    login_limiter = _LoginRateLimiter()

    def _set_session_cookie(
        response: Response, team: TeamSettings, request: Request
    ) -> None:
        response.set_cookie(
            _AUTH_COOKIE,
            _make_ui_token(team, get_settings()),
            max_age=_SESSION_MAX_AGE,
            httponly=True,
            samesite="strict",
            secure=_is_secure_request(request),
            path="/",
        )

    def dashboard(request: Request) -> HTMLResponse:
        html_path = _TEMPLATE_DIR / "dashboard.html"
        response = HTMLResponse(html_path.read_text(encoding="utf-8"))
        team = getattr(request.state, "team", None)
        if team is not None and team.ui_password:
            _set_session_cookie(response, team, request)
        return response

    async def login(request: Request) -> Response:
        settings = get_settings()
        # Auth disabled entirely -> the dashboard is open, no login needed.
        if not any(t.ui_password for t in settings.teams.values()):
            return RedirectResponse("/", status_code=302)
        # Already signed in (valid session cookie) -> go straight to the app.
        token = request.cookies.get(_AUTH_COOKIE)
        if token and _check_cookie_token(token, settings) is not None:
            return RedirectResponse("/", status_code=302)
        if request.method == "POST":
            client_ip = request.client.host if request.client else "unknown"
            if login_limiter.is_limited(client_ip):
                return HTMLResponse(
                    _render_login("Too many failed attempts. Try again later."),
                    status_code=429,
                )
            password = await _read_login_password(request)
            team = settings.get_team_by_ui_password(password) if password else None
            if team is not None:
                login_limiter.clear(client_ip)
                response: Response = RedirectResponse("/", status_code=303)
                _set_session_cookie(response, team, request)
                return response
            login_limiter.record_failure(client_ip)
            return HTMLResponse(_render_login("Invalid password."), status_code=401)
        return HTMLResponse(_render_login())

    def logout(request: Request) -> RedirectResponse:
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(_AUTH_COOKIE, path="/", samesite="strict")
        return response

    def favicon(request: Request) -> Response:
        ico_path = _TEMPLATE_DIR / "favicon.ico"
        return Response(ico_path.read_bytes(), media_type="image/x-icon")

    def logo(request: Request) -> Response:
        logo_path = _TEMPLATE_DIR / "logo.png"
        return Response(logo_path.read_bytes(), media_type="image/png")

    _STATIC_MEDIA_TYPES = {
        ".css": "text/css",
        ".js": "application/javascript",
        ".woff2": "font/woff2",
        ".png": "image/png",
    }

    def static_file(request: Request) -> Response:
        filename = request.path_params["filename"]
        static_dir = (_TEMPLATE_DIR / "static").resolve()
        file_path = (static_dir / filename).resolve()
        media_type = _STATIC_MEDIA_TYPES.get(file_path.suffix)
        if (
            not file_path.is_relative_to(static_dir)
            or media_type is None
            or not file_path.is_file()
        ):
            return Response("Not found", status_code=404)
        return Response(
            file_path.read_bytes(),
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    def _get_ui_team(request: HTTPConnection) -> TeamSettings:
        """Get the team from request state (set by auth middleware) or fallback.

        Accepts any Starlette connection (HTTP ``Request`` or ``WebSocket``);
        both carry the ``state`` populated by the auth middleware.
        """
        if hasattr(request.state, "team"):
            team: TeamSettings = request.state.team
            return team
        settings = get_settings()
        # When auth is enforced (any team has a ui_password), the middleware
        # always populates request.state.team for non-public paths. Reaching
        # here means the request bypassed auth — default-deny instead of silently
        # acting as team "1" (tenant-isolation hazard). The single-team fallback
        # is only for the open (auth-disabled) server.
        if any(t.ui_password for t in settings.teams.values()):
            raise HTTPException(status_code=401, detail="Unauthorized")
        if len(settings.teams) == 1:
            return next(iter(settings.teams.values()))
        return settings.get_team("1")

    def api_list(request: Request) -> JSONResponse:
        try:
            settings = get_settings()
            team = _get_ui_team(request)
            envs = env_ops.list_environments(settings, team)
            return JSONResponse({"ok": True, "environments": envs})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_list")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_start(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        team = _get_ui_team(request)
        try:
            locks.acquire_env(branch, team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            result = env_ops.start_environment(get_settings(), branch, team)
            activity.mark_started(team, branch)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_start")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(branch)

    def api_stop(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        team = _get_ui_team(request)
        try:
            locks.acquire_env(branch, team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            settings = get_settings()
            result = env_ops.stop_environment(settings, team, branch)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_stop")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(branch)

    def api_restart(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        team = _get_ui_team(request)
        try:
            locks.acquire_env(branch, team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            result = env_ops.restart_environment(get_settings(), branch, team)
            activity.mark_started(team, branch)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_restart")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(branch)

    def api_sync(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        team = _get_ui_team(request)
        try:
            locks.acquire_env(branch, team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            settings = get_settings()
            activity.touch(team, branch)
            result = env_ops.pull_environment(settings, team, branch)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_sync")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(branch)

    def api_delete(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        team = _get_ui_team(request)
        try:
            locks.acquire_env(branch, team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            settings = get_settings()
            env_ops.delete_environment(settings, team, branch)
            return JSONResponse({"ok": True, "result": {"deleted": branch}})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_delete")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(branch)

    async def api_update(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        team = _get_ui_team(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        env_vars_raw = (body.get("env_vars") or "").strip() if body else ""
        odoo_image = (body.get("odoo_image") or "").strip() if body else ""
        env_override = None
        if env_vars_raw:
            import re

            env_override = dict(
                item.split("=", 1)
                for item in re.split(r"[\n,]+", env_vars_raw)
                if "=" in item
            )
        try:
            locks.acquire_env(branch, team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            settings = get_settings()
            activity.touch(team, branch)
            result = env_ops.update_environment(
                settings,
                team,
                branch,
                env_override=env_override,
                image_override=odoo_image or None,
            )
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_update")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(branch)

    def api_recreate(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        team = _get_ui_team(request)
        try:
            locks.acquire_env(branch, team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            import docker as _docker
            from oduflow.docker_ops.client import get_client as _get_client
            from oduflow.naming import get_resource_name

            settings = get_settings()
            activity.touch(team, branch)
            client = _get_client()
            odoo_container_name = get_resource_name(
                branch, "odoo", settings.prefix, team.team_id
            )
            try:
                container = client.containers.get(odoo_container_name)
                labels = container.labels
            except _docker.errors.NotFound:
                return _error_response(
                    NotFoundError(f"Environment '{branch}' not found.")
                )

            repo_url = labels.get(settings.repo_label, "")
            odoo_image = labels.get(settings.image_label, "")
            template_raw = labels.get("oduflow.template", "")
            template_name = (
                template_raw if template_raw and template_raw != "none" else None
            )
            extra_addons_raw = labels.get("oduflow.extra_addons", "{}")
            extra_addons = json.loads(extra_addons_raw) or None
            git_user = labels.get("oduflow.git_user", "")
            env_vars = json.loads(labels.get("oduflow.env_vars", "{}")) or None
            local_path = labels.get("oduflow.local_path", "")

            env_ops.delete_environment(settings, team, branch)
            result = env_ops.create_environment(
                settings,
                team,
                branch,
                repo_url,
                odoo_image,
                template_name=template_name,
                extra_addons=extra_addons,
                git_user=git_user,
                env_vars=env_vars,
                local_path=local_path,
            )
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_recreate")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(branch)

    async def api_save_as_template(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        team = _get_ui_team(request)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": "Invalid JSON body"}, status_code=400
            )
        template_name = str((body or {}).get("template_name") or "").strip()
        if not template_name:
            return JSONResponse(
                {"ok": False, "error": "template_name is required"}, status_code=400
            )
        try:
            validate_template_name(template_name)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        # Team lock (not just the env): publishing can remount other envs' overlay
        # filestores, so it must serialize against the whole team like the MCP tool.
        try:
            locks.acquire_team(team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            activity.touch(team, branch)
            # No overwrite from the UI: publishing over an existing template is a
            # deliberate re-baseline reserved for the MCP tool, so a duplicate name
            # here raises ConflictError (surfaced to the client by _error_response).
            result = system_ops.publish_env_as_template(
                get_settings(), team, branch, template_name=template_name
            )
            return JSONResponse(
                {
                    "ok": True,
                    "result": {
                        "status": result.get("status"),
                        "env_name": result.get("env_name"),
                        "template_db": result.get("template_db"),
                        "affected_envs": result.get("affected_envs", []),
                        "remount_failures": result.get("remount_failures", []),
                    },
                }
            )
        except ValueError as e:  # invalid template name
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_save_as_template")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_team(team.team_id)

    async def api_create(request: Request) -> JSONResponse:
        import json as _json

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                {"ok": False, "error": "Invalid JSON body."}, status_code=400
            )

        env_name = (body.get("env_name") or "").strip()
        repo_url = (body.get("repo_url") or "").strip()
        odoo_image = (body.get("odoo_image") or "").strip()
        git_user = (body.get("git_user") or "").strip()
        template_name_raw = (body.get("template_name") or "").strip()
        extra_addons_raw = body.get("extra_addons")
        auto_install_raw = (body.get("auto_install_modules") or "").strip()
        env_vars_raw = (body.get("env_vars") or "").strip()
        env_vars = None
        if env_vars_raw:
            import re

            env_vars = dict(
                item.split("=", 1)
                for item in re.split(r"[\n,]+", env_vars_raw)
                if "=" in item
            )
        if not env_name:
            return JSONResponse(
                {"ok": False, "error": "env_name is required."},
                status_code=400,
            )
        from oduflow.naming import validate_env_name

        try:
            env_name = validate_env_name(env_name)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

        team = _get_ui_team(request)
        # Acquire the env lock in its own try so a BusyError returns WITHOUT
        # entering the try/finally below — otherwise the finally would release a
        # lock that another in-flight request holds (issue #42).
        try:
            locks.acquire_env(env_name, team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            resolved_template: str | None
            if not template_name_raw or template_name_raw.lower() == "none":
                resolved_template = None
            else:
                resolved_template = template_name_raw

            # Load metadata from template
            settings = get_settings()
            extra_dict = None
            if isinstance(extra_addons_raw, dict):
                extra_dict = extra_addons_raw or None
            elif isinstance(extra_addons_raw, str) and extra_addons_raw.strip():
                extra_dict = _parse_extra_addons(extra_addons_raw.strip()) or None
            local_path_from_meta = ""
            if resolved_template:
                metadata_path = team.get_template_metadata_path(resolved_template)
                if os.path.isfile(metadata_path):
                    with open(metadata_path) as f:
                        metadata = _json.load(f)
                    if not repo_url:
                        repo_url = metadata.get("repo_url", "")
                    if not odoo_image:
                        odoo_image = metadata.get("odoo_image", "")
                    if not git_user:
                        git_user = metadata.get("git_user", "")
                    if extra_dict is None:
                        raw = metadata.get("extra_addons")
                        if raw:
                            extra_dict = _normalize_extra_addons(raw) or None
                    if not auto_install_raw:
                        auto_install_raw = metadata.get("auto_install_modules", "")
                    if not repo_url and metadata.get("local_path"):
                        if settings.allow_local_path:
                            local_path_from_meta = metadata["local_path"]
                        else:
                            return JSONResponse(
                                {
                                    "ok": False,
                                    "error": (
                                        f"Template '{resolved_template}' was saved "
                                        "from a live-mounted environment and has no "
                                        "repo_url. Set allow_local_path = true in "
                                        "oduflow.toml [server], or provide "
                                        "repo_url explicitly."
                                    ),
                                },
                                status_code=400,
                            )

            if not repo_url and not local_path_from_meta:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "repo_url is required (not found in template metadata either).",
                    },
                    status_code=400,
                )
            if not odoo_image:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "odoo_image is required (not found in template metadata either).",
                    },
                    status_code=400,
                )
            auto_install_list = (
                [m.strip() for m in auto_install_raw.split(",") if m.strip()]
                if auto_install_raw
                else []
            )
            result = env_ops.create_environment(
                settings,
                team,
                env_name,
                repo_url,
                odoo_image,
                template_name=resolved_template,
                extra_addons=extra_dict,
                git_user=git_user,
                auto_install_modules=auto_install_list or None,
                env_vars=env_vars,
                local_path=local_path_from_meta,
            )
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            # FlowError is an "expected" business error, but for create it is the
            # only record of WHY the environment failed to build (overlay mount,
            # disk/quota, git auth, …): the response often never reaches the
            # browser on multi-minute creates, so log it here to keep the reason
            # in the server journal.
            logger.warning("create_environment failed for %s: %s", env_name, e)
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_create")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(env_name)

    def api_logs(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            n = int(request.query_params.get("n", "200"))
        except (ValueError, TypeError):
            n = 200
        container = request.query_params.get("container", "")
        try:
            logs = get_environment_logs(
                get_settings(),
                branch,
                n_lines=n,
                container_name=container,
                team=_get_ui_team(request),
            )
            return JSONResponse({"ok": True, "logs": logs})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_logs")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_stats(request: Request) -> JSONResponse:
        try:
            settings = get_settings()
            team = _get_ui_team(request)
            containers = get_container_stats(settings, team)
            system = get_system_stats()
            # Cached only — one small JSON read; recomputing storage is an
            # explicit action (the refresh button / the refresh endpoints).
            storage = read_storage_cache(team)
            return JSONResponse(
                {
                    "ok": True,
                    "containers": containers,
                    "system": system,
                    "storage": storage,
                }
            )
        except Exception:
            logger.exception("Unexpected error in api_stats")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_storage_refresh(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            entry = refresh_env_storage(get_settings(), _get_ui_team(request), branch)
            return JSONResponse({"ok": True, "storage": entry})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_storage_refresh")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_usage(request: Request) -> JSONResponse:
        """Cached per-team usage + quotas — the read side for external
        billing/quota tooling (see api_usage_refresh for recomputation)."""
        try:
            team = _get_ui_team(request)
            return JSONResponse(
                {
                    "ok": True,
                    "team_id": team.team_id,
                    "quotas": {
                        "db_quota_gb": team.db_quota_gb,
                        "disk_quota_gb": team.disk_quota_gb,
                    },
                    "usage": read_storage_cache(team),
                }
            )
        except Exception:
            logger.exception("Unexpected error in api_usage")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_usage_refresh(request: Request) -> JSONResponse:
        """Recompute storage for every environment plus team totals. Heavy
        (walks every workspace); meant for operator tooling on a schedule."""
        try:
            team = _get_ui_team(request)
            usage = refresh_team_storage(get_settings(), team)
            return JSONResponse(
                {
                    "ok": True,
                    "team_id": team.team_id,
                    "quotas": {
                        "db_quota_gb": team.db_quota_gb,
                        "disk_quota_gb": team.disk_quota_gb,
                    },
                    "usage": usage,
                }
            )
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_usage_refresh")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_templates(request: Request) -> JSONResponse:
        try:
            templates = system_ops.list_templates(get_settings(), _get_ui_team(request))
            return JSONResponse({"ok": True, "templates": templates})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_templates")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_template_delete(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        try:
            locks.acquire_team(team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            result = system_ops.delete_template(get_settings(), team, name)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_template_delete")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_team(team.team_id)

    async def api_template_rename(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": "Invalid JSON body"}, status_code=400
            )
        new_name = str((body or {}).get("new_name") or "").strip()
        if not new_name:
            return JSONResponse(
                {"ok": False, "error": "new_name is required"}, status_code=400
            )
        try:
            locks.acquire_team(team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            result = system_ops.rename_template(get_settings(), team, name, new_name)
            return JSONResponse({"ok": True, "result": result})
        except ValueError as e:  # invalid template name
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_template_rename")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_team(team.team_id)

    # --- Import from Odoo.sh (push-based template ingest) ------------------

    def _import_token_value(request: Request) -> str:
        """Read the import token from the Authorization: Bearer header only.

        A ``?token=`` query param is deliberately NOT accepted: these endpoints
        bypass Basic auth, so the token is the sole credential, and tokens in
        URLs leak into reverse-proxy/CDN access logs and Referer headers. The
        official ``import-odoo.sh`` client always sends the Bearer header."""
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            return auth[len("Bearer ") :].strip()
        return ""

    def _resolve_import_token(
        request: Request,
    ) -> tuple[TeamSettings, dict[str, object]]:
        return import_tokens.load_token(get_settings(), _import_token_value(request))

    def _import_progress(team: TeamSettings, template_name: str) -> dict[str, object]:
        """Derive upload progress from what sits in the import STAGING dir (not
        the token, and never the live template). Disk-derived progress makes a
        re-run resumable even with a fresh token after the old one expired;
        reading staging (not the template) means importing over an existing
        template re-uploads everything instead of silently "resuming" from the
        old template's files."""
        staging = team.get_import_staging_dir(template_name)  # validates name
        manifest = os.path.isfile(os.path.join(staging, "metadata.json"))
        dump = os.path.isfile(os.path.join(staging, "dump.sql.gz"))
        # dump_bytes lets a chunked upload resume mid-file: bytes already on the
        # server, whether the dump is complete (final file) or partial (.part).
        if dump:
            dump_bytes = os.path.getsize(os.path.join(staging, "dump.sql.gz"))
        else:
            dpart = os.path.join(staging, "dump.sql.gz.part")
            dump_bytes = os.path.getsize(dpart) if os.path.isfile(dpart) else 0
        fs_dir = os.path.join(staging, "filestore")
        chunks: list[str] = []
        if os.path.isdir(fs_dir):
            # A chunk directory appears only after its tar was extracted in
            # full and atomically renamed into place (extract_filestore_chunk),
            # so its presence means that chunk is complete.
            for entry in os.listdir(fs_dir):
                if (
                    re.fullmatch(r"[0-9a-f]{2}", entry) or entry == "checklist"
                ) and os.path.isdir(os.path.join(fs_dir, entry)):
                    chunks.append(entry)
        # File-backed addons (Enterprise/Themes/private extras): a dir under
        # addons/ appears only after its tar fully extracted (extract_addon_dir).
        addons_dir = os.path.join(staging, "addons")
        addons: list[str] = []
        if os.path.isdir(addons_dir):
            for entry in os.listdir(addons_dir):
                if not entry.startswith(".") and os.path.isdir(
                    os.path.join(addons_dir, entry)
                ):
                    addons.append(entry)
        # Remote addons (cloned server-side) are announced in addons.json only.
        remote_addons: list[str] = []
        addons_manifest = os.path.join(staging, "addons.json")
        if os.path.isfile(addons_manifest):
            try:
                with open(addons_manifest) as f:
                    for e in json.load(f):
                        if (
                            isinstance(e, dict)
                            and e.get("kind") == "remote"
                            and e.get("name")
                        ):
                            remote_addons.append(str(e["name"]))
            except (OSError, ValueError):
                pass
        # Partial addon tars (chunked upload in progress): received bytes per
        # addon, so a resumed run can restart an incomplete one.
        addon_bytes: dict[str, int] = {}
        if os.path.isdir(staging):
            for entry in os.listdir(staging):
                if entry.startswith(".addon_") and entry.endswith(".tar.part"):
                    nm = entry[len(".addon_") : -len(".tar.part")]
                    addon_bytes[nm] = os.path.getsize(os.path.join(staging, entry))
        return {
            "manifest": manifest,
            "dump": dump,
            "dump_bytes": dump_bytes,
            "filestore_chunks": sorted(chunks),
            "addons": sorted(addons),
            "addon_bytes": addon_bytes,
            "remote_addons": sorted(set(remote_addons)),
        }

    def import_odoo_script(request: Request) -> Response:
        script = (_TEMPLATE_DIR / "import-odoo.sh").read_text(encoding="utf-8")
        return Response(
            script,
            media_type="text/x-shellscript; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    async def api_import_token(request: Request) -> JSONResponse:
        """Mint a short-lived import token for a target template (UI-authed).

        Optional booleans ``with_enterprise`` / ``with_themes`` /
        ``with_extra_addons`` append the matching ``--with-*`` flags to the
        returned command so the checkboxes in the import dialog drive what the
        Odoo.sh client downloads.
        """
        team = _get_ui_team(request)
        with_flags: list[str] = []
        try:
            body = await request.body()
            data = json.loads(body or b"{}") or {}
            template_name = str(data.get("template_name") or "").strip()
            if not template_name:
                return JSONResponse(
                    {"ok": False, "error": "template_name is required"},
                    status_code=400,
                )
            from oduflow.naming import validate_template_name

            validate_template_name(template_name)
            if data.get("with_enterprise"):
                with_flags.append("--with-enterprise")
            if data.get("with_themes"):
                with_flags.append("--with-themes")
            if data.get("with_extra_addons"):
                with_flags.append("--with-extra-addons")
            record = import_tokens.create_token(team, template_name)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception:
            logger.exception("Unexpected error in api_import_token")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

        base = str(request.base_url).rstrip("/")
        # Behind a TLS-terminating proxy request.base_url is http://; honour
        # X-Forwarded-Proto so the pasted command never sends the Bearer token
        # over plaintext (and -L follows any residual http->https redirect).
        if _is_secure_request(request) and base.startswith("http://"):
            base = "https://" + base[len("http://") :]
        command = (
            f"curl -sSfL {base}/import-odoo.sh | bash -s -- "
            f"--server {base} --token {record['token']}"
        )
        if with_flags:
            command += " " + " ".join(with_flags)
        return JSONResponse(
            {
                "ok": True,
                "token": record["token"],
                "template_name": template_name,
                "expires_at": record["expires_at"],
                "command": command,
            }
        )

    def api_import_status(request: Request) -> JSONResponse:
        try:
            team, record = _resolve_import_token(request)
            progress = _import_progress(team, str(record["template_name"]))
        except FlowError as e:
            return _error_response(e)
        except ValueError as e:  # invalid template name
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse(
            {
                "ok": True,
                "template_name": record["template_name"],
                "progress": progress,
                "expires_at": record["expires_at"],
            }
        )

    async def api_import_manifest(request: Request) -> JSONResponse:
        try:
            team, record = _resolve_import_token(request)
        except FlowError as e:
            return _error_response(e)
        body = await request.body()
        try:
            manifest = json.loads(body or b"{}")
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": "Invalid manifest JSON"}, status_code=400
            )

        template_name = str(record["template_name"])
        branch = str(manifest.get("odoo_branch") or "").strip()  # e.g. "18.0"
        metadata = {
            "odoo_image": f"odoo:{branch}" if branch else "",
            "repo_url": "",
            "source": "odoo.sh",
            "source_db": manifest.get("name", ""),
            "source_revision": manifest.get("revision", ""),
            "source_repository": manifest.get("repository", ""),
            "odoo_version": branch,
            "modules": manifest.get("installed_modules", {}),
            "backup_datetime_utc": manifest.get("backup_datetime_utc", ""),
        }
        try:
            staging = team.get_import_staging_dir(template_name)
            os.makedirs(staging, exist_ok=True)
            with open(os.path.join(staging, "metadata.json"), "w") as f:
                json.dump(metadata, f, indent=2)
        except ValueError as e:  # invalid template name
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True})

    async def _receive_offset_chunk(
        request: Request, part_path: str, offset: int
    ) -> tuple[str, int]:
        """Append a byte-range chunk to ``part_path`` at ``offset`` (in order).

        Chunked uploads work around proxies that cap request body size (e.g.
        Cloudflare's 100 MB limit) by splitting a large payload into parts the
        server concatenates. Returns ``(state, size)`` where state is
        ``"written"`` (appended), ``"duplicate"`` (offset already covered — a
        resent part, discarded) or ``"gap"`` (offset beyond current size —
        caller should 409). ``offset == 0`` truncates/starts fresh. The request
        body is always drained so the connection closes cleanly.
        """
        cur = os.path.getsize(part_path) if os.path.exists(part_path) else 0
        if offset > cur:
            async for _ in request.stream():
                pass
            return "gap", cur
        if 0 < offset < cur:
            async for _ in request.stream():
                pass
            return "duplicate", cur
        mode = "wb" if offset == 0 else "ab"
        with open(part_path, mode) as f:
            async for data in request.stream():
                f.write(data)
        return "written", os.path.getsize(part_path)

    async def api_import_dump(request: Request) -> JSONResponse:
        try:
            team, record = _resolve_import_token(request)
        except FlowError as e:
            return _error_response(e)
        template_name = str(record["template_name"])
        try:
            staging = team.get_import_staging_dir(template_name)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        os.makedirs(staging, exist_ok=True)
        dest = os.path.join(staging, "dump.sql.gz")
        part = f"{dest}.part"

        offset_raw = request.query_params.get("offset")
        if offset_raw is None:
            # Legacy single-shot upload (no chunking): stream straight through.
            try:
                with open(part, "wb") as f:
                    async for chunk in request.stream():
                        f.write(chunk)
                os.replace(part, dest)
            except Exception:
                if os.path.exists(part):
                    os.remove(part)
                logger.exception("Failed to receive dump for %s", template_name)
                return JSONResponse(
                    {"ok": False, "error": "Internal server error."}, status_code=500
                )
            return JSONResponse({"ok": True})

        # Chunked upload: append at `offset`, complete when the part hits `total`.
        if os.path.isfile(dest):  # already assembled — retry after completion
            return JSONResponse(
                {"ok": True, "received": os.path.getsize(dest), "complete": True}
            )
        try:
            offset = int(offset_raw)
            total = int(request.query_params.get("total", "0"))
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": "offset and total must be integers"},
                status_code=400,
            )
        try:
            state, size = await _receive_offset_chunk(request, part, offset)
        except Exception:
            logger.exception("Failed to receive dump chunk for %s", template_name)
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        if state == "gap":
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"unexpected offset {offset}, expected {size}",
                    "expected": size,
                },
                status_code=409,
            )
        complete = total > 0 and size >= total
        if complete:
            os.replace(part, dest)
        return JSONResponse({"ok": True, "received": size, "complete": complete})

    async def api_import_filestore(request: Request) -> JSONResponse:
        try:
            team, record = _resolve_import_token(request)
        except FlowError as e:
            return _error_response(e)
        chunk = request.query_params.get("chunk", "").strip()
        if not re.fullmatch(r"[0-9a-f]{2}|checklist", chunk):
            return JSONResponse(
                {"ok": False, "error": f"Invalid filestore chunk '{chunk}'"},
                status_code=400,
            )
        template_name = str(record["template_name"])
        try:
            staging = team.get_import_staging_dir(template_name)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        fs_dir = os.path.join(staging, "filestore")
        os.makedirs(staging, exist_ok=True)
        tmp_tar = os.path.join(staging, f".chunk_{chunk}.tar")
        try:
            with open(tmp_tar, "wb") as f:
                async for data in request.stream():
                    f.write(data)
            # Atomic: the chunk dir appears in staging only after the whole tar
            # extracted, so a truncated upload is never mistaken for a complete
            # chunk on resume.
            system_ops.extract_filestore_chunk(tmp_tar, fs_dir, chunk)
        except Exception:
            logger.exception(
                "Failed to receive filestore chunk %s for %s", chunk, template_name
            )
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            if os.path.exists(tmp_tar):
                os.remove(tmp_tar)
        return JSONResponse({"ok": True})

    _ADDON_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,63}$")

    def _record_import_addon(
        team: TeamSettings, template_name: str, entry: dict[str, object]
    ) -> None:
        """Append/replace an addon descriptor in the staging addons.json.

        Keyed by name so a resumed upload updates rather than duplicates.
        """
        staging = team.get_import_staging_dir(template_name)  # validates name
        os.makedirs(staging, exist_ok=True)
        path = os.path.join(staging, "addons.json")
        entries: list[Any] = []
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    loaded = json.load(f)
                if isinstance(loaded, list):
                    entries = [
                        e
                        for e in loaded
                        if isinstance(e, dict) and e.get("name") != entry["name"]
                    ]
            except (OSError, ValueError):
                entries = []
        entries.append(entry)
        with open(path, "w") as f:
            json.dump(entries, f, indent=2)

    async def api_import_addon(request: Request) -> JSONResponse:
        """Receive one addon directory (tar stream) that becomes a local
        (remote-less) extra-addons repo — Enterprise, Themes or a private extra
        repo that cannot be cloned."""
        try:
            team, record = _resolve_import_token(request)
        except FlowError as e:
            return _error_response(e)
        name = request.query_params.get("name", "").strip()
        if not _ADDON_NAME_RE.match(name):
            return JSONResponse(
                {"ok": False, "error": f"Invalid addon name '{name}'"},
                status_code=400,
            )
        branch = request.query_params.get("branch", "").strip()
        category = request.query_params.get("category", "").strip()
        template_name = str(record["template_name"])
        try:
            staging = team.get_import_staging_dir(template_name)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        addons_dir = os.path.join(staging, "addons")
        os.makedirs(staging, exist_ok=True)
        final_tar = os.path.join(staging, f".addon_{name}.tar")
        part = f"{final_tar}.part"

        def _finish() -> None:
            """Extract the assembled tar and record the addon; clean up."""
            try:
                system_ops.extract_addon_dir(final_tar, addons_dir, name)
                _record_import_addon(
                    team,
                    template_name,
                    {
                        "name": name,
                        "kind": "local",
                        "branch": branch,
                        "origin_url": "",
                        "category": category,
                    },
                )
            finally:
                if os.path.exists(final_tar):
                    os.remove(final_tar)

        offset_raw = request.query_params.get("offset")
        if offset_raw is None:
            # Legacy single-shot upload (no chunking).
            try:
                with open(part, "wb") as f:
                    async for data in request.stream():
                        f.write(data)
                os.replace(part, final_tar)
                _finish()
            except Exception:
                logger.exception(
                    "Failed to receive addon %s for %s", name, template_name
                )
                return JSONResponse(
                    {"ok": False, "error": "Internal server error."}, status_code=500
                )
            finally:
                if os.path.exists(part):
                    os.remove(part)
            return JSONResponse({"ok": True})

        # Chunked upload: append at `offset`, extract when the part hits `total`.
        if os.path.isdir(os.path.join(addons_dir, name)):  # already extracted
            return JSONResponse({"ok": True, "complete": True})
        try:
            offset = int(offset_raw)
            total = int(request.query_params.get("total", "0"))
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": "offset and total must be integers"},
                status_code=400,
            )
        try:
            state, size = await _receive_offset_chunk(request, part, offset)
        except Exception:
            logger.exception(
                "Failed to receive addon chunk %s for %s", name, template_name
            )
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        if state == "gap":
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"unexpected offset {offset}, expected {size}",
                    "expected": size,
                },
                status_code=409,
            )
        complete = total > 0 and size >= total
        if complete:
            try:
                os.replace(part, final_tar)
                _finish()
            except Exception:
                logger.exception(
                    "Failed to finalize addon %s for %s", name, template_name
                )
                return JSONResponse(
                    {"ok": False, "error": "Internal server error."}, status_code=500
                )
        return JSONResponse({"ok": True, "received": size, "complete": complete})

    async def api_import_addon_remote(request: Request) -> JSONResponse:
        """Announce a reachable extra repo (no files uploaded) — it is cloned
        from its origin at finalize so it stays updatable via Oduflow."""
        try:
            team, record = _resolve_import_token(request)
        except FlowError as e:
            return _error_response(e)
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": "Invalid JSON body"}, status_code=400
            )
        name = str((body or {}).get("name") or "").strip()
        if not _ADDON_NAME_RE.match(name):
            return JSONResponse(
                {"ok": False, "error": f"Invalid addon name '{name}'"},
                status_code=400,
            )
        origin_url = str((body or {}).get("origin_url") or "").strip()
        if not origin_url:
            return JSONResponse(
                {"ok": False, "error": "origin_url is required"}, status_code=400
            )
        branch = str((body or {}).get("branch") or "").strip()
        template_name = str(record["template_name"])
        try:
            _record_import_addon(
                team,
                template_name,
                {
                    "name": name,
                    "kind": "remote",
                    "branch": branch,
                    "origin_url": origin_url,
                    "category": "extra",
                },
            )
        except ValueError as e:  # invalid template name
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        return JSONResponse({"ok": True})

    def api_import_finalize(request: Request) -> JSONResponse:
        try:
            team, record = _resolve_import_token(request)
        except FlowError as e:
            return _error_response(e)
        template_name = str(record["template_name"])
        try:
            progress = _import_progress(team, template_name)
        except ValueError as e:  # invalid template name
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        if not progress["manifest"] or not progress["dump"]:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Manifest and SQL dump must be uploaded before finalize.",
                },
                status_code=400,
            )
        try:
            locks.acquire_team(team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            result = system_ops.finalize_imported_template(
                get_settings(),
                team,
                template_name,
                staging_dir=team.get_import_staging_dir(template_name),
            )
            import_tokens.invalidate(team, str(record["token"]))
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_import_finalize")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_team(team.team_id)

    def api_services(request: Request) -> JSONResponse:
        try:
            services = service_ops.list_services(get_settings(), _get_ui_team(request))
            return JSONResponse({"ok": True, "services": services})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_services")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_service_create(request: Request) -> JSONResponse:
        team = _get_ui_team(request)
        try:
            locks.acquire_team(team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            body = await request.json()
            name = (body.get("name") or "").strip()
            image = (body.get("image") or "").strip()
            port = body.get("port")
            routes = body.get("routes")
            hostname = (body.get("hostname") or "").strip() or None
            env_vars_raw = (body.get("env_vars") or "").strip()
            host_mode = bool(body.get("host_mode", False))
            if not name or not image or (not port and not routes):
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "name, image and either port or routes are required.",
                    },
                    status_code=400,
                )
            env_vars = None
            if env_vars_raw:
                import re

                env_vars = dict(
                    item.split("=", 1)
                    for item in re.split(r"[\n,]+", env_vars_raw)
                    if "=" in item
                )
            volumes_raw = (body.get("volumes") or "").strip()
            parsed_volumes = (
                volume_ops.parse_volume_mounts(volumes_raw) if volumes_raw else None
            )
            privileged = bool(body.get("privileged", False))
            net_admin = bool(body.get("net_admin", False))
            cap_add = ["NET_ADMIN"] if net_admin else None
            result = service_ops.create_service(
                get_settings(),
                team,
                name,
                image,
                int(port) if port else None,
                hostname=hostname,
                env_vars=env_vars,
                host_mode=host_mode,
                volumes=parsed_volumes,
                cap_add=cap_add,
                privileged=privileged,
                routes=routes,
            )
            return JSONResponse({"ok": True, "result": result})
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_service_create")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_team(team.team_id)

    async def api_service_update(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        try:
            locks.acquire_team(team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            # Body is optional; if missing or not JSON, treat as no overrides
            try:
                body = await request.json()
            except Exception:
                body = {}

            env_override = None
            env_vars_raw = (body.get("env_vars") or "").strip() if body else ""
            if env_vars_raw:
                import re

                env_override = dict(
                    item.split("=", 1)
                    for item in re.split(r"[\n,]+", env_vars_raw)
                    if "=" in item
                )

            volumes_raw = (body.get("volumes") or "").strip() if body else ""
            volume_override = (
                volume_ops.parse_volume_mounts(volumes_raw) if volumes_raw else None
            )

            image_override = (body.get("image") or "").strip() or None if body else None
            hostname_override = (
                (body.get("hostname") or "").strip() or None if body else None
            )
            port_raw = body.get("port") if body else None
            port_override = int(port_raw) if port_raw else None
            routes_override = body.get("routes") if body and "routes" in body else None
            host_mode_override = (
                bool(body["host_mode"])
                if body and "host_mode" in body and body["host_mode"] is not None
                else None
            )
            privileged_override = (
                bool(body["privileged"])
                if body and "privileged" in body and body["privileged"] is not None
                else None
            )
            cap_add_override = None
            if body and "net_admin" in body and body["net_admin"] is not None:
                cap_add_override = ["NET_ADMIN"] if bool(body["net_admin"]) else []

            result = service_ops.update_service(
                get_settings(),
                team,
                name,
                env_override=env_override,
                image_override=image_override,
                port_override=port_override,
                hostname_override=hostname_override,
                host_mode_override=host_mode_override,
                volume_override=volume_override,
                cap_add_override=cap_add_override,
                privileged_override=privileged_override,
                routes_override=routes_override,
            )
            return JSONResponse({"ok": True, "result": result})
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_service_update")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_team(team.team_id)

    def api_service_restart(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            result = service_ops.restart_service(
                get_settings(), _get_ui_team(request), name
            )
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_service_restart")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_service_delete(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        try:
            locks.acquire_team(team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            result = service_ops.delete_service(
                get_settings(), _get_ui_team(request), name
            )
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_service_delete")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_team(team.team_id)

    def api_service_logs(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            n = int(request.query_params.get("n", "200"))
        except (ValueError, TypeError):
            n = 200
        try:
            logs = service_ops.get_service_logs(
                get_settings(), _get_ui_team(request), name, n
            )
            return JSONResponse({"ok": True, "logs": logs})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_service_logs")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_service_presets(request: Request) -> JSONResponse:
        try:
            presets = service_presets.list_presets(_get_ui_team(request))
            return JSONResponse({"ok": True, "presets": presets})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_service_presets")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_service_restore(request: Request) -> JSONResponse:
        team = _get_ui_team(request)
        try:
            locks.acquire_team(team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            body = await request.json()
            name = (body.get("name") or "").strip()
            image = (body.get("image") or "").strip()
            port = body.get("port")
            routes = body.get("routes")
            hostname = (body.get("hostname") or "").strip() or None
            env_vars_raw = (body.get("env_vars") or "").strip()
            host_mode = bool(body.get("host_mode", False))
            if not name or not image or (not port and not routes):
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "name, image and either port or routes are required.",
                    },
                    status_code=400,
                )
            env_vars = None
            if env_vars_raw:
                import re

                env_vars = dict(
                    item.split("=", 1)
                    for item in re.split(r"[\n,]+", env_vars_raw)
                    if "=" in item
                )
            volumes_raw = (body.get("volumes") or "").strip()
            parsed_volumes = (
                volume_ops.parse_volume_mounts(volumes_raw) if volumes_raw else None
            )
            privileged = bool(body.get("privileged", False))
            net_admin = bool(body.get("net_admin", False))
            cap_add = ["NET_ADMIN"] if net_admin else None
            result = service_ops.create_service(
                get_settings(),
                team,
                name,
                image,
                int(port) if port else None,
                hostname=hostname,
                env_vars=env_vars,
                host_mode=host_mode,
                volumes=parsed_volumes,
                cap_add=cap_add,
                privileged=privileged,
                routes=routes,
            )
            return JSONResponse({"ok": True, "result": result})
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_service_restore")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_team(team.team_id)

    def api_service_preset_delete(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            service_presets.delete_preset(_get_ui_team(request), name)
            return JSONResponse({"ok": True, "result": {"deleted": name}})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_service_preset_delete")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_volumes(request: Request) -> JSONResponse:
        try:
            vols = volume_ops.list_volumes(get_settings(), _get_ui_team(request))
            return JSONResponse({"ok": True, "volumes": vols})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_volumes")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_volume_create(request: Request) -> JSONResponse:
        team = _get_ui_team(request)
        try:
            locks.acquire_team(team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            body = await request.json()
            name = (body.get("name") or "").strip()
            description = (body.get("description") or "").strip()
            if not name:
                return JSONResponse(
                    {"ok": False, "error": "Volume name is required."},
                    status_code=400,
                )
            result = volume_ops.create_volume(
                get_settings(), team, name, description=description
            )
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_volume_create")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_team(team.team_id)

    def api_volume_delete(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        try:
            locks.acquire_team(team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            result = volume_ops.delete_volume(get_settings(), team, name)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_volume_delete")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_team(team.team_id)

    def api_agent_guides_list(request: Request) -> JSONResponse:
        try:
            guides = []
            guides_dir = os.path.join(_get_ui_team(request).data_dir, "agent_guides")
            bundled_dir = _TEMPLATE_DIR / "agent_guides"
            seen = set()
            if os.path.isdir(guides_dir):
                for fname in sorted(os.listdir(guides_dir)):
                    if fname.endswith(".md"):
                        seen.add(fname)
                        guides.append(
                            {
                                "filename": fname,
                                "title": _guide_title(os.path.join(guides_dir, fname)),
                            }
                        )
            if bundled_dir.is_dir():
                for fpath in sorted(bundled_dir.iterdir()):
                    if fpath.suffix == ".md" and fpath.name not in seen:
                        guides.append(
                            {"filename": fpath.name, "title": _guide_title(str(fpath))}
                        )
            return JSONResponse({"ok": True, "guides": guides})
        except Exception:
            logger.exception("Unexpected error in api_agent_guides_list")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_agent_guide_get(request: Request) -> JSONResponse:
        try:
            filename = request.path_params["filename"]
            if not filename.endswith(".md") or "/" in filename or "\\" in filename:
                return JSONResponse(
                    {"ok": False, "error": "Invalid filename"}, status_code=400
                )
            guides_dir = os.path.join(_get_ui_team(request).data_dir, "agent_guides")
            guide_path = os.path.join(guides_dir, filename)
            content = ""
            if os.path.isfile(guide_path):
                with open(guide_path, "r", encoding="utf-8") as f:
                    content = f.read()
            elif (_TEMPLATE_DIR / "agent_guides" / filename).is_file():
                content = (_TEMPLATE_DIR / "agent_guides" / filename).read_text(
                    encoding="utf-8"
                )
            else:
                return JSONResponse(
                    {"ok": False, "error": "Guide not found"}, status_code=404
                )
            return JSONResponse({"ok": True, "content": content, "filename": filename})
        except Exception:
            logger.exception("Unexpected error in api_agent_guide_get")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_agent_info(request: Request) -> JSONResponse:
        """Whether the coding agent is enabled for this team, and its default
        type. The dashboard fetches this at boot to decide if the Agent Chat /
        Agent CLI actions are shown at all. Configuration lives in oduflow.toml
        ([team.X] agent_* keys); there is no runtime editing."""
        try:
            team = _get_ui_team(request)
            return JSONResponse(
                {
                    "ok": True,
                    "enabled": team.agent_enabled,
                    "default": agent_config.effective_agent_default(team),
                }
            )
        except Exception:
            logger.exception("Unexpected error in api_agent_info")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_agent_acp_info(request: Request) -> JSONResponse:
        """Info the browser chat needs before connecting: the in-container
        working dir (used as ACP ``cwd``) and the stored session id to resume
        (or null to start fresh). See specs/0029-agent-console-and-chat.md."""
        try:
            from oduflow import agent_sessions
            from oduflow.naming import get_agent_checkout_dir

            branch = request.path_params["branch"]
            team = _get_ui_team(request)
            agent_type = agent_config.resolve_agent_type(
                request.query_params.get("type"), team
            )
            return JSONResponse(
                {
                    "ok": True,
                    "cwd": get_agent_checkout_dir(branch),
                    "type": agent_type,
                    "session_id": agent_sessions.get_session(team, branch, agent_type),
                }
            )
        except Exception:
            logger.exception("Unexpected error in api_agent_acp_info")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_agent_acp_session(request: Request) -> JSONResponse:
        """Persist (or clear) the durable session id for this environment+agent.
        The chat POSTs here after a ``session/new`` so the next open resumes it;
        an empty session_id clears it (Codex on a brand-new conversation)."""
        try:
            from oduflow import agent_sessions

            branch = request.path_params["branch"]
            team = _get_ui_team(request)
            body = await request.json()
            agent_type = agent_config.resolve_agent_type(body.get("type"), team)
            session_id = body.get("session_id")
            if session_id:
                agent_sessions.set_session(team, branch, agent_type, str(session_id))
            else:
                agent_sessions.clear_session(team, branch, agent_type)
            return JSONResponse({"ok": True})
        except Exception:
            logger.exception("Unexpected error in api_agent_acp_session")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_license(request: Request) -> JSONResponse:
        settings = get_settings()
        info = get_license_info(settings.etc_dir)
        return JSONResponse({"ok": True, "license": info.to_dict()})

    async def api_license_activate(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            key_text = (body.get("key") or "").strip()
            if not key_text:
                return JSONResponse(
                    {"ok": False, "error": "License key is required."}, status_code=400
                )
            settings = get_settings()
            info = install_license_from_text(key_text, settings.etc_dir)
            return JSONResponse({"ok": True, "license": info.to_dict()})
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception:
            logger.exception("Unexpected error in api_license_activate")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_extra_repos(request: Request) -> JSONResponse:
        from oduflow.extra_addons import list_extra_repos

        try:
            repos = list_extra_repos(_get_ui_team(request))
            return JSONResponse({"ok": True, "repos": repos})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_extra_repos")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_extra_repo_add(request: Request) -> JSONResponse:
        team = _get_ui_team(request)
        try:
            locks.acquire_team(team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            body = await request.json()
            name = (body.get("name") or "").strip()
            repo_url = (body.get("repo_url") or "").strip()
            git_user = (body.get("git_user") or "").strip()
            if not name or not repo_url:
                return JSONResponse(
                    {"ok": False, "error": "name and repo_url are required."},
                    status_code=400,
                )
            from oduflow.extra_addons import clone_extra_repo

            result = clone_extra_repo(team, name, repo_url, git_user=git_user)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_extra_repo_add")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_team(team.team_id)

    async def api_extra_repo_pull(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        try:
            locks.acquire_team(team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            from oduflow.extra_addons import fetch_extra_repo

            summary = fetch_extra_repo(team, name)
            return JSONResponse({"ok": True, "result": summary})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_extra_repo_pull")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_team(team.team_id)

    def api_extra_repo_protect(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            from oduflow.extra_addons import protect_extra_repo

            team = _get_ui_team(request)
            result = protect_extra_repo(team, name)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_extra_repo_protect")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_extra_repo_unprotect(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            from oduflow.extra_addons import unprotect_extra_repo

            team = _get_ui_team(request)
            result = unprotect_extra_repo(team, name)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_extra_repo_unprotect")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_extra_repo_delete(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        try:
            locks.acquire_team(team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            from oduflow.extra_addons import delete_extra_repo

            delete_extra_repo(get_settings(), team, name)
            return JSONResponse({"ok": True, "result": {"deleted": name}})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_extra_repo_delete")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_team(team.team_id)

    def api_credentials(request: Request) -> JSONResponse:
        from oduflow.git_ops import list_credentials

        try:
            team = _get_ui_team(request)
            creds = list_credentials(cred_file=team.git_credentials_file())
            return JSONResponse({"ok": True, "credentials": creds})
        except Exception:
            logger.exception("Unexpected error in api_credentials")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_credential_add(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            repo_url = (body.get("repo_url") or "").strip()
            if not repo_url:
                return JSONResponse(
                    {"ok": False, "error": "repo_url is required."},
                    status_code=400,
                )
            from oduflow import git_ops

            team = _get_ui_team(request)
            result = git_ops.setup_repo_auth(
                repo_url, cred_file=team.git_credentials_file()
            )
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_credential_add")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_credential_delete(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            host = (body.get("host") or "").strip()
            username = (body.get("username") or "").strip()
            if not host or not username:
                return JSONResponse(
                    {"ok": False, "error": "host and username are required."},
                    status_code=400,
                )
            from oduflow.git_ops import delete_credential

            team = _get_ui_team(request)
            removed = delete_credential(
                host, username, cred_file=team.git_credentials_file()
            )
            if not removed:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": f"Credential not found for {host}/{username}.",
                    },
                    status_code=404,
                )
            return JSONResponse(
                {"ok": True, "result": {"host": host, "username": username}}
            )
        except Exception:
            logger.exception("Unexpected error in api_credential_delete")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_credential_validate(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            host = (body.get("host") or "").strip()
            username = (body.get("username") or "").strip()
            if not host or not username:
                return JSONResponse(
                    {"ok": False, "error": "host and username are required."},
                    status_code=400,
                )
            from oduflow.git_ops import validate_credential

            team = _get_ui_team(request)
            status = validate_credential(
                host, username, cred_file=team.git_credentials_file()
            )
            return JSONResponse({"ok": True, "status": status})
        except Exception:
            logger.exception("Unexpected error in api_credential_validate")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_protect(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            team = _get_ui_team(request)
            result = env_ops.protect_environment(get_settings(), team, branch)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_protect")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_unprotect(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            team = _get_ui_team(request)
            result = env_ops.unprotect_environment(get_settings(), team, branch)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_unprotect")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_set_note(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            team = _get_ui_team(request)
            body = await request.json()
            note = body.get("note", "")
            result = env_ops.set_note(get_settings(), team, branch, note)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_set_note")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_mcp_access(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            settings = get_settings()
            team = _get_ui_team(request)
            token = env_ops.get_env_token(settings, team, branch)
            # In traefik mode the MCP endpoint is served on the team's own
            # (TLS-terminated) hostname, which is also the per-request OAuth
            # issuer — so advertise that, not a central oauth_base_url or the
            # internal request host. Port mode keeps the explicit issuer/base.
            if settings.routing_mode == "traefik":
                base = f"https://{team.hostname}"
            else:
                base = (settings.oauth_base_url or str(request.base_url)).rstrip("/")
            url = f"{base}/mcp/{quote(branch, safe='/')}"
            return JSONResponse({"ok": True, "result": {"url": url, "token": token}})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_mcp_access")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_env_users(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            settings = get_settings()
            team = _get_ui_team(request)
            users = odoo_ops.list_env_users(settings, team, branch)
            return JSONResponse({"ok": True, "result": {"users": users}})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_env_users")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_connect_as(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            settings = get_settings()
            team = _get_ui_team(request)
            body = await request.json()
            user = (body.get("user") or "admin").strip() or "admin"
            activity.touch(team, branch)
            result = odoo_ops.connect_as_user(settings, team, branch, user)
            return JSONResponse(
                {
                    "ok": True,
                    "result": {
                        "url": result["url"],
                        "login": result["login"],
                        "uid": result["uid"],
                        "expires_at": result["expires_at"],
                        "cookie": {
                            "name": "session_id",
                            "value": result["sid"],
                            "domain": result["cookie_domain"],
                            "path": "/",
                        },
                    },
                }
            )
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("Unexpected error in api_connect_as")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_connect_open(request: Request) -> Response:
        """ "Open": mint a session and land the browser in the env already
        logged in.

        The dashboard's Open button can't set the session_id cookie from
        JavaScript: Odoo issues session_id as HttpOnly and cookies are shared
        across localhost ports, so a ``document.cookie`` write is silently
        dropped and the env keeps the browser's stale session.

        Port / same-host mode: the dashboard and env share a host, so an HTTP
        Set-Cookie here (which overrides the HttpOnly cookie) reaches the env and
        the redirect lands authenticated.

        Traefik mode: the env lives on its own host, so a cookie set here would
        not reach it (and a parent-domain cookie would not override a stale
        host-only session_id on the env host). Instead mint the session, stash it
        behind a one-time token, and 303 the browser to the env's own
        ``/oduflow-connect`` (routed to Oduflow by Traefik), which sets the cookie
        host-only there.
        """
        branch = request.path_params["branch"]
        try:
            settings = get_settings()
            team = _get_ui_team(request)
            user = (request.query_params.get("user") or "admin").strip() or "admin"
            activity.touch(team, branch)
            result = odoo_ops.connect_as_user(settings, team, branch, user)
            if settings.routing_mode == "traefik":
                env_host = result["cookie_domain"]
                token = connect_tokens.issue(env_host, result["sid"])
                landing = f"https://{env_host}/oduflow-connect?token={token}"
                return RedirectResponse(landing, status_code=303)
            response: Response = RedirectResponse(result["url"], status_code=303)
            # Host-only cookie (no domain): scoped to the dashboard's host and,
            # since cookies ignore ports, sent to the env on the same host.
            # HttpOnly mirrors Odoo's own session_id so it overrides it cleanly.
            response.set_cookie(
                "session_id",
                result["sid"],
                path="/",
                httponly=True,
                samesite="lax",
            )
            return response
        except FlowError as e:
            return Response(
                f"Connect failed: {e}", status_code=400, media_type="text/plain"
            )
        except Exception as e:
            logger.exception("Unexpected error in api_connect_open")
            return Response(
                f"Connect failed: {e}", status_code=500, media_type="text/plain"
            )

    async def api_connect_land(request: Request) -> Response:
        """Traefik cross-subdomain Connect As landing, served ON the env host.

        Consumes the one-time token minted by :func:`api_connect_open`, sets the
        env's ``session_id`` cookie HOST-ONLY (no ``Domain`` → scoped to this env
        host, so it overrides any stale host-only cookie Odoo left here), and
        303-redirects to ``/web`` already authenticated. No dashboard session is
        required or present on this host; the token is the sole credential.
        """
        token = request.query_params.get("token") or ""
        env_host = request.url.hostname or ""
        sid = connect_tokens.consume(token, env_host)
        if not sid:
            return Response(
                "Connect link is invalid or expired. Reopen it from the dashboard.",
                status_code=400,
                media_type="text/plain",
            )
        response: Response = RedirectResponse(
            f"https://{env_host}/web", status_code=303
        )
        response.set_cookie(
            "session_id",
            sid,
            path="/",
            httponly=True,
            samesite="lax",
            secure=_is_secure_request(request),
        )
        return response

    async def ws_terminal(websocket: WebSocket) -> None:
        branch = websocket.path_params["branch"]
        await websocket.accept()
        try:
            import docker as _docker
            from oduflow.docker_ops.client import get_client as _get_client
            from oduflow.naming import get_resource_name, get_db_name

            settings = get_settings()
            team = _get_ui_team(websocket)
            client = _get_client()
            container_name = get_resource_name(
                branch, "odoo", settings.prefix, team.team_id
            )
            db_name = get_db_name(branch, team.team_id)

            try:
                container = client.containers.get(container_name)
            except _docker.errors.NotFound:
                await websocket.send_text(
                    "\x1b[31mError: environment not found\x1b[0m\r\n"
                )
                await websocket.close(code=1011)
                return

            if container.status != "running":
                await websocket.send_text(
                    "\x1b[31mError: container is not running\x1b[0m\r\n"
                )
                await websocket.close(code=1011)
                return

            exec_id = client.api.exec_create(
                container.id,
                [
                    "/entrypoint.sh",
                    "odoo",
                    "shell",
                    "-d",
                    db_name,
                    "--no-http",
                    "-c",
                    "/etc/odoo/odoo.conf",
                ],
                stdin=True,
                tty=True,
                stdout=True,
                stderr=True,
            )["Id"]
            sock = client.api.exec_start(exec_id, detach=False, tty=True, socket=True)
            raw_sock = sock._sock

            loop = asyncio.get_event_loop()
            closed = asyncio.Event()

            async def docker_to_browser() -> None:
                try:
                    while not closed.is_set():
                        data = await loop.run_in_executor(None, raw_sock.recv, 4096)
                        if not data:
                            break
                        await websocket.send_text(
                            data.decode("utf-8", errors="replace")
                        )
                except Exception:
                    pass
                finally:
                    closed.set()

            async def browser_to_docker() -> None:
                try:
                    while not closed.is_set():
                        text = await websocket.receive_text()
                        msg = json.loads(text)
                        if msg.get("type") == "input":
                            await loop.run_in_executor(
                                None, raw_sock.sendall, msg["data"].encode("utf-8")
                            )
                        elif msg.get("type") == "resize":
                            cols = msg.get("cols", 80)
                            rows = msg.get("rows", 24)
                            try:
                                client.api.exec_resize(exec_id, height=rows, width=cols)
                            except Exception:
                                pass
                except Exception:
                    pass
                finally:
                    closed.set()

            try:
                await asyncio.gather(docker_to_browser(), browser_to_docker())
            finally:
                try:
                    raw_sock.close()
                except Exception:
                    pass
                try:
                    sock.close()
                except Exception:
                    pass

        except Exception as e:
            logger.exception("WebSocket terminal error for branch %s", branch)
            try:
                await websocket.send_text(f"\x1b[31mError: {e}\x1b[0m\r\n")
                await websocket.close(code=1011)
            except Exception:
                pass

    async def ws_sql_terminal(websocket: WebSocket) -> None:
        branch = websocket.path_params["branch"]
        await websocket.accept()
        try:
            import docker as _docker
            from oduflow.docker_ops.client import get_client as _get_client
            from oduflow.naming import get_db_name, validate_env_name

            # Validate before deriving DB/credential paths, matching the REST
            # create handler: a name like ".." would otherwise resolve
            # get_workspace_path one level above the workspaces dir when loading
            # scoped credentials. Reject it explicitly instead of failing opaquely.
            try:
                validate_env_name(branch)
            except ValueError as e:
                await websocket.send_text(f"\x1b[31mError: {e}\x1b[0m\r\n")
                await websocket.close(code=1008)
                return

            settings = get_settings()
            team = _get_ui_team(websocket)
            client = _get_client()
            db_name = get_db_name(branch, team.team_id)

            try:
                db_container = client.containers.get(settings.shared_db_container)
            except _docker.errors.NotFound:
                await websocket.send_text(
                    "\x1b[31mError: database container not found\x1b[0m\r\n"
                )
                await websocket.close(code=1011)
                return

            if db_container.status != "running":
                await websocket.send_text(
                    "\x1b[31mError: database container is not running\x1b[0m\r\n"
                )
                await websocket.close(code=1011)
                return

            from oduflow.env_credentials import (
                MissingCredentialsError,
                load_credentials,
            )

            # Never open an interactive psql as the cluster superuser: that would
            # allow \c into another team's database and COPY ... FROM PROGRAM
            # (RCE). Require the environment's scoped role.
            try:
                creds = load_credentials(
                    branch,
                    team.workspaces_dir,
                    settings.db_user,
                    settings.db_password,
                    allow_fallback=False,
                )
            except MissingCredentialsError as e:
                await websocket.send_text(f"\x1b[31mError: {e}\x1b[0m\r\n")
                await websocket.close(code=1011)
                return

            exec_id = client.api.exec_create(
                db_container.id,
                ["psql", "-U", creds["pg_user"], "-d", db_name],
                stdin=True,
                tty=True,
                stdout=True,
                stderr=True,
            )["Id"]
            sock = client.api.exec_start(exec_id, detach=False, tty=True, socket=True)
            raw_sock = sock._sock

            loop = asyncio.get_event_loop()
            closed = asyncio.Event()

            async def docker_to_browser() -> None:
                try:
                    while not closed.is_set():
                        data = await loop.run_in_executor(None, raw_sock.recv, 4096)
                        if not data:
                            break
                        await websocket.send_text(
                            data.decode("utf-8", errors="replace")
                        )
                except Exception:
                    pass
                finally:
                    closed.set()

            async def browser_to_docker() -> None:
                try:
                    while not closed.is_set():
                        text = await websocket.receive_text()
                        msg = json.loads(text)
                        if msg.get("type") == "input":
                            await loop.run_in_executor(
                                None, raw_sock.sendall, msg["data"].encode("utf-8")
                            )
                        elif msg.get("type") == "resize":
                            cols = msg.get("cols", 80)
                            rows = msg.get("rows", 24)
                            try:
                                client.api.exec_resize(exec_id, height=rows, width=cols)
                            except Exception:
                                pass
                except Exception:
                    pass
                finally:
                    closed.set()

            try:
                await asyncio.gather(docker_to_browser(), browser_to_docker())
            finally:
                try:
                    raw_sock.close()
                except Exception:
                    pass
                try:
                    sock.close()
                except Exception:
                    pass

        except Exception as e:
            logger.exception("WebSocket SQL terminal error for branch %s", branch)
            try:
                await websocket.send_text(f"\x1b[31mError: {e}\x1b[0m\r\n")
                await websocket.close(code=1011)
            except Exception:
                pass

    async def ws_agent_console(websocket: WebSocket) -> None:
        branch = websocket.path_params["branch"]
        await websocket.accept()
        try:
            import docker as _docker
            from oduflow.docker_ops.client import get_client as _get_client
            from oduflow.naming import (
                get_agent_checkout_dir,
                get_agent_container_name,
            )

            settings = get_settings()
            team = _get_ui_team(websocket)
            client = _get_client()

            if not team.agent_enabled:
                await websocket.send_text(
                    "\x1b[31mError: the coding agent is disabled for this team. "
                    "Set agent_enabled = true in the [team."
                    + team.team_id
                    + "] section of oduflow.toml and restart the server.\x1b[0m\r\n"
                )
                await websocket.close(code=1011)
                return

            # Which agent to run (claude | codex); default from config.
            agent_type = agent_config.resolve_agent_type(
                websocket.query_params.get("type"), team
            )

            container_name = get_agent_container_name(team.team_id, settings.prefix)
            try:
                container = client.containers.get(container_name)
            except _docker.errors.NotFound:
                await websocket.send_text(
                    "\x1b[31mError: agent container not found "
                    "(it is created on server start; check the logs).\x1b[0m\r\n"
                )
                await websocket.close(code=1011)
                return

            if container.status != "running":
                await websocket.send_text(
                    "\x1b[31mError: agent container is not running\x1b[0m\r\n"
                )
                await websocket.close(code=1011)
                return

            # The console attaches at the environment's checkout. If it is
            # missing (an installation upgraded to the agent feature, or a
            # setup that failed at creation) heal it on demand by cloning from
            # the Odoo container labels.
            checkout = get_agent_checkout_dir(branch)

            def _checkout_missing() -> bool:
                code, _ = container.exec_run(["test", "-d", checkout])
                return bool(code != 0)

            missing = await asyncio.get_event_loop().run_in_executor(
                None, _checkout_missing
            )
            if missing:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: env_ops.ensure_agent_env_checkout(settings, team, branch),
                )
                missing = await asyncio.get_event_loop().run_in_executor(
                    None, _checkout_missing
                )
            if missing:
                await websocket.send_text(
                    "\x1b[31mError: no checkout at "
                    + checkout
                    + " — could not build it automatically (live-mount "
                    "environments have no repo to clone); recreate the "
                    "environment or check the server logs.\x1b[0m\r\n"
                )
                await websocket.close(code=1011)
                return

            # SCOPED per-env MCP credentials, injected into THIS session's exec
            # env only (never the container env): the token grants the ADR-0028
            # allowlist for this one environment, so a console user/agent can
            # only ever see a credential for the environment they already hold.
            mcp_url = env_ops.get_agent_mcp_url(settings, branch)
            try:
                mcp_token = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: env_ops.get_env_token(settings, team, branch)
                )
            except Exception:
                mcp_token = None
            if not mcp_token:
                await websocket.send_text(
                    "\x1b[33mWarning: this environment has no scoped MCP token "
                    "(created before MCP Access?) — the agent cannot drive it "
                    "over MCP. Update or recreate the environment.\x1b[0m\r\n"
                )
            exec_env = {
                "ODUFLOW_MCP_URL": mcp_url,
                "ODUFLOW_MCP_TOKEN": mcp_token or "",
            }

            if agent_type == "codex":
                # Codex has no project-scoped config; wire the Oduflow MCP
                # server via CLI overrides (values are parsed as TOML).
                cmd = [
                    "codex",
                    "-c",
                    f'mcp_servers.oduflow.url="{mcp_url}"',
                    "-c",
                    'mcp_servers.oduflow.bearer_token_env_var="ODUFLOW_MCP_TOKEN"',
                ]
                if settings.agent_codex_model:
                    cmd += ["--model", settings.agent_codex_model]
            else:
                cmd = ["claude"]
                if settings.agent_claude_model:
                    cmd += ["--model", settings.agent_claude_model]

            exec_id = client.api.exec_create(
                container.id,
                cmd,
                stdin=True,
                tty=True,
                stdout=True,
                stderr=True,
                workdir=checkout,
                environment=exec_env,
            )["Id"]
            sock = client.api.exec_start(exec_id, detach=False, tty=True, socket=True)
            raw_sock = sock._sock

            loop = asyncio.get_event_loop()
            closed = asyncio.Event()

            async def docker_to_browser() -> None:
                try:
                    while not closed.is_set():
                        data = await loop.run_in_executor(None, raw_sock.recv, 4096)
                        if not data:
                            break
                        await websocket.send_text(
                            data.decode("utf-8", errors="replace")
                        )
                except Exception:
                    pass
                finally:
                    closed.set()

            async def browser_to_docker() -> None:
                try:
                    while not closed.is_set():
                        text = await websocket.receive_text()
                        msg = json.loads(text)
                        if msg.get("type") == "input":
                            await loop.run_in_executor(
                                None, raw_sock.sendall, msg["data"].encode("utf-8")
                            )
                        elif msg.get("type") == "resize":
                            cols = msg.get("cols", 80)
                            rows = msg.get("rows", 24)
                            try:
                                client.api.exec_resize(exec_id, height=rows, width=cols)
                            except Exception:
                                pass
                except Exception:
                    pass
                finally:
                    closed.set()

            tasks = [
                asyncio.ensure_future(docker_to_browser()),
                asyncio.ensure_future(browser_to_docker()),
            ]
            try:
                # Return as soon as EITHER side ends. Unlike ws_terminal (whose
                # exec dies with the environment container), the agent container
                # is long-lived: waiting for BOTH would park docker_to_browser
                # in recv() forever after the browser closes an idle console,
                # leaking the exec'd agent process and the executor thread.
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                closed.set()
                # shutdown() reliably wakes a thread blocked in recv (a bare
                # close() need not) and gives the agent EOF on stdin so the
                # `docker exec` process exits instead of leaking.
                try:
                    raw_sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    raw_sock.close()
                except Exception:
                    pass
                try:
                    sock.close()
                except Exception:
                    pass
                try:
                    await websocket.close()
                except Exception:
                    pass
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        except Exception as e:
            logger.exception("WebSocket agent console error for branch %s", branch)
            try:
                await websocket.send_text(f"\x1b[31mError: {e}\x1b[0m\r\n")
                await websocket.close(code=1011)
            except Exception:
                pass

    async def ws_agent_acp(websocket: WebSocket) -> None:
        """Bridge the browser ACP chat to an ACP adapter in the coder container.

        Unlike ``ws_agent_console`` (an interactive PTY terminal), this is a dumb
        line-framed relay: the adapter speaks JSON-RPC over stdio. We exec it
        WITHOUT a TTY, demux docker's multiplexed stdout/stderr, forward one
        complete stdout line (= one JSON-RPC frame) per WebSocket message, and
        write inbound frames to stdin. See specs/0029-agent-console-and-chat.md."""
        branch = websocket.path_params["branch"]
        await websocket.accept()

        async def _err(msg: str) -> None:
            try:
                await websocket.send_text(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "_chat/error",
                            "params": {"message": msg},
                        }
                    )
                )
            except Exception:
                pass
            try:
                await websocket.close(code=1011)
            except Exception:
                pass

        async def _notice(msg: str) -> None:
            """Non-fatal system line in the chat (unlike _err, keeps going)."""
            try:
                await websocket.send_text(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "_chat/notice",
                            "params": {"message": msg},
                        }
                    )
                )
            except Exception:
                pass

        try:
            import docker as _docker
            from docker.utils.socket import (
                STDERR,
                next_frame_header,
                read_exactly,
            )

            from oduflow.docker_ops.client import get_client as _get_client
            from oduflow.naming import (
                get_agent_checkout_dir,
                get_agent_container_name,
            )

            settings = get_settings()
            team = _get_ui_team(websocket)
            client = _get_client()

            if not team.agent_enabled:
                await _err(
                    "the coding agent is disabled for this team. Set "
                    f"agent_enabled = true in the [team.{team.team_id}] section "
                    "of oduflow.toml and restart the server."
                )
                return

            agent_type = agent_config.resolve_agent_type(
                websocket.query_params.get("type"), team
            )

            container_name = get_agent_container_name(team.team_id, settings.prefix)
            try:
                container = client.containers.get(container_name)
            except _docker.errors.NotFound:
                await _err(
                    "agent container not found "
                    "(it is created on server start; check the logs)."
                )
                return
            if container.status != "running":
                await _err("agent container is not running")
                return

            checkout = get_agent_checkout_dir(branch)

            def _checkout_missing() -> bool:
                code, _ = container.exec_run(["test", "-d", checkout])
                return bool(code != 0)

            missing = await asyncio.get_event_loop().run_in_executor(
                None, _checkout_missing
            )
            if missing:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: env_ops.ensure_agent_env_checkout(settings, team, branch),
                )
                missing = await asyncio.get_event_loop().run_in_executor(
                    None, _checkout_missing
                )
            if missing:
                await _err(
                    f"no checkout at {checkout} — could not build it "
                    "automatically (live-mount environments have no repo to "
                    "clone); recreate the environment or check the logs."
                )
                return

            # SCOPED per-env MCP credentials for THIS session only (see the
            # matching block in ws_agent_console). Claude's adapter picks them
            # up via the ${VAR} placeholders in the checkout's .mcp.json; the
            # Codex adapter has no override channel yet (best-effort, per the
            # ADR) so its chat runs without Oduflow MCP for now.
            try:
                mcp_token = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: env_ops.get_env_token(settings, team, branch)
                )
            except Exception:
                mcp_token = None
            if not mcp_token:
                await _notice(
                    "This environment has no scoped MCP token (created before "
                    "MCP Access existed) — the agent cannot drive it over MCP. "
                    "Update or recreate the environment."
                )
            if agent_type == "codex":
                await _notice(
                    "Codex chat cannot reach the Oduflow MCP server yet (its "
                    "ACP adapter has no configuration channel) — the agent can "
                    "edit and push code, but use Claude chat or the Agent CLI "
                    "to drive the environment."
                )
            exec_env = {
                "ODUFLOW_MCP_URL": env_ops.get_agent_mcp_url(settings, branch),
                "ODUFLOW_MCP_TOKEN": mcp_token or "",
            }

            exec_id = client.api.exec_create(
                container.id,
                _acp_adapter_cmd(agent_type),
                stdin=True,
                tty=False,
                stdout=True,
                stderr=True,
                workdir=checkout,
                environment=exec_env,
            )["Id"]
            sock = client.api.exec_start(exec_id, detach=False, tty=False, socket=True)
            raw_sock = sock._sock

            loop = asyncio.get_event_loop()
            closed = asyncio.Event()

            async def docker_to_browser() -> None:
                buf = b""
                try:
                    while not closed.is_set():
                        stream, length = await loop.run_in_executor(
                            None, next_frame_header, raw_sock
                        )
                        if stream == -1 or length < 0:
                            break
                        if length == 0:
                            continue
                        payload = await loop.run_in_executor(
                            None, read_exactly, raw_sock, length
                        )
                        if stream == STDERR:
                            logger.info(
                                "acp[%s/%s] stderr: %s",
                                branch,
                                agent_type,
                                payload.decode("utf-8", "replace").rstrip(),
                            )
                            continue
                        # stdout carries NDJSON JSON-RPC. Frame boundaries do NOT
                        # align with lines, so buffer and emit one complete line
                        # (one JSON-RPC frame) per WebSocket message.
                        buf += payload
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            text = line.strip()
                            if text:
                                await websocket.send_text(
                                    text.decode("utf-8", "replace")
                                )
                except Exception:
                    pass
                finally:
                    closed.set()

            async def browser_to_docker() -> None:
                try:
                    while not closed.is_set():
                        text = await websocket.receive_text()
                        if not text:
                            continue
                        frame = text if text.endswith("\n") else text + "\n"
                        await loop.run_in_executor(
                            None, raw_sock.sendall, frame.encode("utf-8")
                        )
                except Exception:
                    pass
                finally:
                    closed.set()

            tasks = [
                asyncio.ensure_future(docker_to_browser()),
                asyncio.ensure_future(browser_to_docker()),
            ]
            try:
                # Return as soon as EITHER side ends — the browser closed the
                # chat, or the adapter exited.
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                closed.set()
                # Tear the exec down so it never outlives the chat. A thread
                # parked in next_frame_header/recv is only reliably woken by
                # shutdown() (a bare close() need not interrupt a blocked recv);
                # dropping the socket also gives the adapter EOF on stdin so the
                # `docker exec` process exits instead of leaking. Then cancel the
                # still-parked coroutine (receive_text is cancellable) and reap.
                try:
                    raw_sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    raw_sock.close()
                except Exception:
                    pass
                try:
                    sock.close()
                except Exception:
                    pass
                try:
                    await websocket.close()
                except Exception:
                    pass
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        except Exception:
            logger.exception("WebSocket ACP chat error for branch %s", branch)
            try:
                await websocket.close(code=1011)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Production hosting
    # ------------------------------------------------------------------

    def _prod_lock_key(team: TeamSettings, name: str) -> str:
        from oduflow.server import prod_lock_key

        return prod_lock_key(team.team_id, name)

    def api_productions(request: Request) -> JSONResponse:
        try:
            settings = get_settings()
            team = _get_ui_team(request)
            prods = production_ops.list_productions(settings, team)
            webhook_secret = production_registry.get_webhook_secret(team)
            return JSONResponse(
                {
                    "ok": True,
                    "productions": prods,
                    "backup_configured": settings.backup is not None,
                    "webhook": {
                        "path": "/api/webhooks/github",
                        "secret": webhook_secret,
                    },
                }
            )
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_productions failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_production_create(request: Request) -> JSONResponse:
        settings = get_settings()
        team = _get_ui_team(request)
        try:
            data = await request.json()
            name = str(data.get("name", "")).strip()
            repo_url = str(data.get("repo_url", "")).strip()
            git_ops.validate_repo_url(repo_url)
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        try:
            locks.acquire_env(_prod_lock_key(team, name))
        except FlowError as e:
            return _error_response(e)
        try:
            result = production_ops.create_production(
                settings,
                team,
                name,
                repo_url,
                str(data.get("branch", "")).strip(),
                str(data.get("domain", "")).strip(),
                str(data.get("odoo_image", "")).strip(),
                git_user=str(data.get("git_user", "")).strip(),
                extra_addons=_normalize_extra_addons(data.get("extra_addons")),
                auto_update=bool(data.get("auto_update")),
                template_name=str(data.get("template_name", "")).strip() or None,
            )
            return JSONResponse({"ok": True, **result})
        except FlowError as e:
            return _error_response(e)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception:
            logger.exception("api_production_create failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(_prod_lock_key(team, name))

    def _production_action(
        request: Request,
        action: Callable[[Settings, TeamSettings, str], dict[str, Any]],
    ) -> JSONResponse:
        """Shared lock/error wrapper for simple per-production POST actions."""
        settings = get_settings()
        team = _get_ui_team(request)
        name = request.path_params["name"]
        try:
            locks.acquire_env(_prod_lock_key(team, name))
        except FlowError as e:
            return _error_response(e)
        try:
            result = action(settings, team, name)
            return JSONResponse({"ok": True, **result})
        except FlowError as e:
            return _error_response(e)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception:
            logger.exception("production action failed for '%s'", name)
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(_prod_lock_key(team, name))

    def api_production_start(request: Request) -> JSONResponse:
        return _production_action(request, production_ops.start_production)

    def api_production_stop(request: Request) -> JSONResponse:
        return _production_action(request, production_ops.stop_production)

    def api_production_restart(request: Request) -> JSONResponse:
        return _production_action(request, production_ops.restart_production)

    def api_production_info(request: Request) -> JSONResponse:
        try:
            settings = get_settings()
            team = _get_ui_team(request)
            name = request.path_params["name"]
            info = production_ops.get_production_info(settings, team, name)
            return JSONResponse({"ok": True, **info})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_production_info failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_production_update(request: Request) -> JSONResponse:
        """Deploy in a background thread: deploys can run for minutes and
        would time browsers out; the dashboard polls status instead."""
        settings = get_settings()
        team = _get_ui_team(request)
        name = request.path_params["name"]
        try:
            production_registry.get_production(team, name)
        except FlowError as e:
            return _error_response(e)

        def _run() -> None:
            key = _prod_lock_key(team, name)
            if not locks.acquire_env_blocking(key, 300):
                logger.warning("UI deploy of '%s' timed out on lock", name)
                return
            try:
                production_ops.update_production(settings, team, name, trigger="ui")
            except Exception:
                logger.exception("UI deploy of production '%s' failed", name)
            finally:
                locks.release_env(key)

        threading.Thread(
            target=_run, name=f"oduflow-ui-deploy-{name}", daemon=True
        ).start()
        return JSONResponse({"ok": True, "started": True}, status_code=202)

    def api_production_rollback(request: Request) -> JSONResponse:
        settings = get_settings()
        team = _get_ui_team(request)
        name = request.path_params["name"]
        to_commit = request.query_params.get("to_commit", "")
        try:
            locks.acquire_env(_prod_lock_key(team, name))
        except FlowError as e:
            return _error_response(e)
        try:
            result = production_ops.rollback_production(
                settings, team, name, to_commit, trigger="ui"
            )
            return JSONResponse({"ok": True, **result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_production_rollback failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(_prod_lock_key(team, name))

    async def api_production_auto_update(request: Request) -> JSONResponse:
        try:
            team = _get_ui_team(request)
            name = request.path_params["name"]
            data = await request.json()
            production_registry.get_production(team, name)
            production_registry.update_production(
                team, name, {"auto_update": bool(data.get("enabled"))}
            )
            return JSONResponse({"ok": True})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_production_auto_update failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_production_logs(request: Request) -> JSONResponse:
        try:
            settings = get_settings()
            team = _get_ui_team(request)
            name = request.path_params["name"]
            n_lines = int(request.query_params.get("lines", "200"))
            logs = production_ops.production_logs(
                settings, team, name, n_lines=min(n_lines, 2000)
            )
            return JSONResponse({"ok": True, "logs": logs})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_production_logs failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_production_deploys(request: Request) -> JSONResponse:
        try:
            team = _get_ui_team(request)
            name = request.path_params["name"]
            production_registry.get_production(team, name)
            deploys = production_ops.read_deploys(team, name, limit=20)
            return JSONResponse({"ok": True, "deploys": deploys})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_production_deploys failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    async def api_production_delete(request: Request) -> JSONResponse:
        settings = get_settings()
        team = _get_ui_team(request)
        name = request.path_params["name"]
        try:
            data = await request.json()
        except Exception:
            data = {}
        if str(data.get("confirm", "")) != name:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Confirmation failed: type the production name.",
                },
                status_code=400,
            )
        try:
            locks.acquire_env(_prod_lock_key(team, name))
        except FlowError as e:
            return _error_response(e)
        try:
            result = production_ops.delete_production(
                settings, team, name, drop_database=bool(data.get("drop_database"))
            )
            return JSONResponse({"ok": True, **result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_production_delete failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(_prod_lock_key(team, name))

    def api_production_snapshots(request: Request) -> JSONResponse:
        try:
            from oduflow import backup_ops

            settings = get_settings()
            team = _get_ui_team(request)
            name = request.path_params["name"]
            refresh = request.query_params.get("refresh") == "true"
            manifests = backup_ops.list_snapshots(settings, team, name, refresh=refresh)
            return JSONResponse({"ok": True, "snapshots": manifests})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_production_snapshots failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_production_snapshot_now(request: Request) -> JSONResponse:
        from oduflow import backup_ops

        def _snapshot(
            settings: Settings, team: TeamSettings, name: str
        ) -> dict[str, Any]:
            return backup_ops.snapshot_production(settings, team, name, trigger="ui")

        return _production_action(request, _snapshot)

    async def api_production_restore(request: Request) -> JSONResponse:
        from oduflow import backup_ops

        settings = get_settings()
        team = _get_ui_team(request)
        name = request.path_params["name"]
        try:
            data = await request.json()
        except Exception:
            data = {}
        snapshot_id = str(data.get("snapshot_id", "")).strip()
        if str(data.get("confirm", "")) != name:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "Confirmation failed: type the production name.",
                },
                status_code=400,
            )
        if not snapshot_id:
            return JSONResponse(
                {"ok": False, "error": "snapshot_id is required"}, status_code=400
            )
        try:
            locks.acquire_env(_prod_lock_key(team, name))
        except FlowError as e:
            return _error_response(e)
        try:
            result = backup_ops.restore_production(settings, team, name, snapshot_id)
            return JSONResponse({"ok": True, **result})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_production_restore failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )
        finally:
            locks.release_env(_prod_lock_key(team, name))

    async def api_production_backup_schedule(request: Request) -> JSONResponse:
        try:
            team = _get_ui_team(request)
            name = request.path_params["name"]
            data = await request.json()
            schedule = str(data.get("schedule", "")).strip().lower()
            if schedule != "off" and not re.match(
                r"^([01]\d|2[0-3]):[0-5]\d$", schedule
            ):
                return JSONResponse(
                    {"ok": False, "error": 'schedule must be "HH:MM" or "off"'},
                    status_code=400,
                )
            production_registry.get_production(team, name)
            production_registry.set_nested(team, name, "backup", {"schedule": schedule})
            return JSONResponse({"ok": True})
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_production_backup_schedule failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    def api_production_backup_status(request: Request) -> JSONResponse:
        try:
            from oduflow import backup_ops

            settings = get_settings()
            team = _get_ui_team(request)
            return JSONResponse(
                {"ok": True, **backup_ops.backup_status(settings, team)}
            )
        except FlowError as e:
            return _error_response(e)
        except Exception:
            logger.exception("api_production_backup_status failed")
            return JSONResponse(
                {"ok": False, "error": "Internal server error."}, status_code=500
            )

    # ------------------------------------------------------------------
    # Health + GitHub webhook (both PUBLIC paths with their own auth)
    # ------------------------------------------------------------------

    def healthz(request: Request) -> JSONResponse:
        from oduflow.health import collect_health

        result = collect_health(get_settings())
        return JSONResponse(result, status_code=200 if result["ok"] else 503)

    async def webhook_github(request: Request) -> JSONResponse:
        from oduflow import webhooks

        body = await request.body()
        status, payload = webhooks.handle_github_event(
            get_settings(),
            locks,
            event=request.headers.get("x-github-event", ""),
            body=body,
            signature_header=request.headers.get("x-hub-signature-256", ""),
        )
        return JSONResponse(payload, status_code=status)

    return [
        Route("/", dashboard, methods=["GET"]),
        Route("/login", login, methods=["GET", "POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/favicon.ico", favicon, methods=["GET"]),
        Route("/logo.png", logo, methods=["GET"]),
        Route("/static/{filename}", static_file, methods=["GET"]),
        Route("/api/license", api_license, methods=["GET"]),
        Route("/api/license/activate", api_license_activate, methods=["POST"]),
        Route("/api/templates", api_templates, methods=["GET"]),
        Route("/import-odoo.sh", import_odoo_script, methods=["GET"]),
        Route("/api/templates/import-token", api_import_token, methods=["POST"]),
        Route("/api/templates/import/status", api_import_status, methods=["GET"]),
        Route("/api/templates/import/manifest", api_import_manifest, methods=["POST"]),
        Route("/api/templates/import/dump", api_import_dump, methods=["POST"]),
        Route(
            "/api/templates/import/filestore", api_import_filestore, methods=["POST"]
        ),
        Route("/api/templates/import/addon", api_import_addon, methods=["POST"]),
        Route(
            "/api/templates/import/addon-remote",
            api_import_addon_remote,
            methods=["POST"],
        ),
        Route("/api/templates/import/finalize", api_import_finalize, methods=["POST"]),
        Route("/api/templates/{name}/delete", api_template_delete, methods=["POST"]),
        Route("/api/templates/{name}/rename", api_template_rename, methods=["POST"]),
        Route("/api/environments", api_list, methods=["GET"]),
        Route("/api/environments/create", api_create, methods=["POST"]),
        Route("/api/productions", api_productions, methods=["GET"]),
        Route("/api/productions/create", api_production_create, methods=["POST"]),
        Route(
            "/api/productions/backup-status",
            api_production_backup_status,
            methods=["GET"],
        ),
        Route("/api/productions/{name}", api_production_info, methods=["GET"]),
        Route("/api/productions/{name}/start", api_production_start, methods=["POST"]),
        Route("/api/productions/{name}/stop", api_production_stop, methods=["POST"]),
        Route(
            "/api/productions/{name}/restart",
            api_production_restart,
            methods=["POST"],
        ),
        Route(
            "/api/productions/{name}/update", api_production_update, methods=["POST"]
        ),
        Route(
            "/api/productions/{name}/rollback",
            api_production_rollback,
            methods=["POST"],
        ),
        Route(
            "/api/productions/{name}/auto-update",
            api_production_auto_update,
            methods=["POST"],
        ),
        Route("/api/productions/{name}/logs", api_production_logs, methods=["GET"]),
        Route(
            "/api/productions/{name}/deploys",
            api_production_deploys,
            methods=["GET"],
        ),
        Route(
            "/api/productions/{name}/delete", api_production_delete, methods=["POST"]
        ),
        Route(
            "/api/productions/{name}/snapshots",
            api_production_snapshots,
            methods=["GET"],
        ),
        Route(
            "/api/productions/{name}/snapshot",
            api_production_snapshot_now,
            methods=["POST"],
        ),
        Route(
            "/api/productions/{name}/restore",
            api_production_restore,
            methods=["POST"],
        ),
        Route(
            "/api/productions/{name}/backup-schedule",
            api_production_backup_schedule,
            methods=["POST"],
        ),
        Route("/healthz", healthz, methods=["GET"]),
        Route("/api/webhooks/github", webhook_github, methods=["POST"]),
        Route("/api/stats", api_stats, methods=["GET"]),
        Route("/api/usage", api_usage, methods=["GET"]),
        Route("/api/usage/refresh", api_usage_refresh, methods=["POST"]),
        Route(
            "/api/environments/{branch:path}/storage/refresh",
            api_storage_refresh,
            methods=["POST"],
        ),
        Route("/api/agent-guides", api_agent_guides_list, methods=["GET"]),
        Route("/api/agent-guides/{filename}", api_agent_guide_get, methods=["GET"]),
        Route("/api/agent", api_agent_info, methods=["GET"]),
        Route(
            "/api/environments/{branch:path}/agent-acp/info",
            api_agent_acp_info,
            methods=["GET"],
        ),
        Route(
            "/api/environments/{branch:path}/agent-acp/session",
            api_agent_acp_session,
            methods=["POST"],
        ),
        Route("/api/environments/{branch:path}/start", api_start, methods=["POST"]),
        Route("/api/environments/{branch:path}/stop", api_stop, methods=["POST"]),
        Route("/api/environments/{branch:path}/restart", api_restart, methods=["POST"]),
        Route("/api/environments/{branch:path}/sync", api_sync, methods=["POST"]),
        Route("/api/environments/{branch:path}/protect", api_protect, methods=["POST"]),
        Route(
            "/api/environments/{branch:path}/unprotect", api_unprotect, methods=["POST"]
        ),
        Route("/api/environments/{branch:path}/note", api_set_note, methods=["POST"]),
        Route(
            "/api/environments/{branch:path}/mcp-access",
            api_mcp_access,
            methods=["GET"],
        ),
        Route(
            "/api/environments/{branch:path}/users",
            api_env_users,
            methods=["GET"],
        ),
        Route(
            "/api/environments/{branch:path}/connect-as",
            api_connect_as,
            methods=["POST"],
        ),
        Route(
            "/api/environments/{branch:path}/connect-open",
            api_connect_open,
            methods=["GET"],
        ),
        # Served on an env host (routed here by Traefik's PathPrefix router);
        # public + token-authenticated. See api_connect_land.
        Route("/oduflow-connect", api_connect_land, methods=["GET"]),
        Route("/api/environments/{branch:path}/update", api_update, methods=["POST"]),
        Route(
            "/api/environments/{branch:path}/recreate", api_recreate, methods=["POST"]
        ),
        Route(
            "/api/environments/{branch:path}/save-as-template",
            api_save_as_template,
            methods=["POST"],
        ),
        Route("/api/environments/{branch:path}/delete", api_delete, methods=["POST"]),
        Route("/api/services", api_services, methods=["GET"]),
        Route("/api/services/create", api_service_create, methods=["POST"]),
        Route("/api/services/{name}/update", api_service_update, methods=["POST"]),
        Route("/api/services/{name}/restart", api_service_restart, methods=["POST"]),
        Route("/api/services/{name}/delete", api_service_delete, methods=["POST"]),
        Route("/api/services/{name}/logs", api_service_logs, methods=["GET"]),
        Route("/api/service-presets", api_service_presets, methods=["GET"]),
        Route("/api/service-presets/restore", api_service_restore, methods=["POST"]),
        Route(
            "/api/service-presets/{name}/delete",
            api_service_preset_delete,
            methods=["POST"],
        ),
        Route("/api/volumes", api_volumes, methods=["GET"]),
        Route("/api/volumes/create", api_volume_create, methods=["POST"]),
        Route("/api/volumes/{name}/delete", api_volume_delete, methods=["POST"]),
        Route("/api/extra-repos", api_extra_repos, methods=["GET"]),
        Route("/api/extra-repos/add", api_extra_repo_add, methods=["POST"]),
        Route("/api/extra-repos/{name}/pull", api_extra_repo_pull, methods=["POST"]),
        Route(
            "/api/extra-repos/{name}/protect", api_extra_repo_protect, methods=["POST"]
        ),
        Route(
            "/api/extra-repos/{name}/unprotect",
            api_extra_repo_unprotect,
            methods=["POST"],
        ),
        Route(
            "/api/extra-repos/{name}/delete", api_extra_repo_delete, methods=["POST"]
        ),
        Route("/api/credentials", api_credentials, methods=["GET"]),
        Route("/api/credentials/add", api_credential_add, methods=["POST"]),
        Route("/api/credentials/delete", api_credential_delete, methods=["POST"]),
        Route("/api/credentials/validate", api_credential_validate, methods=["POST"]),
        Route("/api/environments/{branch:path}/logs", api_logs, methods=["GET"]),
        WebSocketRoute("/api/environments/{branch:path}/terminal", ws_terminal),
        WebSocketRoute("/api/environments/{branch:path}/sql", ws_sql_terminal),
        WebSocketRoute("/api/environments/{branch:path}/agent", ws_agent_console),
        WebSocketRoute("/api/environments/{branch:path}/agent-acp", ws_agent_acp),
    ]


def mount_web_ui(
    app: Starlette,
    get_settings: Callable[[], Settings],
    locks: LockManager,
) -> None:
    from starlette.routing import Router

    routes = _build_routes(get_settings, locks)
    sub_app: ASGIApp = Router(routes=routes)

    settings = get_settings()
    has_ui_passwords = any(t.ui_password for t in settings.teams.values())
    if has_ui_passwords:
        sub_app = BasicAuthMiddleware(sub_app, get_settings)
        logger.info("Web UI Basic Auth ENABLED (user: %s)", _AUTH_USER)
    else:
        logger.warning("Web UI auth DISABLED (no ui_password set in any team)")

    from starlette.routing import Mount

    app.routes.append(Mount("/", app=sub_app))
