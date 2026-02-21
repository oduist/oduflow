# Environment Management

[TOC]

## Creating Environments

```bash
# Create from existing branch with default template
oduflow call create_environment feature-login https://github.com/owner/repo.git odoo:17.0

# Create with a named template
oduflow call create_environment feature-login https://github.com/owner/repo.git odoo:17.0 myproject

# Create without a template (fresh Odoo with -i base)
oduflow call create_environment feature-login https://github.com/owner/repo.git odoo:17.0 none
```

When creating an environment, Oduflow:

1. **Clones the repository** — shallow clone (`--depth 1`) for speed
2. **Creates the database** — `CREATE DATABASE ... TEMPLATE odoo_ref_default` for instant copy, or empty DB when `template=none`
3. **Mounts the filestore overlay** — fuse-overlayfs with the template as lower layer
4. **Detects UID/GID** — runs `id` in the Odoo image to set correct file ownership
5. **Installs dependencies** — auto-installs from `apt_packages.txt` and `requirements.txt` if present in the repo
6. **Configures Odoo** — uses repo's `odoo.conf` if available, otherwise the default template
7. **Starts the container** — with `--dev=xml` for hot-reloading XML/QWeb changes
8. **Initializes base** — when `template=none`, runs `odoo -i base --stop-after-init`

### Auto branch creation

If the requested branch doesn't exist on the remote, Oduflow automatically:

1. Clones the default branch (configured via `ODUFLOW_DEFAULT_BRANCH`)
2. Creates a new local branch with the requested name
3. Reports the branch was created from the default branch

### Private repository authentication

For private repos, configure credentials first:

```bash
oduflow call setup_repo_auth https://user:PAT@github.com/owner/private-repo.git
```

Credentials are stored in the git credential store. Subsequent `create_environment` calls can use the clean URL without credentials.

### Auto-dependency installation

Place these files in your repository root for automatic installation during environment creation:

**`requirements.txt`** — Python packages installed via pip:

```
phonenumbers==8.13.0
python-barcode==0.15.1
xlsxwriter>=3.0
```

**`apt_packages.txt`** — System packages installed via apt:

```
# Dependencies for wkhtmltopdf
libfontconfig1
libxrender1
xfonts-75dpi
```

## Lifecycle Management

```bash
# List all environments with status, URL, image, and repo info
oduflow call list_environments

# Check detailed environment info (DB, URL, repo, image, CPU/RAM stats)
oduflow call get_environment_info feature-login

# Stop an environment (preserves data)
oduflow call stop_environment feature-login

# Start a stopped environment
oduflow call start_environment feature-login

# Restart the Odoo container
oduflow call restart_environment feature-login

# Rebuild the container from scratch (keeps database and filestore)
oduflow call rebuild_environment feature-login

# Tear down everything (container, database, filestore, workspace)
oduflow call delete_environment feature-login
```

## Viewing Logs

```bash
# Last 100 lines (default)
oduflow call get_environment_logs feature-login

# Last 500 lines
oduflow call get_environment_logs feature-login 500
```

## Installing and Upgrading Modules

```bash
# Install modules (odoo -i)
oduflow call install_odoo_modules feature-login sale,crm,website

# Upgrade modules (odoo -u)
oduflow call upgrade_odoo_modules feature-login sale,crm
```

## Running Tests

```bash
oduflow call test_environment feature-login sale,crm
```

This runs `odoo --test-enable --stop-after-init -i <modules>` inside the container.

## Smart Pull — Intelligent Change Detection

The `sync_environment` tool is one of Oduflow's most powerful features. It pulls the latest changes from the remote repository and **automatically determines the minimal action required**:

```bash
oduflow call sync_environment feature-login
```

### How it works

After `git pull --rebase`, Oduflow compares `HEAD` before and after, then classifies every changed file:

| Changed File | Analysis | Action |
|---|---|---|
| `__manifest__.py` (new module) | No previous manifest exists | **Install** the module |
| `__manifest__.py` (version changed) | `version` key differs | **Upgrade** the module |
| `__manifest__.py` (data/assets/demo/qweb changed) | File lists in manifest changed | **Upgrade** the module |
| `*.py` with `fields.*` changes | Field definitions added/removed/modified | **Upgrade** the module |
| `*.py` (no field changes) | Business logic change | **Restart** the container |
| `security/*.xml` | Access control or record rules | **Upgrade** the module |
| `*.xml` (not in security/) | Views, actions, data | **Refresh** (hot-reloaded via `--dev=xml`) |
| `*.js` | Frontend assets | **Refresh** (hot-reloaded via `--dev=xml`) |

### Action priority

`install` > `upgrade` > `restart` > `refresh`

If any module needs installation, all pending upgrades are also executed. If only Python files changed (without field modifications), a container restart is sufficient. If only XML/JS changed, no server-side action is needed — just refresh the browser.

!!! note
    `sync_environment` updates only the **main project repository**. Extra addons repositories are pinned to the commit they were deployed with and are not affected. See [Extra Addons — Updating](extra-addons.md#updating-extra-repos) for details.

### Module detection

Oduflow walks up from each changed file to find the nearest `__manifest__.py`, correctly identifying which Odoo module a file belongs to, even in nested directory structures.

## Executing Commands Inside Environments

Run arbitrary shell commands inside the Odoo container:

```bash
# List addon files
oduflow call exec_in_environment feature-login "ls /mnt/extra-addons"

# Check Python version
oduflow call exec_in_environment feature-login "python3 --version"

# Run a Python script
oduflow call exec_in_environment feature-login "python3 -c 'import odoo; print(odoo.release.version)'"

# Install a package as root
oduflow call exec_in_environment feature-login "pip3 install phonenumbers" root

# Debug database
oduflow call exec_in_environment feature-login "psql -h oduflow-db -U odoo -d oduflow_feature-login -c 'SELECT count(*) FROM res_partner;'"
```

The `user` parameter defaults to `odoo`. Use `root` for privileged operations (installing packages, modifying system files).

## Environment Protection

Environments can be **protected** from accidental deletion. A protected environment cannot be deleted until protection is removed.

Protection state is stored as a `.protected` marker file in the environment's workspace directory, so it survives container rebuilds and restarts.

When an environment is protected:

- **Delete** is blocked with a `ProtectedError`
- **Stop** is also blocked with a `ProtectedError`
- Other operations (restart, sync, install/upgrade modules) are unaffected

### Via REST API

```bash
# Protect an environment
curl -X POST http://localhost:8000/api/environments/feature-login/protect

# Unprotect an environment
curl -X POST http://localhost:8000/api/environments/feature-login/unprotect
```

### Via Web Dashboard

Click the **🔓 Protect** button on any environment card to toggle protection. When protected:

- The button shows **🔒 Protected** (highlighted)
- The **Delete** button is disabled
- Attempting to delete via MCP or API returns a `ProtectedError`
