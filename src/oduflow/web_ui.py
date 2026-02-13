import base64
import hmac
import json
import logging
import os
import pathlib
import threading

from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from oduflow.docker_ops import env_ops, service_ops, service_presets, system_ops
from oduflow.docker_ops.odoo_ops import get_environment_logs
from oduflow.docker_ops.stats import get_container_stats, get_system_stats
from oduflow.errors import BusyError, FlowError, NotFoundError
from oduflow.settings import Settings
from oduflow.licensing import get_license_info, install_license_from_text

logger = logging.getLogger("oduflow")

_AUTH_REALM = "Oduflow Dashboard"
_AUTH_USER = "admin"


class BasicAuthMiddleware:
    def __init__(self, app: ASGIApp, password: str) -> None:
        self._app = app
        self._password = password

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode()

        if self._check_credentials(auth_header):
            await self._app(scope, receive, send)
        else:
            response = Response(
                "Unauthorized",
                status_code=401,
                headers={"WWW-Authenticate": f'Basic realm="{_AUTH_REALM}"'},
            )
            await response(scope, receive, send)

    def _check_credentials(self, auth_header: str) -> bool:
        if not auth_header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(auth_header[6:]).decode()
            user, password = decoded.split(":", 1)
        except Exception:
            return False
        return user == _AUTH_USER and hmac.compare_digest(password, self._password)

_TEMPLATE_DIR = pathlib.Path(__file__).resolve().parents[2] / "templates"


def _error_response(e: FlowError) -> JSONResponse:
    if isinstance(e, NotFoundError):
        status = 404
    elif isinstance(e, BusyError):
        status = 409
    else:
        status = 400
    return JSONResponse({"ok": False, "error": str(e)}, status_code=status)


def _build_routes(
    get_settings: "callable",
    busy_lock: threading.Lock,
) -> list[Route]:

    def dashboard(request: Request) -> HTMLResponse:
        html_path = _TEMPLATE_DIR / "dashboard.html"
        return HTMLResponse(html_path.read_text(encoding="utf-8"))

    def favicon(request: Request) -> Response:
        ico_path = _TEMPLATE_DIR / "favicon.ico"
        return Response(ico_path.read_bytes(), media_type="image/x-icon")

    def api_list(request: Request) -> JSONResponse:
        try:
            envs = env_ops.list_environments(get_settings())
            return JSONResponse({"ok": True, "environments": envs})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_list")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    def api_start(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        if not busy_lock.acquire(blocking=False):
            return _error_response(BusyError("Another operation is in progress."))
        try:
            result = env_ops.start_environment(get_settings(), branch)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_start")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        finally:
            busy_lock.release()

    def api_stop(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        if not busy_lock.acquire(blocking=False):
            return _error_response(BusyError("Another operation is in progress."))
        try:
            result = env_ops.stop_environment(get_settings(), branch)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_stop")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        finally:
            busy_lock.release()

    def api_restart(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        if not busy_lock.acquire(blocking=False):
            return _error_response(BusyError("Another operation is in progress."))
        try:
            result = env_ops.restart_environment(get_settings(), branch)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_restart")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        finally:
            busy_lock.release()

    def api_delete(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        if not busy_lock.acquire(blocking=False):
            return _error_response(BusyError("Another operation is in progress."))
        try:
            env_ops.delete_environment(get_settings(), branch)
            return JSONResponse({"ok": True, "result": {"deleted": branch}})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_delete")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        finally:
            busy_lock.release()

    async def api_create(request: Request) -> JSONResponse:
        if not busy_lock.acquire(blocking=False):
            return _error_response(BusyError("Another operation is in progress."))
        try:
            body = await request.json()
            branch_name = (body.get("branch_name") or "").strip()
            repo_url = (body.get("repo_url") or "").strip()
            odoo_image = (body.get("odoo_image") or "").strip()
            template_name_raw = (body.get("template_name") or "").strip()
            extra_addons_raw = (body.get("extra_addons") or "").strip()
            extra_addons_branch = (body.get("extra_addons_branch") or "").strip()
            extra_list = [x.strip() for x in extra_addons_raw.split(",") if x.strip()] if extra_addons_raw else None
            if not branch_name or not repo_url or not odoo_image:
                return JSONResponse(
                    {"ok": False, "error": "branch_name, repo_url and odoo_image are required."},
                    status_code=400,
                )
            resolved_template: str | None
            if template_name_raw.lower() == "none":
                resolved_template = None
            else:
                resolved_template = template_name_raw
            result = env_ops.create_environment(
                get_settings(), branch_name, repo_url, odoo_image,
                template_name=resolved_template,
                extra_addons=extra_list,
                extra_addons_branch=extra_addons_branch,
            )
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_create")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        finally:
            busy_lock.release()

    def api_logs(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            n = int(request.query_params.get("n", "200"))
        except (ValueError, TypeError):
            n = 200
        try:
            logs = get_environment_logs(get_settings(), branch, n_lines=n)
            return JSONResponse({"ok": True, "logs": logs})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_logs")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    def api_stats(request: Request) -> JSONResponse:
        try:
            settings = get_settings()
            containers = get_container_stats(settings)
            system = get_system_stats()
            return JSONResponse({"ok": True, "containers": containers, "system": system})
        except Exception as e:
            logger.exception("Unexpected error in api_stats")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    def api_templates(request: Request) -> JSONResponse:
        try:
            templates = system_ops.list_templates(get_settings())
            return JSONResponse({"ok": True, "templates": templates})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_templates")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    def api_services(request: Request) -> JSONResponse:
        try:
            services = service_ops.list_services(get_settings())
            return JSONResponse({"ok": True, "services": services})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_services")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    async def api_service_create(request: Request) -> JSONResponse:
        if not busy_lock.acquire(blocking=False):
            return _error_response(BusyError("Another operation is in progress."))
        try:
            body = await request.json()
            name = (body.get("name") or "").strip()
            image = (body.get("image") or "").strip()
            port = body.get("port")
            hostname = (body.get("hostname") or "").strip() or None
            env_vars_raw = (body.get("env_vars") or "").strip()
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
            result = service_ops.create_service(
                get_settings(), name, image, int(port), hostname=hostname, env_vars=env_vars
            )
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_service_create")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        finally:
            busy_lock.release()

    def api_service_update(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        if not busy_lock.acquire(blocking=False):
            return _error_response(BusyError("Another operation is in progress."))
        try:
            result = service_ops.update_service(get_settings(), name)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_service_update")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        finally:
            busy_lock.release()

    def api_service_delete(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        if not busy_lock.acquire(blocking=False):
            return _error_response(BusyError("Another operation is in progress."))
        try:
            result = service_ops.delete_service(get_settings(), name)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_service_delete")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        finally:
            busy_lock.release()

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
            presets = service_presets.list_presets(get_settings())
            return JSONResponse({"ok": True, "presets": presets})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_service_presets")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    async def api_service_restore(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        if not busy_lock.acquire(blocking=False):
            return _error_response(BusyError("Another operation is in progress."))
        try:
            settings = get_settings()
            preset = service_presets.get_preset(settings, name)
            result = service_ops.create_service(
                settings,
                name=preset["name"],
                image=preset["image"],
                port=preset["port"],
                hostname=preset.get("hostname") or None,
                env_vars=preset.get("env_vars") or None,
            )
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_service_restore")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        finally:
            busy_lock.release()

    def api_service_preset_delete(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            service_presets.delete_preset(get_settings(), name)
            return JSONResponse({"ok": True, "result": {"deleted": name}})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_service_preset_delete")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    def api_agents_guide_get(request: Request) -> JSONResponse:
        try:
            import re as _re
            guide_path = os.path.join(get_settings().home, "agents_guide.md")
            content = ""
            if os.path.isfile(guide_path):
                with open(guide_path, "r", encoding="utf-8") as f:
                    content = f.read()
            elif (_TEMPLATE_DIR / "agents_guide.md").is_file():
                content = (_TEMPLATE_DIR / "agents_guide.md").read_text(encoding="utf-8")
            version = 0
            m = _re.search(r"^Version:\s*(\d+)", content, _re.MULTILINE)
            if m:
                version = int(m.group(1))
            return JSONResponse({"ok": True, "content": content, "version": version})
        except Exception as e:
            logger.exception("Unexpected error in api_agents_guide_get")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


    def api_license(request: Request) -> JSONResponse:
        info = get_license_info()
        return JSONResponse({"ok": True, "license": info.to_dict()})

    async def api_license_activate(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            key_text = (body.get("key") or "").strip()
            if not key_text:
                return JSONResponse({"ok": False, "error": "License key is required."}, status_code=400)
            info = install_license_from_text(key_text)
            return JSONResponse({"ok": True, "license": info.to_dict()})
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
        except Exception as e:
            logger.exception("Unexpected error in api_license_activate")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    def api_extra_repos(request: Request) -> JSONResponse:
        from oduflow.extra_addons import list_extra_repos
        try:
            repos = list_extra_repos(get_settings())
            return JSONResponse({"ok": True, "repos": repos})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_extra_repos")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    async def api_extra_repo_add(request: Request) -> JSONResponse:
        if not busy_lock.acquire(blocking=False):
            return _error_response(BusyError("Another operation is in progress."))
        try:
            body = await request.json()
            name = (body.get("name") or "").strip()
            repo_url = (body.get("repo_url") or "").strip()
            if not name or not repo_url:
                return JSONResponse(
                    {"ok": False, "error": "name and repo_url are required."},
                    status_code=400,
                )
            from oduflow.extra_addons import clone_extra_repo
            result = clone_extra_repo(get_settings(), name, repo_url)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_extra_repo_add")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        finally:
            busy_lock.release()

    def api_extra_repo_delete(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        if not busy_lock.acquire(blocking=False):
            return _error_response(BusyError("Another operation is in progress."))
        try:
            from oduflow.extra_addons import delete_extra_repo
            delete_extra_repo(get_settings(), name)
            return JSONResponse({"ok": True, "result": {"deleted": name}})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_extra_repo_delete")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        finally:
            busy_lock.release()

    def api_protect(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            result = env_ops.protect_environment(get_settings(), branch)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_protect")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    def api_unprotect(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        try:
            result = env_ops.unprotect_environment(get_settings(), branch)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_unprotect")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    return [
        Route("/", dashboard, methods=["GET"]),
        Route("/favicon.ico", favicon, methods=["GET"]),
        Route("/api/license", api_license, methods=["GET"]),
        Route("/api/license/activate", api_license_activate, methods=["POST"]),
        Route("/api/templates", api_templates, methods=["GET"]),
        Route("/api/environments", api_list, methods=["GET"]),
        Route("/api/environments/create", api_create, methods=["POST"]),
        Route("/api/stats", api_stats, methods=["GET"]),
        Route("/api/agents-guide", api_agents_guide_get, methods=["GET"]),
        Route("/api/environments/{branch:path}/start", api_start, methods=["POST"]),
        Route("/api/environments/{branch:path}/stop", api_stop, methods=["POST"]),
        Route("/api/environments/{branch:path}/restart", api_restart, methods=["POST"]),
        Route("/api/environments/{branch:path}/protect", api_protect, methods=["POST"]),
        Route("/api/environments/{branch:path}/unprotect", api_unprotect, methods=["POST"]),
        Route("/api/environments/{branch:path}/delete", api_delete, methods=["POST"]),
        Route("/api/services", api_services, methods=["GET"]),
        Route("/api/services/create", api_service_create, methods=["POST"]),
        Route("/api/services/{name}/update", api_service_update, methods=["POST"]),
        Route("/api/services/{name}/delete", api_service_delete, methods=["POST"]),
        Route("/api/services/{name}/logs", api_service_logs, methods=["GET"]),
        Route("/api/service-presets", api_service_presets, methods=["GET"]),
        Route("/api/service-presets/{name}/restore", api_service_restore, methods=["POST"]),
        Route("/api/service-presets/{name}/delete", api_service_preset_delete, methods=["POST"]),
        Route("/api/extra-repos", api_extra_repos, methods=["GET"]),
        Route("/api/extra-repos/add", api_extra_repo_add, methods=["POST"]),
        Route("/api/extra-repos/{name}/delete", api_extra_repo_delete, methods=["POST"]),
        Route("/api/environments/{branch:path}/logs", api_logs, methods=["GET"]),
    ]


def mount_web_ui(
    app,
    get_settings: "callable",
    busy_lock: threading.Lock,
) -> None:
    from starlette.routing import Router

    routes = _build_routes(get_settings, busy_lock)
    sub_app: ASGIApp = Router(routes=routes)

    ui_password = (os.getenv("ODUFLOW_UI_PASSWORD") or "").strip()
    if ui_password:
        sub_app = BasicAuthMiddleware(sub_app, ui_password)
        logger.info("Web UI Basic Auth ENABLED (user: %s)", _AUTH_USER)
    else:
        logger.warning("Web UI auth DISABLED (ODUFLOW_UI_PASSWORD not set)")

    from starlette.routing import Mount
    app.routes.append(Mount("/", app=sub_app))
