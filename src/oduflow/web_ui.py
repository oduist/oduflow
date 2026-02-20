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

_TEMPLATE_DIR = pathlib.Path(__file__).resolve().parent / "templates"


def _error_response(e: FlowError) -> JSONResponse:
    if isinstance(e, NotFoundError):
        status = 404
    elif isinstance(e, BusyError):
        status = 409
    else:
        status = 400
    return JSONResponse({"ok": False, "error": str(e)}, status_code=status)


def _normalize_extra_addons(raw_addons, fallback_branch: str) -> dict[str, str]:
    if isinstance(raw_addons, dict):
        return raw_addons
    if isinstance(raw_addons, list):
        return {name: fallback_branch for name in raw_addons}
    return {}

def _parse_extra_addons(raw: str, fallback_branch: str) -> dict[str, str]:
    result = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            name, branch = item.split(":", 1)
            result[name.strip()] = branch.strip()
        else:
            result[item] = fallback_branch
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
    busy_lock: threading.Lock,
) -> list[Route]:

    def dashboard(request: Request) -> HTMLResponse:
        html_path = _TEMPLATE_DIR / "dashboard.html"
        return HTMLResponse(html_path.read_text(encoding="utf-8"))

    def favicon(request: Request) -> Response:
        ico_path = _TEMPLATE_DIR / "favicon.ico"
        return Response(ico_path.read_bytes(), media_type="image/x-icon")

    def logo(request: Request) -> Response:
        logo_path = _TEMPLATE_DIR / "logo.png"
        return Response(logo_path.read_bytes(), media_type="image/png")

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

    def api_sync(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        if not busy_lock.acquire(blocking=False):
            return _error_response(BusyError("Another operation is in progress."))
        try:
            result = env_ops.pull_environment(get_settings(), branch)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_sync")
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

    def api_recreate(request: Request) -> JSONResponse:
        branch = request.path_params["branch"]
        if not busy_lock.acquire(blocking=False):
            return _error_response(BusyError("Another operation is in progress."))
        try:
            import docker as _docker
            from oduflow.docker_ops.client import get_client as _get_client

            settings = get_settings()
            client = _get_client()
            odoo_container_name = env_ops.get_resource_name(branch, "odoo", settings.prefix)
            try:
                container = client.containers.get(odoo_container_name)
                labels = container.labels
            except _docker.errors.NotFound:
                return _error_response(NotFoundError(f"Environment '{branch}' not found."))

            repo_url = labels.get(settings.repo_label, "")
            odoo_image = labels.get(settings.image_label, "")
            template_raw = labels.get("oduflow.template", "")
            template_name = template_raw if template_raw and template_raw != "none" else None
            extra_addons_raw = labels.get("oduflow.extra_addons", "{}")
            extra_addons = json.loads(extra_addons_raw) or None
            git_user = labels.get("oduflow.git_user", "")

            env_ops.delete_environment(settings, branch)
            result = env_ops.create_environment(
                settings, branch, repo_url, odoo_image,
                template_name=template_name,
                extra_addons=extra_addons,
                git_user=git_user,
            )
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_recreate")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        finally:
            busy_lock.release()

    async def api_create(request: Request) -> JSONResponse:
        if not busy_lock.acquire(blocking=False):
            return _error_response(BusyError("Another operation is in progress."))
        try:
            import json as _json
            body = await request.json()
            branch_name = (body.get("branch_name") or "").strip()
            repo_url = (body.get("repo_url") or "").strip()
            odoo_image = (body.get("odoo_image") or "").strip()
            git_user = (body.get("git_user") or "").strip()
            template_name_raw = (body.get("template_name") or "").strip()
            extra_addons_raw = body.get("extra_addons")
            if not branch_name:
                return JSONResponse(
                    {"ok": False, "error": "branch_name is required."},
                    status_code=400,
                )
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
                extra_dict = _parse_extra_addons(extra_addons_raw.strip(), settings.default_branch) or None
            if resolved_template:
                metadata_path = settings.get_template_metadata_path(resolved_template)
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
                            extra_dict = _normalize_extra_addons(raw, settings.default_branch) or None

            if not repo_url or not odoo_image:
                return JSONResponse(
                    {"ok": False, "error": "repo_url and odoo_image are required (not found in template metadata either)."},
                    status_code=400,
                )
            result = env_ops.create_environment(
                settings, branch_name, repo_url, odoo_image,
                template_name=resolved_template,
                extra_addons=extra_dict,
                git_user=git_user,
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

    def api_agent_guides_list(request: Request) -> JSONResponse:
        try:
            guides = []
            guides_dir = os.path.join(get_settings().home, "agent_guides")
            bundled_dir = _TEMPLATE_DIR / "agent_guides"
            seen = set()
            if os.path.isdir(guides_dir):
                for fname in sorted(os.listdir(guides_dir)):
                    if fname.endswith(".md"):
                        seen.add(fname)
                        guides.append({"filename": fname, "title": _guide_title(os.path.join(guides_dir, fname))})
            if bundled_dir.is_dir():
                for fpath in sorted(bundled_dir.iterdir()):
                    if fpath.suffix == ".md" and fpath.name not in seen:
                        guides.append({"filename": fpath.name, "title": _guide_title(str(fpath))})
            return JSONResponse({"ok": True, "guides": guides})
        except Exception as e:
            logger.exception("Unexpected error in api_agent_guides_list")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    def api_agent_guide_get(request: Request) -> JSONResponse:
        try:
            filename = request.path_params["filename"]
            if not filename.endswith(".md") or "/" in filename or "\\" in filename:
                return JSONResponse({"ok": False, "error": "Invalid filename"}, status_code=400)
            guides_dir = os.path.join(get_settings().home, "agent_guides")
            guide_path = os.path.join(guides_dir, filename)
            content = ""
            if os.path.isfile(guide_path):
                with open(guide_path, "r", encoding="utf-8") as f:
                    content = f.read()
            elif (_TEMPLATE_DIR / "agent_guides" / filename).is_file():
                content = (_TEMPLATE_DIR / "agent_guides" / filename).read_text(encoding="utf-8")
            else:
                return JSONResponse({"ok": False, "error": "Guide not found"}, status_code=404)
            return JSONResponse({"ok": True, "content": content, "filename": filename})
        except Exception as e:
            logger.exception("Unexpected error in api_agent_guide_get")
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
            git_user = (body.get("git_user") or "").strip()
            if not name or not repo_url:
                return JSONResponse(
                    {"ok": False, "error": "name and repo_url are required."},
                    status_code=400,
                )
            from oduflow.extra_addons import clone_extra_repo
            result = clone_extra_repo(get_settings(), name, repo_url, git_user=git_user)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_extra_repo_add")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        finally:
            busy_lock.release()

    async def api_extra_repo_pull(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        if not busy_lock.acquire(blocking=False):
            return _error_response(BusyError("Another operation is in progress."))
        try:
            from oduflow.extra_addons import fetch_extra_repo
            fetch_extra_repo(get_settings(), name)
            return JSONResponse({"ok": True, "result": {"pulled": name}})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_extra_repo_pull")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
        finally:
            busy_lock.release()

    def api_extra_repo_protect(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            from oduflow.extra_addons import protect_extra_repo
            result = protect_extra_repo(get_settings(), name)
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
            result = unprotect_extra_repo(get_settings(), name)
            return JSONResponse({"ok": True, "result": result})
        except FlowError as e:
            return _error_response(e)
        except Exception as e:
            logger.exception("Unexpected error in api_extra_repo_unprotect")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

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

    def api_credentials(request: Request) -> JSONResponse:
        from oduflow.git_ops import list_credentials
        try:
            creds = list_credentials()
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
            result = git_ops.setup_repo_auth(repo_url)
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
            removed = delete_credential(host, username)
            if not removed:
                return JSONResponse(
                    {"ok": False, "error": f"Credential not found for {host}/{username}."},
                    status_code=404,
                )
            return JSONResponse({"ok": True, "result": {"host": host, "username": username}})
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
            status = validate_credential(host, username)
            return JSONResponse({"ok": True, "status": status})
        except Exception as e:
            logger.exception("Unexpected error in api_credential_validate")
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

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
        Route("/logo.png", logo, methods=["GET"]),
        Route("/api/license", api_license, methods=["GET"]),
        Route("/api/license/activate", api_license_activate, methods=["POST"]),
        Route("/api/templates", api_templates, methods=["GET"]),
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
        Route("/api/environments/{branch:path}/unprotect", api_unprotect, methods=["POST"]),
        Route("/api/environments/{branch:path}/recreate", api_recreate, methods=["POST"]),
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
        Route("/api/extra-repos/{name}/pull", api_extra_repo_pull, methods=["POST"]),
        Route("/api/extra-repos/{name}/protect", api_extra_repo_protect, methods=["POST"]),
        Route("/api/extra-repos/{name}/unprotect", api_extra_repo_unprotect, methods=["POST"]),
        Route("/api/extra-repos/{name}/delete", api_extra_repo_delete, methods=["POST"]),
        Route("/api/credentials", api_credentials, methods=["GET"]),
        Route("/api/credentials/add", api_credential_add, methods=["POST"]),
        Route("/api/credentials/delete", api_credential_delete, methods=["POST"]),
        Route("/api/credentials/validate", api_credential_validate, methods=["POST"]),
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
