# Extra Addons — Implementation Spec

## Overview

Add support for **extra addon repositories** (e.g. Odoo Enterprise) that are cloned once
at the instance level and then attached to individual environments via git worktrees.

Each environment can reference one or more extra repos and specify a branch.
The extra addons are mounted read-only into the Odoo container and automatically
added to `addons_path` in `odoo.conf`.

---

## Architecture

```
$ODUFLOW_HOME/
  shared_repos/
    enterprise/          ← bare git clone
    custom-themes/       ← bare git clone
  workspaces/
    feature-x/
      repo/              ← main project repo (existing)
      extra/
        enterprise/      ← git worktree (branch 17.0)
        custom-themes/   ← git worktree (branch main)
      odoo.conf          ← generated config with merged addons_path
```

### Flow

1. **Admin clones an extra repo once** (via UI or MCP tool) →
   `git clone --bare <url> $ODUFLOW_HOME/shared_repos/<name>`
2. **When creating an environment** with `extra_addons=["enterprise"]`
   and `extra_addons_branch="17.0"`:
   - For each extra repo: `git fetch` in the bare repo, then
     `git worktree add <workspace>/extra/<name> <branch>`
   - Generate `odoo.conf` with merged `addons_path`
   - Mount worktree into container as `/mnt/extra-addons-<name>` (read-only)
3. **On environment delete** — remove worktrees and clean up

---

## File-by-file Changes

### 1. `src/oduflow/settings.py`

Add a computed property for the shared repos directory:

```python
@property
def shared_repos_dir(self) -> str:
    return os.path.join(self.home, "shared_repos")
```

**Note:** `Settings` is a frozen dataclass, so use `@property` (not a stored field).

---

### 2. NEW: `src/oduflow/extra_addons.py`

New module with all extra-repo logic. Functions:

#### `clone_extra_repo(settings, name: str, repo_url: str) -> dict`
- Validate `name` (alphanumeric + hyphens only, no path traversal)
- Target: `settings.shared_repos_dir / name`
- If already exists → raise `ConflictError`
- `os.makedirs(settings.shared_repos_dir, exist_ok=True)`
- `git clone --bare <repo_url> <target>`
- Handle auth errors the same way as `env_ops.py` (check for auth keywords)
- Return `{"name": name, "repo_url": repo_url, "path": target}`

#### `list_extra_repos(settings) -> list[dict]`
- Scan `settings.shared_repos_dir` for directories
- For each: read origin URL from `git -C <path> config --get remote.origin.url`
- List branches: `git -C <path> branch -a --format='%(refname:short)'`
- Return `[{"name": ..., "repo_url": ..., "branches": [...]}]`

#### `delete_extra_repo(settings, name: str) -> dict`
- Path: `settings.shared_repos_dir / name`
- If not exists → raise `NotFoundError`
- Check if any running environments reference this repo (via container labels
  `oduflow.extra_addons`). If so → raise `ConflictError` with message listing
  dependent environments.
- `shutil.rmtree(path)`
- Return `{"name": name, "deleted": True}`

#### `fetch_extra_repo(settings, name: str) -> None`
- `git -C <path> fetch --all --prune`
- Called internally before creating a worktree

#### `create_worktree(settings, repo_name: str, branch: str, target_path: str) -> str`
- Bare repo path: `settings.shared_repos_dir / repo_name`
- If not exists → raise `NotFoundError("Extra repo '<name>' not found. Add it first.")`
- `fetch_extra_repo(settings, repo_name)` first
- `git -C <bare_path> worktree add <target_path> <branch>`
- If branch not found → raise `ExternalCommandError` with clear message
- Return `target_path`

#### `remove_worktree(settings, repo_name: str, target_path: str) -> None`
- `git -C <bare_path> worktree remove <target_path> --force`
- Silently ignore errors (environment may already be partially cleaned up)

#### `generate_odoo_conf(base_conf_path: str, output_path: str, extra_paths: list[str]) -> str`
- Read `base_conf_path` using `configparser.ConfigParser()`
  - Use `configparser.RawConfigParser()` to avoid `%` interpolation issues
  - Set `optionxform = str` to preserve case
- Read existing `addons_path` from `[options]` section (default: `/mnt/extra-addons`)
- Append each path from `extra_paths` to the comma-separated list
- Write result to `output_path`
- Return `output_path`

---

### 3. `src/oduflow/docker_ops/env_ops.py`

#### `create_environment()` — modify signature:

```python
def create_environment(
    settings: Settings,
    branch_name: str,
    repo_url: str,
    odoo_image: str = "odoo:15.0",
    template_name: str | None = "",
    extra_addons: list[str] | None = None,      # NEW
    extra_addons_branch: str = "",                # NEW
) -> dict[str, str]:
```

#### Changes inside `create_environment()`:

**After the main repo clone block (after line ~466), before DB creation:**

```python
# --- Extra addons worktrees ---
extra_mount_paths = []  # list of (host_path, container_path) tuples
if extra_addons:
    from oduflow.extra_addons import create_worktree
    extra_dir = os.path.join(workspace_path, "extra")
    os.makedirs(extra_dir, exist_ok=True)
    for repo_name in extra_addons:
        wt_path = os.path.join(extra_dir, repo_name)
        branch = extra_addons_branch or settings.default_branch
        create_worktree(settings, repo_name, branch, wt_path)
        container_path = f"/mnt/extra-addons-{repo_name}"
        extra_mount_paths.append((wt_path, container_path))
```

**Labels — add extra addons info (after line ~359):**

```python
import json
if extra_addons:
    labels["oduflow.extra_addons"] = json.dumps(extra_addons)
    labels["oduflow.extra_addons_branch"] = extra_addons_branch or settings.default_branch
```

**Volumes — add extra mounts (after line ~487 where `odoo_volumes` is built):**

```python
for host_path, container_path in extra_mount_paths:
    odoo_volumes[host_path] = {"bind": container_path, "mode": "ro"}
```

**odoo.conf generation — replace the current static mount logic (lines 488-500):**

Instead of directly mounting the source `odoo.conf`, generate a merged one:

```python
from oduflow.extra_addons import generate_odoo_conf

# Determine base odoo.conf
repo_odoo_conf = os.path.join(repo_path, "odoo.conf")
if os.path.isfile(repo_odoo_conf):
    base_conf_path = repo_odoo_conf
    logger.info("Using odoo.conf from repository")
elif _ODOO_CONF_TEMPLATE.exists():
    base_conf_path = str(_ODOO_CONF_TEMPLATE)
else:
    base_conf_path = None

if base_conf_path:
    if extra_mount_paths:
        # Generate merged odoo.conf with extra addons_path entries
        generated_conf = os.path.join(workspace_path, "odoo.conf")
        extra_container_paths = [cp for _, cp in extra_mount_paths]
        generate_odoo_conf(base_conf_path, generated_conf, extra_container_paths)
        odoo_conf_to_mount = generated_conf
    else:
        odoo_conf_to_mount = base_conf_path

    odoo_volumes[odoo_conf_to_mount] = {
        "bind": "/etc/odoo/odoo.conf",
        "mode": "ro",
    }
```

**Return value — include extra_addons info:**

```python
result["extra_addons"] = extra_addons or []
result["extra_addons_branch"] = extra_addons_branch or ""
```

#### `delete_environment()` — add worktree cleanup:

Before `shutil.rmtree(workspace_path)`, add:

```python
# Clean up extra addons worktrees
extra_dir = os.path.join(workspace_path, "extra")
if os.path.isdir(extra_dir):
    from oduflow.extra_addons import remove_worktree
    for repo_name in os.listdir(extra_dir):
        wt_path = os.path.join(extra_dir, repo_name)
        if os.path.isdir(wt_path):
            remove_worktree(settings, repo_name, wt_path)
```

#### `rebuild_environment()` — restore extra mounts from labels:

In the section that reconstructs volumes from the old container (around line ~914-922),
the existing code already preserves all bind mounts from the old container. Since
extra addons are stored as regular bind mounts, they will be automatically preserved
by the existing rebuild logic. **No changes needed here** — just verify this works.

However, if a worktree mount point is not mounted (e.g. after host reboot), we need
to check and warn. Add after the filestore re-mount block (line ~980):

```python
# Verify extra addons worktrees are intact
extra_addons_json = labels.get("oduflow.extra_addons", "")
if extra_addons_json:
    import json
    extra_names = json.loads(extra_addons_json)
    extra_dir = os.path.join(get_workspace_path(branch_name, settings.workspaces_dir), "extra")
    for rn in extra_names:
        wt = os.path.join(extra_dir, rn)
        if not os.path.isdir(wt):
            logger.warning("Extra addons worktree missing: %s", wt)
```

---

### 4. `src/oduflow/server.py`

#### New MCP tools:

```python
@mcp.tool()
@handle_errors
@with_mutex
def add_extra_repo(name: str, repo_url: str) -> str:
    """
    Clone an extra addons repository for use with environments.

    The repository is cloned as a bare repo to the shared repos directory.
    When creating an environment, reference it by name to mount it as
    additional addons (e.g., Odoo Enterprise).

    Args:
        name: Short name for the repo (e.g. "enterprise", "custom-themes").
        repo_url: Git URL of the repository (HTTPS or SSH).
    """
    from oduflow.extra_addons import clone_extra_repo
    result = clone_extra_repo(_get_settings(), name, repo_url)
    return f"Extra repo '{result['name']}' cloned successfully.\nPath: {result['path']}"


@mcp.tool()
@handle_errors
def list_extra_repos() -> str:
    """List all cloned extra addons repositories."""
    from oduflow.extra_addons import list_extra_repos as _list
    repos = _list(_get_settings())
    if not repos:
        return "No extra addons repositories found."
    lines = ["Extra addons repositories:"]
    for r in repos:
        branches = ", ".join(r["branches"][:10]) if r["branches"] else "(no branches)"
        lines.append(f"- {r['name']}: {r['repo_url']} [{branches}]")
    return "\n".join(lines)


@mcp.tool()
@handle_errors
@with_mutex
def delete_extra_repo(name: str) -> str:
    """
    Delete a cloned extra addons repository.

    Args:
        name: Name of the extra repo to delete.
    """
    from oduflow.extra_addons import delete_extra_repo as _delete
    _delete(_get_settings(), name)
    return f"Extra repo '{name}' deleted."
```

#### Modify `create_environment` tool:

Add parameters:

```python
def create_environment(
    branch_name: str,
    repo_url: str,
    odoo_image: str,
    template_name: str = "",
    extra_addons: str = "",          # NEW — comma-separated repo names
    extra_addons_branch: str = "",   # NEW — branch for extra repos
) -> str:
```

Parse `extra_addons` into a list:

```python
extra_list = [x.strip() for x in extra_addons.split(",") if x.strip()] if extra_addons else []
```

Pass to `env_ops.create_environment()`:

```python
result = env_ops.create_environment(
    settings, branch_name, repo_url, odoo_image,
    template_name=resolved_template,
    extra_addons=extra_list or None,
    extra_addons_branch=extra_addons_branch,
)
```

Add to output:

```python
if extra_list:
    lines.append(f"Extra Addons: {', '.join(extra_list)} (branch: {extra_addons_branch or settings.default_branch})")
```

---

### 5. `src/oduflow/web_ui.py`

#### New API endpoints:

```python
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
```

#### Register routes (add to the routes list):

```python
Route("/api/extra-repos", api_extra_repos, methods=["GET"]),
Route("/api/extra-repos/add", api_extra_repo_add, methods=["POST"]),
Route("/api/extra-repos/{name}/delete", api_extra_repo_delete, methods=["POST"]),
```

---

### 6. `templates/dashboard.html`

#### New tab button (after "Services", before "Templates"):

```html
<button class="tab-btn" data-tab="extra-addons" onclick="switchTab('extra-addons')">Extra Addons</button>
```

#### New tab content (after `tab-services` div):

```html
<div id="tab-extra-addons" class="tab-content">
  <div style="display:flex;justify-content:flex-end;margin-bottom:12px;">
    <button class="btn-create" onclick="openAddExtraRepoModal()">+ Add Repo</button>
  </div>
  <div id="extra-repos-list"></div>
</div>
```

#### New modal — Add Extra Repo:

```html
<div class="modal-overlay" id="add-extra-repo-modal" onclick="closeAddExtraRepoModal(event)">
  <div class="modal" style="max-width:500px;">
    <div class="modal-header">
      <h2>Add Extra Addons Repository</h2>
      <button class="modal-close" onclick="closeAddExtraRepoModal()">&times;</button>
    </div>
    <div class="modal-body">
      <div class="form-error" id="add-extra-repo-error"></div>
      <div class="form-group">
        <label for="extra-repo-name">Name</label>
        <input type="text" id="extra-repo-name" placeholder="e.g. enterprise, custom-themes">
      </div>
      <div class="form-group">
        <label for="extra-repo-url">Repository URL</label>
        <input type="text" id="extra-repo-url" placeholder="https://github.com/odoo/enterprise.git">
      </div>
      <div class="form-actions">
        <button class="btn" onclick="closeAddExtraRepoModal()">Cancel</button>
        <button class="btn-create" id="extra-repo-submit" onclick="submitAddExtraRepo()">Clone</button>
      </div>
    </div>
  </div>
</div>
```

#### JavaScript — Extra Addons section:

```javascript
const API_EXTRA_REPOS = '/api/extra-repos';
let cachedExtraRepos = [];

async function loadExtraRepos() {
  try {
    var r = await fetch(API_EXTRA_REPOS);
    var data = await r.json();
    if (data.ok) {
      cachedExtraRepos = data.repos || [];
      renderExtraRepos(cachedExtraRepos);
    } else {
      showToast(data.error || 'Failed to load extra repos', true);
    }
  } catch (e) {
    showToast('Connection error: ' + e.message, true);
  }
}

function renderExtraRepos(repos) {
  var el = document.getElementById('extra-repos-list');
  if (!repos || repos.length === 0) {
    el.innerHTML = '<div class="empty">No extra addons repositories.<br>Add one to use with environments.</div>';
    return;
  }
  el.innerHTML = repos.map(function(r) {
    var branches = (r.branches || []).slice(0, 10).map(function(b) { return esc(b); }).join(', ');
    return '<div class="svc-card">' +
      '<div class="svc-header">' +
        '<div class="left">' +
          '<span class="svc-name">' + esc(r.name) + '</span>' +
        '</div>' +
        '<div class="svc-actions">' +
          '<button class="btn btn-delete" onclick="doDeleteExtraRepo(\'' + escAttr(r.name) + '\')">Delete</button>' +
        '</div>' +
      '</div>' +
      '<div class="svc-meta">' +
        '<span>URL: <strong>' + esc(r.repo_url) + '</strong></span>' +
      '</div>' +
      (branches ? '<div class="svc-meta"><span>Branches: <strong>' + branches + '</strong></span></div>' : '') +
    '</div>';
  }).join('');
}

function openAddExtraRepoModal() {
  document.getElementById('extra-repo-name').value = '';
  document.getElementById('extra-repo-url').value = '';
  document.getElementById('add-extra-repo-error').style.display = 'none';
  document.getElementById('extra-repo-submit').disabled = false;
  document.getElementById('extra-repo-submit').textContent = 'Clone';
  document.getElementById('add-extra-repo-modal').classList.add('show');
  document.getElementById('extra-repo-name').focus();
}

function closeAddExtraRepoModal(event) {
  if (event && event.target !== event.currentTarget) return;
  document.getElementById('add-extra-repo-modal').classList.remove('show');
}

async function submitAddExtraRepo() {
  var name = document.getElementById('extra-repo-name').value.trim();
  var url = document.getElementById('extra-repo-url').value.trim();
  var errEl = document.getElementById('add-extra-repo-error');
  if (!name || !url) {
    errEl.textContent = 'Name and repository URL are required.';
    errEl.style.display = 'block';
    return;
  }
  errEl.style.display = 'none';
  var btn = document.getElementById('extra-repo-submit');
  btn.disabled = true;
  btn.textContent = 'Cloning...';
  try {
    var r = await fetch(API_EXTRA_REPOS + '/add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: name, repo_url: url})
    });
    var data = await r.json();
    if (data.ok) {
      closeAddExtraRepoModal();
      showToast('Extra repo "' + name + '" cloned');
      await loadExtraRepos();
    } else {
      errEl.textContent = data.error || 'Clone failed';
      errEl.style.display = 'block';
    }
  } catch (e) {
    errEl.textContent = 'Connection error: ' + e.message;
    errEl.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Clone';
  }
}

async function doDeleteExtraRepo(name) {
  if (!confirm('Delete extra repo "' + name + '"? The cloned repository will be removed.')) return;
  try {
    var r = await fetch(API_EXTRA_REPOS + '/' + encodeURIComponent(name) + '/delete', {method:'POST'});
    var data = await r.json();
    if (data.ok) {
      showToast('Extra repo "' + name + '" deleted');
      await loadExtraRepos();
    } else {
      showToast(data.error || 'Delete failed', true);
    }
  } catch (e) {
    showToast('Connection error: ' + e.message, true);
  }
}
```

#### Modify `switchTab()` — add extra-addons loading:

```javascript
if (name === 'extra-addons') loadExtraRepos();
```

#### Modify Create Environment modal — add extra addons fields:

After the Template `<select>` form-group, add:

```html
<div class="form-group">
  <label>Extra Addons Repos</label>
  <div id="cr-extra-addons-checkboxes" style="max-height:120px;overflow-y:auto;padding:4px 0;"></div>
  <div style="font-size:11px;color:var(--text-dim);margin-top:4px;">Select repos to mount in this environment</div>
</div>
<div class="form-group" id="cr-extra-branch-group" style="display:none;">
  <label for="cr-extra-branch">Extra Addons Branch</label>
  <input type="text" id="cr-extra-branch" placeholder="e.g. 17.0 (default: use default branch)">
</div>
```

#### Modify `openCreateModal()` — populate extra addons checkboxes:

```javascript
loadExtraRepos().then(function() {
  var container = document.getElementById('cr-extra-addons-checkboxes');
  if (cachedExtraRepos.length === 0) {
    container.innerHTML = '<span style="font-size:12px;color:var(--text-dim);">No extra repos available</span>';
    document.getElementById('cr-extra-branch-group').style.display = 'none';
    return;
  }
  container.innerHTML = cachedExtraRepos.map(function(r) {
    return '<label style="display:flex;align-items:center;gap:8px;padding:3px 0;font-size:13px;cursor:pointer;">' +
      '<input type="checkbox" class="cr-extra-cb" value="' + escAttr(r.name) + '" onchange="toggleExtraBranchField()">' +
      esc(r.name) + ' <span style="color:var(--text-dim);font-size:11px;">(' + esc(r.repo_url) + ')</span>' +
    '</label>';
  }).join('');
  document.getElementById('cr-extra-branch-group').style.display = 'none';
});
```

```javascript
function toggleExtraBranchField() {
  var checked = document.querySelectorAll('.cr-extra-cb:checked');
  document.getElementById('cr-extra-branch-group').style.display =
    checked.length > 0 ? 'block' : 'none';
}
```

#### Modify `submitCreate()` — include extra addons in payload:

```javascript
var extraAddons = Array.from(document.querySelectorAll('.cr-extra-cb:checked'))
  .map(function(cb) { return cb.value; });
var extraBranch = document.getElementById('cr-extra-branch').value.trim();

var payload = {
  branch_name: branch,
  repo_url: repo,
  odoo_image: image,
  template_name: template,
  extra_addons: extraAddons.join(','),           // NEW
  extra_addons_branch: extraBranch,               // NEW
};
```

#### Modify `api_create()` in `web_ui.py` — pass extra addons:

```python
extra_addons_raw = (body.get("extra_addons") or "").strip()
extra_addons_branch = (body.get("extra_addons_branch") or "").strip()
extra_list = [x.strip() for x in extra_addons_raw.split(",") if x.strip()] if extra_addons_raw else None

result = env_ops.create_environment(
    get_settings(), branch_name, repo_url, odoo_image,
    template_name=resolved_template,
    extra_addons=extra_list,
    extra_addons_branch=extra_addons_branch,
)
```

#### Display extra addons info in environment cards:

In `renderEnvironments()`, add after repo meta:

```javascript
// Show extra addons if present
var extraHtml = '';
if (env.extra_addons && env.extra_addons.length > 0) {
  extraHtml = '<div class="env-meta"><span>Extra: <strong>' +
    env.extra_addons.map(function(n){ return esc(n); }).join(', ') +
    '</strong></span>' +
    (env.extra_addons_branch ? '<span>Branch: <strong>' + esc(env.extra_addons_branch) + '</strong></span>' : '') +
  '</div>';
}
```

And include `extraHtml` in the card template after `metaHtml`.

---

### 7. `list_environments()` in `env_ops.py`

Add extra addons info to the returned environment data by reading labels:

```python
import json

extra_addons_json = container.labels.get("oduflow.extra_addons", "")
extra_addons = json.loads(extra_addons_json) if extra_addons_json else []
extra_addons_branch = container.labels.get("oduflow.extra_addons_branch", "")

# Add to the environment dict:
env_info["extra_addons"] = extra_addons
env_info["extra_addons_branch"] = extra_addons_branch
```

---

## Important Implementation Notes

1. **`configparser` quirks**: Odoo's config uses `%` in some values (e.g. log format).
   Use `configparser.RawConfigParser()` to avoid interpolation errors.

2. **Bare clone**: Use `git clone --bare` so the shared repo is lightweight and
   supports multiple worktrees efficiently. The bare repo has no working directory.

3. **Worktree branch**: All extra repos in one environment use the same branch
   (`extra_addons_branch`). This is by design — enterprise/themes repos typically
   track Odoo version branches (e.g. `17.0`, `16.0`).

4. **Auth**: Extra repos may be private (e.g. Odoo Enterprise). The existing
   `setup_repo_auth` mechanism caches git credentials globally, so it works
   for extra repo cloning too. The error messages should hint at using
   `setup_repo_auth` if authentication fails.

5. **Read-only mount**: Extra addons are mounted `ro` because they come from
   shared bare repos. The project's own repo stays `rw` as before.

6. **`odoo.conf` generation**: Only generate a custom file when extra addons are
   present. When no extra addons — keep existing behavior (mount original file
   directly).

7. **Validation**: The `name` parameter for extra repos must be validated:
   only `[a-zA-Z0-9_-]` characters, no `.`, no `/`, max 63 chars.

8. **No auto-pull for extra repos**: Extra repos are NOT pulled on
   `pull_environment`. They are static per environment. To update, delete
   and recreate the environment.

---

## Testing Checklist

- [ ] Clone an extra repo via MCP tool
- [ ] Clone an extra repo via Web UI
- [ ] List extra repos (MCP + UI)
- [ ] Delete an extra repo (MCP + UI, verify conflict check)
- [ ] Create environment WITHOUT extra addons (no regression)
- [ ] Create environment WITH extra addons via MCP tool
- [ ] Create environment WITH extra addons via Web UI
- [ ] Verify worktree created in workspace
- [ ] Verify `odoo.conf` has merged `addons_path`
- [ ] Verify extra addons mounted read-only in container
- [ ] Verify Odoo can see/install modules from extra addons
- [ ] Delete environment with extra addons (worktree cleanup)
- [ ] Rebuild environment with extra addons (mounts preserved)
- [ ] Private extra repo with `setup_repo_auth`
- [ ] Create env with user's own `odoo.conf` in repo + extra addons
