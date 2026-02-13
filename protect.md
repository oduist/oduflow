# Environment Protection Feature

## Overview
Add a "Protect" toggle button to the UI that prevents an environment from being deleted. Protection state is stored as a Docker label on the container.

## Storage Mechanism
Docker label `oduflow.protected` on the Odoo container (`"true"` / absent). This follows the existing pattern used for `oduflow.template`, `oduflow.extra_addons`, etc.

---

## Changes

### 1. `src/oduflow/errors.py`
- Add `ProtectedError(FlowError)` with message like `"Environment '{branch}' is protected. Unprotect it first."`

### 2. `src/oduflow/docker_ops/env_ops.py`

#### `delete_environment()` (line ~647)
- Before any destructive action, get the container and check label `oduflow.protected`.
- If label equals `"true"`, raise `ProtectedError`.

#### `list_environments()` (line ~704)
- Add `"protected"` field to the env dict, read from `container.labels.get("oduflow.protected", "") == "true"`.

#### New functions
```python
def protect_environment(settings: Settings, branch_name: str) -> dict[str, str]:
    """Set oduflow.protected=true label on the Odoo container."""
    client = get_client()
    container_name = get_resource_name(branch_name, "odoo", settings.prefix)
    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Environment '{branch_name}' does not exist.")
    # Docker SDK doesn't support updating labels on a running container directly.
    # We must stop, remove, and re-create — BUT that's heavyweight.
    # Instead, use low-level API: docker container update doesn't support labels either.
    # Simplest approach: use `container.rename` trick won't work.
    # Best approach: store protection state in a file inside the workspace directory.
```

**REVISED storage approach** (Docker labels are immutable after creation):
- Store protection in a simple file: `<workspace_path>/.protected` (empty file, presence = protected).
- This is simpler, survives container rebuilds, and doesn't require container recreation.

#### Revised functions in `env_ops.py`
```python
def protect_environment(settings: Settings, branch_name: str) -> dict[str, str]:
    """Mark environment as protected by creating .protected marker file."""
    client = get_client()
    container_name = get_resource_name(branch_name, "odoo", settings.prefix)
    try:
        client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Environment '{branch_name}' does not exist.")
    workspace_path = get_workspace_path(branch_name, settings.workspaces_dir)
    marker = os.path.join(workspace_path, ".protected")
    open(marker, "w").close()
    logger.info("Environment protected", extra={"branch": branch_name})
    return {"branch": branch_name, "protected": True}


def unprotect_environment(settings: Settings, branch_name: str) -> dict[str, str]:
    """Remove protection from environment by deleting .protected marker file."""
    client = get_client()
    container_name = get_resource_name(branch_name, "odoo", settings.prefix)
    try:
        client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Environment '{branch_name}' does not exist.")
    workspace_path = get_workspace_path(branch_name, settings.workspaces_dir)
    marker = os.path.join(workspace_path, ".protected")
    if os.path.exists(marker):
        os.remove(marker)
    logger.info("Environment unprotected", extra={"branch": branch_name})
    return {"branch": branch_name, "protected": False}


def is_protected(settings: Settings, branch_name: str) -> bool:
    workspace_path = get_workspace_path(branch_name, settings.workspaces_dir)
    return os.path.exists(os.path.join(workspace_path, ".protected"))
```

#### `delete_environment()` — add guard at the top (after line 647)
```python
if is_protected(settings, branch_name):
    raise ProtectedError(f"Environment '{branch_name}' is protected. Unprotect it before deleting.")
```

#### `list_environments()` — add field (after line 716)
```python
"protected": is_protected(settings, branch),
```

### 3. `src/oduflow/web_ui.py`

#### New endpoints
```python
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
```

#### Routes (add to `_build_routes` return list, ~line 465)
```python
Route("/api/environments/{branch:path}/protect", api_protect, methods=["POST"]),
Route("/api/environments/{branch:path}/unprotect", api_unprotect, methods=["POST"]),
```

### 4. `src/oduflow/server.py`
No changes needed — `delete_environment` MCP tool already calls `env_ops.delete_environment()` which will now raise `ProtectedError`. The `@handle_errors` decorator will convert it to an error message for the agent.

### 5. `templates/dashboard.html`

#### CSS (after `.btn-logs` styles, ~line 130)
```css
.btn-protect { border-color: var(--yellow); color: var(--yellow); }
.btn-protect:hover { background: rgba(210,153,34,0.1); }
.btn-protect.active { background: var(--yellow); color: #000; border-color: var(--yellow); }
```

#### Button in `renderEnvironments()` (after Logs button, before Delete button, ~line 691)
```javascript
'<button class="btn btn-protect' + (env.protected ? ' active' : '') + '" ' +
  'onclick="doProtect(\'' + escAttr(env.branch) + '\',' + (env.protected ? 'true' : 'false') + ')">' +
  (env.protected ? '🔒 Protected' : '🔓 Protect') + '</button>' +
```

#### Delete button — disable when protected (~line 691)
```javascript
'<button class="btn btn-delete" onclick="doDelete(\'' + escAttr(env.branch) + '\')" ' +
  (env.protected ? 'disabled title="Environment is protected"' : '') + '>Delete</button>' +
```

#### JS function `doProtect()` (after `doDelete`, ~line 765)
```javascript
async function doProtect(branch, isProtected) {
  const action = isProtected ? 'unprotect' : 'protect';
  if (isProtected && !confirm('Remove protection from "' + branch + '"?')) return;
  try {
    const r = await fetch(API + '/' + encodeURIComponent(branch) + '/' + action, { method: 'POST' });
    const data = await r.json();
    if (data.ok) {
      showToast(branch + ': ' + (isProtected ? 'unprotected' : 'protected'));
    } else {
      showToast(data.error || action + ' failed', true);
    }
  } catch (e) {
    showToast('Connection error: ' + e.message, true);
  }
  await loadEnvironments();
}
```

---

## File Summary

| File | Action |
|------|--------|
| `src/oduflow/errors.py` | Add `ProtectedError` class |
| `src/oduflow/docker_ops/env_ops.py` | Add `protect_environment()`, `unprotect_environment()`, `is_protected()`; guard in `delete_environment()`; field in `list_environments()` |
| `src/oduflow/web_ui.py` | Add `api_protect`, `api_unprotect` handlers + routes |
| `src/oduflow/server.py` | No changes (inherits protection via `env_ops.delete_environment`) |
| `templates/dashboard.html` | CSS for `.btn-protect`, toggle button, disable Delete when protected, `doProtect()` JS function |
