from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import pathlib
import secrets

from itsdangerous import BadData, URLSafeTimedSerializer
from starlette.requests import HTTPConnection, Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route, WebSocketRoute
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

from oduflow.docker_ops import (
    env_ops,
    service_ops,
    service_presets,
    system_ops,
    volume_ops,
)
from oduflow import activity
from oduflow.docker_ops.odoo_ops import get_environment_logs
from oduflow.docker_ops.stats import get_container_stats, get_system_stats
from oduflow.errors import BusyError, FlowError, NotFoundError
from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.licensing import get_license_info, install_license_from_text

logger = logging.getLogger("oduflow")

_AUTH_USER = "admin"
_AUTH_COOKIE = "oduflow_ui_auth"
# Reachable without authentication: the login flow and static brand assets
# (so the login page can render its logo/favicon/fonts). /static/ serves only
# vetted extensions from the packaged assets dir (fonts, icons, xterm).
_PUBLIC_PATHS = frozenset({"/login", "/logout", "/favicon.ico", "/logo.png"})
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


class BasicAuthMiddleware:
    def __init__(self, app: ASGIApp, get_settings: "callable") -> None:
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
    html = (_TEMPLATE_DIR / "login.html").read_text(encoding="utf-8")
    banner = f'<div class="error">{error}</div>' if error else ""
    return html.replace("<!--ERROR-->", banner)


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


def _normalize_extra_addons(raw_addons) -> dict[str, str]:
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


def _build_routes(
    get_settings: "callable",
    locks: LockManager,
) -> list[Route]:

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
            password = await _read_login_password(request)
            team = settings.get_team_by_ui_password(password) if password else None
            if team is not None:
                response: Response = RedirectResponse("/", status_code=303)
                _set_session_cookie(response, team, request)
                return response
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

    def _get_ui_team(request: Request) -> TeamSettings:
        """Get the team from request state (set by auth middleware) or fallback."""
        if hasattr(request.state, "team"):
            return request.state.team
        settings = get_settings()
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
        except Exception as e:
            logger.exception("Unexpected error in api_list")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    def api_start(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            result = env_ops.start_environment(get_settings(), branch)
            activity.mark_started(_get_ui_team(request), branch)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_start")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    def api_stop(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            settings = get_settings()
            team = _get_ui_team(request)
            result = env_ops.stop_environment(settings, team, branch)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_stop")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    def api_restart(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            result = env_ops.restart_environment(get_settings(), branch)
            activity.mark_started(_get_ui_team(request), branch)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_restart")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    def api_sync(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            locks.acquire_env(branch)
        except BusyError as e:
            return _error_response(e)
        try:
            settings = get_settings()
            team = _get_ui_team(request)
            activity.touch(team, branch)
            result = env_ops.pull_environment(settings, team, branch)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_sync")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        finally:
            locks.release_env(branch)

    def api_delete(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            locks.acquire_env(branch)
        except BusyError as e:
            return _error_response(e)
        try:
            settings = get_settings()
            team = _get_ui_team(request)
            env_ops.delete_environment(settings, team, branch)
            return JSONResponse({"ok": True, "result": {"deleted": branch}})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_delete")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        finally:
            locks.release_env(branch)

    async def api_update(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
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
            locks.acquire_env(branch)
        except BusyError as e:
            return _error_response(e)
        try:
            settings = get_settings()
            team = _get_ui_team(request)
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
        except Exception as e:
            logger.exception("Unexpected error in api_update")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        finally:
            locks.release_env(branch)

    def api_recreate(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            locks.acquire_env(branch)
        except BusyError as e:
            return _error_response(e)
        try:
            import docker as _docker
            from oduflow.docker_ops.client import get_client as _get_client

            settings = get_settings()
            team = _get_ui_team(request)
            activity.touch(team, branch)
            client = _get_client()
            odoo_container_name = env_ops.get_resource_name(
                branch, "odoo", settings.prefix
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
        except Exception as e:
            logger.exception("Unexpected error in api_recreate")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        finally:
            locks.release_env(branch)

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

        # Acquire the env lock in its own try so a BusyError returns WITHOUT
        # entering the try/finally below — otherwise the finally would release a
        # lock that another in-flight request holds (issue #42).
        try:
            locks.acquire_env(env_name)
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
            team = _get_ui_team(request)
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
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_create")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
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
                get_settings(), branch, n_lines=n, container_name=container
            )
            return JSONResponse({"ok": True, "logs": logs})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_logs")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    def api_stats(request: Request) -> JSONResponse:
        try:
            settings = get_settings()
            containers = get_container_stats(settings, _get_ui_team(request))
            system = get_system_stats()
            return JSONResponse(
                {"ok": True, "containers": containers, "system": system}
            )
        except Exception as e:
            logger.exception("Unexpected error in api_stats")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    def api_templates(request: Request) -> JSONResponse:
        try:
            templates = system_ops.list_templates(get_settings(), _get_ui_team(request))
            return JSONResponse({"ok": True, "templates": templates})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_templates")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

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
        except Exception as e:
            logger.exception("Unexpected error in api_template_delete")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        finally:
            locks.release_team(team.team_id)

    def api_services(request: Request) -> JSONResponse:
        try:
            services = service_ops.list_services(get_settings(), _get_ui_team(request))
            return JSONResponse({"ok": True, "services": services})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_services")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

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
            hostname = (body.get("hostname") or "").strip() or None
            env_vars_raw = (body.get("env_vars") or "").strip()
            host_mode = bool(body.get("host_mode", False))
            if not name or not image or not port:
                return JSONResponse(
                    {"ok": False, "error": "name, image and port are required."},
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
                int(port),
                hostname=hostname,
                env_vars=env_vars,
                host_mode=host_mode,
                volumes=parsed_volumes,
                cap_add=cap_add,
                privileged=privileged,
            )
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_service_create")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
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
            )
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_service_update")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        finally:
            locks.release_team(team.team_id)

    def api_service_restart(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            result = service_ops.restart_service(get_settings(), name)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_service_restart")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    def api_service_delete(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        team = _get_ui_team(request)
        try:
            locks.acquire_team(team.team_id)
        except BusyError as e:
            return _error_response(e)
        try:
            result = service_ops.delete_service(get_settings(), name)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_service_delete")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        finally:
            locks.release_team(team.team_id)

    def api_service_logs(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            n = int(request.query_params.get("n", "200"))
        except (ValueError, TypeError):
            n = 200
        try:
            logs = service_ops.get_service_logs(get_settings(), name, n)
            return JSONResponse({"ok": True, "logs": logs})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_service_logs")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    def api_service_presets(request: Request) -> JSONResponse:
        try:
            presets = service_presets.list_presets(_get_ui_team(request))
            return JSONResponse({"ok": True, "presets": presets})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_service_presets")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

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
            hostname = (body.get("hostname") or "").strip() or None
            env_vars_raw = (body.get("env_vars") or "").strip()
            host_mode = bool(body.get("host_mode", False))
            if not name or not image or not port:
                return JSONResponse(
                    {"ok": False, "error": "name, image and port are required."},
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
                int(port),
                hostname=hostname,
                env_vars=env_vars,
                host_mode=host_mode,
                volumes=parsed_volumes,
                cap_add=cap_add,
                privileged=privileged,
            )
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_service_restore")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        finally:
            locks.release_team(team.team_id)

    def api_service_preset_delete(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            service_presets.delete_preset(_get_ui_team(request), name)
            return JSONResponse({"ok": True, "result": {"deleted": name}})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_service_preset_delete")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    def api_volumes(request: Request) -> JSONResponse:
        try:
            vols = volume_ops.list_volumes(get_settings(), _get_ui_team(request))
            return JSONResponse({"ok": True, "volumes": vols})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_volumes")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

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
        except Exception as e:
            logger.exception("Unexpected error in api_volume_create")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
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
        except Exception as e:
            logger.exception("Unexpected error in api_volume_delete")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
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
        except Exception as e:
            logger.exception("Unexpected error in api_agent_guides_list")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

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
        except Exception as e:
            logger.exception("Unexpected error in api_agent_guide_get")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

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
        except Exception as e:
            logger.exception("Unexpected error in api_license_activate")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    def api_extra_repos(request: Request) -> JSONResponse:
        from oduflow.extra_addons import list_extra_repos

        try:
            repos = list_extra_repos(_get_ui_team(request))
            return JSONResponse({"ok": True, "repos": repos})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_extra_repos")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

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
        except Exception as e:
            logger.exception("Unexpected error in api_extra_repo_add")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
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
        except Exception as e:
            logger.exception("Unexpected error in api_extra_repo_pull")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
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
        except Exception as e:
            logger.exception("Unexpected error in api_extra_repo_protect")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    def api_extra_repo_unprotect(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            from oduflow.extra_addons import unprotect_extra_repo

            team = _get_ui_team(request)
            result = unprotect_extra_repo(team, name)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_extra_repo_unprotect")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

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
        except Exception as e:
            logger.exception("Unexpected error in api_extra_repo_delete")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        finally:
            locks.release_team(team.team_id)

    def api_credentials(request: Request) -> JSONResponse:
        from oduflow.git_ops import list_credentials

        try:
            team = _get_ui_team(request)
            creds = list_credentials(cred_file=team.git_credentials_file())
            return JSONResponse({"ok": True, "credentials": creds})
        except Exception as e:
            logger.exception("Unexpected error in api_credentials")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

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
        except Exception as e:
            logger.exception("Unexpected error in api_credential_add")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

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
        except Exception as e:
            logger.exception("Unexpected error in api_credential_delete")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

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
        except Exception as e:
            logger.exception("Unexpected error in api_credential_validate")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    def api_protect(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            team = _get_ui_team(request)
            result = env_ops.protect_environment(get_settings(), team, branch)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_protect")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    def api_unprotect(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            team = _get_ui_team(request)
            result = env_ops.unprotect_environment(get_settings(), team, branch)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_unprotect")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

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
        except Exception as e:
            logger.exception("Unexpected error in api_set_note")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

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
            container_name = get_resource_name(branch, "odoo", settings.prefix)
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
            from oduflow.naming import get_db_name

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

            from oduflow.env_credentials import load_credentials

            creds = load_credentials(
                branch, team.workspaces_dir, settings.db_user, settings.db_password
            )

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
        Route(
            "/api/templates/{name}/delete", api_template_delete, methods=["POST"]
        ),
        Route("/api/environments", api_list, methods=["GET"]),
        Route("/api/environments/create", api_create, methods=["POST"]),
        Route("/api/stats", api_stats, methods=["GET"]),
        Route("/api/agent-guides", api_agent_guides_list, methods=["GET"]),
        Route("/api/agent-guides/{filename}", api_agent_guide_get, methods=["GET"]),
        Route("/api/environments/{branch:path}/start", api_start, methods=["POST"]),
        Route("/api/environments/{branch:path}/stop", api_stop, methods=["POST"]),
        Route("/api/environments/{branch:path}/restart", api_restart, methods=["POST"]),
        Route("/api/environments/{branch:path}/sync", api_sync, methods=["POST"]),
        Route("/api/environments/{branch:path}/protect", api_protect, methods=["POST"]),
        Route(
            "/api/environments/{branch:path}/unprotect", api_unprotect, methods=["POST"]
        ),
        Route("/api/environments/{branch:path}/note", api_set_note, methods=["POST"]),
        Route("/api/environments/{branch:path}/update", api_update, methods=["POST"]),
        Route(
            "/api/environments/{branch:path}/recreate", api_recreate, methods=["POST"]
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
    ]


def mount_web_ui(
    app,
    get_settings: "callable",
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
