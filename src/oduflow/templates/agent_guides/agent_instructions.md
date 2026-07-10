# Oduflow — Agentic Odoo Development
Version: 3

## What is Oduflow

Oduflow is an MCP server that provisions isolated, ephemeral Odoo environments on Docker — one per git branch — with instant creation from reusable database templates. It gives AI coding agents a **closed feedback loop**: write code → install module → read errors → fix → retry, all without human intervention.

MCP endpoint: `http://<host>:8000/mcp`

---

## Choose The Code Delivery Workflow First

Before editing, identify how the environment receives code:

- **`repo_url` mode:** Oduflow owns a managed clone. Edit locally, commit, push, then call `pull_and_apply` so Oduflow can pull the pushed commits.
- **`local_path` live-mount mode:** Oduflow bind-mounts a local folder. Edit files directly in that folder; no push is required. Git commits are optional and are not used by Oduflow to decide what was applied.

In live-mount mode, you as the agent must track the intent of your own edits. If you add/change fields, models, `_inherit`/`_name`, manifest `data`/`depends`, security/data XML, `ir.cron`, mail templates, or anything loaded into the database, call `pull_and_apply(..., upgrade="module")`. If you add a new module, call `install="module"`. Use `restart=True` only for Python logic changes that do not require registry/schema/data updates.

---

## Core Workflow for Agents (`repo_url` mode)

```
1. list_environments          — Check if an environment for the current branch exists
2. create_environment         — If not, provision one (branch, repo_url, odoo_image)
3. Write / edit code locally
4. git push
5. pull_and_apply — Pull changes; errors/tracebacks are returned directly in the response
6. If errors in response → fix code → go to step 4
7. run_odoo_tests            — Run Odoo tests for the changed modules
8. delete_environment          — Tear down when done
```

---

## Core Workflow for Agents (`local_path` live-mount mode)

```
1. list_environments          — Check if an environment already uses local_path
2. create_environment         — If not, provision one with local_path="/abs/path"
3. Write / edit files directly in the mounted local folder
4. pull_and_apply             — Pass install/upgrade/restart explicitly when your edits require it
5. If errors in response → fix code → repeat from step 4
6. run_odoo_tests             — Run Odoo tests for the changed modules
7. delete_environment         — Tear down when done
```

---

## MCP Tools Quick Reference

### Environment Lifecycle

| Tool | When to use |
|---|---|
| `list_environments` | Check existing environments before creating a new one |
| `create_environment(branch, env_name?, repo_url, odoo_image, template_name?, env_vars?, local_path?)` | Provision an environment. `branch` is the git branch; `env_name` defaults to the branch name. Use the correct Odoo Docker image. Pass `template_name="none"` for greenfield projects. `env_vars` is a comma-separated `KEY=VALUE` list injected into the Odoo container. **Local fast-path / live-mount:** pass `local_path="/abs/path/to/checkout"` to bind-mount local files live instead of cloning — edits apply with no git push (see "Local Fast-Path / Live-Mount" below). |
| `get_environment_info(env_name)` | Get full environment details: database name, URL, repo, image, template, extra addons, workspace, container status, CPU/RAM stats |
| `delete_environment(env_name)` | Tear down when the task is complete or cancelled |
| `start_environment` / `stop_environment` | Resume or pause a stopped environment |
| *(automatic)* | Environments **auto-stop** after 48h without tool calls and **auto-delete** 72h after stopping (configurable). Container-level tools (`pull_and_apply`, shell, tests, installs, file ops) wake a stopped environment automatically — a `Note:` line in the response tells you when that happened. `protect_environment` exempts an environment from both |
| `restart_environment(env_name)` | Restart the Odoo container (rarely needed — `pull_and_apply` handles this) |
| `update_environment(env_name, env_vars, odoo_image)` | Recreate the container without losing DB or filestore — to fix a broken container, switch image, or change env vars |

### Code → Environment Sync

| Tool | When to use |
|---|---|
| `pull_and_apply(env_name, install?, upgrade?, restart?, strict?)` | Sync code and apply the right action. **git mode:** call after every `git push`. **live-mount mode:** call after editing files (no push needed). **Auto** (no args): Oduflow analyzes the diff and decides install/upgrade/restart/refresh — best when pulling commits you did not author. **Explicit** (recommended when you authored the edits): pass `install`/`upgrade` (comma-separated modules) and/or `restart=True` per the decision rules below; a guardrail warns if your action looks incomplete (`strict=True` refuses instead). **Errors and tracebacks are returned directly in the tool response** — do NOT call `get_environment_logs` after this tool. |

### Odoo Module Operations

| Tool | When to use |
|---|---|
| `install_odoo_modules(env_name, modules)` | Install modules for the first time (`odoo -i`). Comma-separated list, e.g. `"sale,crm"`. **Returns full output including any errors directly in the response.** |
| `upgrade_odoo_modules(env_name, modules)` | Force-upgrade modules (`odoo -u`). Usually handled by `pull_and_apply`. **Returns full output including any errors directly in the response.** |
| `run_odoo_tests(env_name, modules)` | Run Odoo tests (`--test-enable`) for specific modules. **Returns full test output directly in the response.** |
| `list_installed_modules(env_name, name_filter?, state_filter?)` | List Odoo modules with name/state filtering. Default: installed modules only. |

### ORM & Scripting

| Tool | When to use |
|---|---|
| `run_odoo_shell(env_name, python_code)` | Execute Python in Odoo shell with full ORM access (`self.env`, models, registry). Use `print()` to produce output. |
| `write_file_in_odoo(env_name, path, content, user?)` | Write a text file inside the container (CSV imports, scripts, configs). Uses tar stream — no shell escaping issues. Do NOT use for source code. |
| `http_request_to_odoo(env_name, path, method?, body?, headers?, session_id?)` | HTTP request to the running Odoo instance. Test controllers, JSON-RPC, REST endpoints. |
| `search_in_odoo(env_name, pattern, path?, glob?, max_results?)` | Grep for a pattern inside container files. Fixed-string search with file/line numbers. |
| `reset_admin_password(env_name, new_password?)` | Reset the admin user's password in the environment's Odoo database (default: `"test"`). Useful to log into a template-based env whose admin password is unknown |

### Debugging & Logs

> ⚠️ **Critical: understand where logs come from.**
>
> `install_odoo_modules`, `upgrade_odoo_modules`, `run_odoo_tests`, and `pull_and_apply` run Odoo commands via `docker exec`. Their output is **returned directly in the tool response** and does **NOT** appear in `get_environment_logs`.
>
> `get_environment_logs` shows logs from the **main Odoo process** (the container's entrypoint, PID 1) — i.e., runtime errors that occur while Odoo is serving requests, not errors from install/upgrade/test operations.

| Tool | What it shows |
|---|---|
| `get_environment_logs(env_name, n_lines?, grep?, level?)` | Logs from the **running Odoo server** (main container process). Use `grep` for substring filtering, `level` for "ERROR"/"WARNING"/"CRITICAL". Does **NOT** contain output from install/upgrade/test operations. |
| `read_file_in_odoo(env_name, path, read_range?)` | Read a text file or list a directory inside the container. Use to inspect Odoo source code, addon structure, config files. Supports `read_range="START:END"` for large files. **Prefer this over `run_odoo_command` with `cat`/`ls`.** |
| `run_odoo_command(env_name, command, user?)` | Run shell commands inside the container. Use `user="root"` for privileged ops (pip install, apt). Useful for debugging, running Odoo shell commands. For reading files, prefer `read_file_in_odoo` instead |

**When to use which:**
- After `pull_and_apply` / `install_odoo_modules` / `upgrade_odoo_modules` / `run_odoo_tests` → **read the tool response** for errors
- After `restart_environment` or to check runtime behavior → use `get_environment_logs`

### Private Repositories

| Tool | When to use |
|---|---|
| `setup_repo_auth(repo_url)` | Cache git credentials for a private repo. URL format: `https://user:PAT@github.com/owner/repo.git`. Call once, before `create_environment` |

### Extra Addons (Enterprise & third-party repos)

Extra addons repositories (Odoo Enterprise, OCA, your own shared addons) are cloned once on the server and then become available to environments. For private repos, call `setup_repo_auth` first.

| Tool | When to use |
|---|---|
| `add_extra_repo(name, repo_url)` | Clone an extra addons repository so its modules become available when creating or updating environments |
| `list_extra_repos` | List all cloned extra addons repositories |
| `update_extra_repo(name)` | Fetch the latest changes from the remote for an extra addons repository |
| `delete_extra_repo(name)` | Remove a cloned extra addons repository |

### Auxiliary Services

| Tool | When to use |
|---|---|
| `create_service(name, image, port, hostname?, env_vars?, host_mode?, volumes?, privileged?, net_admin?)` | Spin up a sidecar (Redis, Meilisearch, etc.). Accessible from Odoo containers via `oduflow-svc-{name}:{port}`. Set `net_admin=true` for VPN/tun/iptables; `privileged=true` for full host access. The image is always pulled fresh, so mutable tags like `:latest` get the current version |
| `get_service_info(name)` | **Use this before recreating a service.** Returns full live state: image + digest, port, hostname, URL, `host_mode`, `volumes`, env vars, `cap_add`, `privileged`, restart count, started_at, whether a preset exists |
| `update_service(name, env_vars?, image?, port?, hostname?, host_mode?, volumes?, privileged?, net_admin?)` | Change **any** setting on a running service. Without overrides, pulls the latest image. With any override, fully replaces that setting and recreates the container, preserving everything else. `env_vars` and `volumes` are full replacements, not merges. This is the preferred way to change a service |
| `list_services` / `get_service_logs(name)` / `restart_service(name)` / `delete_service(name)` | Manage auxiliary services |
| `restore_service(name)` | Recreate a service from its saved preset after deletion (volumes, host_mode, cap_add, env are all preserved) |
| `list_service_presets` | List saved service presets (configurations that can be restored after a service is deleted) |
| `delete_service_preset(name)` | Remove a saved service preset |
| `run_service_command(name, command, user?)` | Execute a shell command inside a service container. Default user is `root`. Output is cached if large — use `read_output` for drill-down |

> **Changing a service:** use `update_service` for **any** change — image, env vars, port, hostname, host_mode, volumes, privileged, or net_admin. It recreates the container automatically and preserves every setting you don't override, so you almost never need to `delete_service` + `create_service` by hand. (If you do recreate manually — e.g. to rename a service — call `get_service_info(name)` first and replay its `image`, `port`, `hostname`, `env_vars`, `host_mode`, `volumes`, `cap_add`, `privileged` in the new `create_service` call, otherwise you'll silently drop them.)

### Volumes

| Tool | When to use |
|---|---|
| `create_volume(name, description?)` | Create a named Docker volume for use with services |
| `list_volumes` | List all managed volumes and which services use them |
| `inspect_volume(name)` | Get detailed info about a volume (Docker name, mountpoint, usage) |
| `delete_volume(name)` | Delete a volume (fails if mounted by a service) |
| `read_file_in_volume(name, path, read_range?)` | Read a text file or list a directory inside a volume. Spins up a temporary container |
| `write_file_in_volume(name, path, content)` | Write a text file inside a volume |
| `search_in_volume(name, pattern, path?, glob?, max_results?)` | Grep for a pattern in files inside a volume. Fixed-string search with file/line numbers |
| `delete_file_in_volume(name, path)` | Delete a file or directory inside a volume |

### Template Management (use with caution)

| Tool | When to use |
|---|---|
| `list_templates` | List available database template profiles |
| `import_template_from_odoo(odoo_url, master_pwd, db_name?, template_name?, without_filestore=False)` | Import a template from a running Odoo instance via its database manager API. Set `without_filestore=True` for a database-only PostgreSQL custom dump. Requires explicit user permission |
| `save_as_template(env_name, reset_env_changes=False)` | Make a branch the new template baseline. Other overlay environments on this template are remounted against the new baseline, **keeping their filestore changes by default** (non-destructive); `reset_env_changes=True` discards them. The source env is always reset. Requires explicit user permission |
| `refresh_template(template_name, reset_env_changes=False)` | Re-apply a template's current filestore to live overlay environments, keeping their changes (non-destructive); `reset_env_changes=True` resets them to the template baseline. Use after the template filestore changed on disk or to re-sync a skipped env. Requires explicit user permission |
| `attach_filestore(template_name, source, reset_env_changes=False, strip_prefix="auto")` | Attach or replace a template filestore from a local directory, archive, `rsync://` URL, or SSH rsync source such as `user@host:/path`. It normalizes paths to `XX/<sha1>` and preserves live env changes by default. Requires explicit user permission |
| `delete_template(template_name)` | ⚠️ **Destructive**. Remove a template profile. Requires explicit user permission |

### Reference & Guides

| Tool | When to use |
|---|---|
| `get_odoo_development_guide(version)` | Fetch Odoo development standards and constraints for a specific Odoo version (15–19). Read it before writing or refactoring module code |
| `get_agent_instructions` | Re-fetch this instruction document (the one you are reading) — e.g. after a context reset |

---

## Working with Large Outputs

Tools like `install_odoo_modules`, `upgrade_odoo_modules`, `run_odoo_tests`, `pull_and_apply`, `run_odoo_command`, `run_service_command`, and `run_db_query` can produce very large output (tens of thousands of lines). When output exceeds ~5K characters, Oduflow automatically:

1. **Caches** the full output on the server
2. **Returns a smart summary**: first 200 lines + all errors with context + last 100 lines + metadata
3. **Includes an `output_id`** for drill-down

The summary footer looks like:
```
[Cached output: id=a3f7c012, 1897 lines, 68449 chars]
[Use read_output(output_id="a3f7c012", ...) to search, read ranges, or get full output]
```

### Drill-down with `read_output`

| Mode | What it does |
|---|---|
| `read_output(output_id, mode="errors")` | Show only ERROR/WARNING lines with context |
| `read_output(output_id, mode="grep", grep="pattern")` | Search for a substring (case-insensitive) |
| `read_output(output_id, mode="lines", start=100, end=200)` | Read a specific line range |
| `read_output(output_id, mode="tail")` | Last 100 lines |
| `read_output(output_id, mode="info")` | Metadata: line count, char count, error count |

**When to use:** If the summary shows an error but you need more context around it, or if you need to find a specific module/field/class in the output.

**Cache lifetime:** 1 hour. After that, the output expires.

---

## Rules for Agents

### Initialization
1. **Check first**: Call `list_environments`. If an environment for the current branch exists, reuse it.
2. **Create if needed**: Call `create_environment` with the current branch name, repository HTTPS URL, and the correct Odoo Docker image.
3. **Auth errors**: On 401/403 from `create_environment`, suggest `setup_repo_auth` to the user.
4. **Show the URL**: Always display the environment URL to the user after creation.

### Environment Lifecycle (automatic)
1. Idle environments **auto-stop** after 48 hours without any env-scoped tool call; stopped environments **auto-delete** after another 72 hours (both configurable, both skip protected environments).
2. You do not need to start a stopped environment yourself: container-level tools (`pull_and_apply`, `run_odoo_shell`, `run_odoo_tests`, installs/upgrades, file and command tools) start it automatically and prepend `Note: environment was stopped; started it ...` to the response. Treat that note as normal, not as an error.
3. If an environment must survive idle periods (e.g. handed to a customer for testing), call `protect_environment` — protection disables stop and delete entirely.

### Sync & Work Cycle
1. After every `git push` (git mode) — or after editing files directly (live-mount mode) — call `pull_and_apply`.
2. When **you** authored the edits, pass the action explicitly (`install` / `upgrade` / `restart`) per the decision rules below — you know what you changed, so don't make Oduflow guess. Leave them empty (auto mode) only when pulling commits you did not author.
3. Do **not** call `restart_environment` or `upgrade_odoo_modules` separately for a normal sync — `pull_and_apply` does it in one call.
4. After `pull_and_apply`, **read the tool response** for errors and any guardrail ⚠ warnings. Do NOT call `get_environment_logs` — install/upgrade output is returned directly and does not appear in container logs.

### Debugging Loop
```
push → pull_and_apply → read the response for errors → fix if errors → repeat
```
Do NOT call `get_environment_logs` after `pull_and_apply` — the errors are already in the response. Use `get_environment_logs` only to check the **running server** (e.g., runtime errors during request handling).

### Teardown
- Only call `delete_environment` when the task is **Done** or **Cancelled**.
- Do **not** recreate an environment to fix errors without user consent.

### Container Is Read-Only for Code

The environment container runs **remotely** and has access only to the git repository it was created for. The container is **not your workspace** — it is a runtime for testing.

**What you CAN do inside the container:**
- Read files and inspect paths — use `read_file_in_odoo` (preferred) or `run_odoo_command`
- Run Odoo shell commands (`odoo shell`, `odoo scaffold`, etc.) — use `run_odoo_command`

> **Note:** For logs use `get_environment_logs` — container logs are not accessible via shell commands inside Docker. For database queries use `run_db_query` — it connects to PostgreSQL directly without needing `run_odoo_command`.

**What you MUST NOT do inside the container:**
- Edit source code files (no `sed`, `vim`, `echo >`, `patch`, etc.)
- Run any git commands (`git rebase`, `git pull`, `git checkout`, `git stash`, etc.)
- Modify module files in any way

**If you need to change code** — in **git mode**, do it locally, then `git commit` → `git push` → `pull_and_apply`. In **live-mount mode** (env created with `local_path`), edit the files in that local directory directly and call `pull_and_apply` — no commit/push required (the directory is bind-mounted live into the container). Either way, never edit code *inside* the container.

**Non-standard operations** (e.g., `apt install`, `pip install`, modifying system configs) are possible but **require explicit user confirmation** before proceeding — explain what you want to do and why.

### Searching Odoo Source Code
- If Odoo Community or Enterprise source repositories are available **locally** (cloned to your machine), prefer using your native search tools (Grep, Glob, Read) over `search_in_odoo` — local search is faster and doesn't require a running environment.
- If local copies are not available, `search_in_odoo` works well for searching both Odoo core (`/usr/lib/python3/dist-packages/odoo/addons`) and extra addons inside the container.

### General
- **One task = one branch = one environment.**
- Mutexed tools (create, delete, install, upgrade, pull, test, exec) reject concurrent calls with `BusyError` — retry after a short delay.
- `run_odoo_command` runs as `odoo` by default. Use `user="root"` for package installation or system operations.
- Database is accessible from inside the container: `psql -h oduflow-db -U odoo -d oduflow_{env_name}`.

---

## Smart Pull — What Happens Automatically

When you call `pull_and_apply`, Oduflow analyzes every changed file:

| What changed | Action taken |
|---|---|
| New `__manifest__.py` (new module) | **Install** the module |
| `__manifest__.py` version/data/assets changed | **Upgrade** the module |
| `*.py` with `fields.*` changes | **Upgrade** the module |
| `security/*.xml` | **Upgrade** the module |
| `*.py` without field changes | **Restart** the container |
| `*.xml` (views) / `*.js` / `*.css` / `*.scss` | **Nothing** — hot-reloaded via `--dev=xml` (refresh browser) |

Priority: install > upgrade > restart > refresh (no action).

This auto-classification runs when you call `pull_and_apply` **without** `install`/`upgrade`/`restart`. In `repo_url` mode it can compare pulled commits with Git history, including field/manifest details. In `local_path` live-mount mode it is snapshot/path-based only, so it cannot reliably know whether a Python edit added a field or changed only method logic. Use auto mode for pulling commits you did not author. When you authored the edits, prefer the explicit form (next section).

---

## Apply Decision Rules (explicit mode)

When you changed the code yourself, you already know what to do — tell `pull_and_apply` directly. Pick by **where the changed thing lives**:

| You changed… | It lives in… | Pass |
|---|---|---|
| view/QWeb XML, JS, CSS, static assets | the browser / read from file | nothing — just refresh the browser (`--dev=xml` is active) |
| Python logic/methods only (no new/changed fields or models) | the worker process | `restart=True` |
| a **field** or model (`fields.*`, new model, `_inherit`/`_name`), security/data records, `ir.cron`, mail templates, `ir.model.access`, or manifest `data`/`depends` | the **database** (loaded only on install/upgrade) | `upgrade="module"` (-u) |
| a brand-new, not-yet-installed module | — | `install="module"` (-i) |

⚠️ **Sharp edge:** editing a `.py` to add/change a **field** needs `upgrade` (-u), not `restart` — even though it feels like a code change. A restart reloads code but won't touch the DB schema/registry.

### Guardrail
In explicit mode, Oduflow cross-checks your action against the detected diff and appends non-blocking ⚠ warnings if something looks missing (e.g. you restarted but a module's data/schema changed and needs `-u`). It still applies what you asked — read the warnings and re-call with the correction if it's right. Pass `strict=True` to make it refuse instead of warn.

---

## Local Fast-Path / Live-Mount

When Oduflow runs on the same machine as the code, skip the GitHub round-trip entirely:

1. `create_environment(branch=..., odoo_image=..., template_name=..., local_path="/abs/path/to/your/checkout")` — bind-mounts your working copy live into the container instead of cloning. `repo_url` is not needed.
2. Edit files in that directory with your normal tools. Changes are visible inside the container **instantly** (no push, no clone).
3. `pull_and_apply(env_name, upgrade="my_module")` (or `restart=True`, or `install=...`) to apply — per the decision rules above. XML view edits usually need no call at all: just refresh the browser.

Notes:
- `local_path` is controlled by `[server].allow_local_path` (default: `true`). Set it to `false` to disable live-mounts.
- Live-mount change detection is snapshot-based and independent of Git. Oduflow records which local file state was last successfully applied and compares later calls against that snapshot.
- In live-mount mode, you as the agent must track the intent of your own edits. If you add/change fields, models, `_inherit`/`_name`, manifest `data`/`depends`, security/data XML, `ir.cron`, mail templates, or anything loaded into the database, call `pull_and_apply(..., upgrade="module")`. If you add a new module, call `install="module"`. Use `restart=True` only for Python logic changes that do not require registry/schema/data updates.
- Git is optional in live-mount mode. Commit whenever you want for your own workflow; Oduflow does not require commits, create commits, or read Git state to apply local changes.
- With `repo_url` mode, use the normal remote workflow: edit locally, commit, push, then call `pull_and_apply` so the managed clone can pull the pushed commits.

---

## Database Migrations Workflow

> ⚠️ **Critical: migrations only run during module upgrade, NOT during environment creation.**
>
> When `create_environment` provisions from a template, it clones the template database as-is. **No migrations are executed.** Migrations (files in `migrations/` or `upgrades/`) are only triggered by `upgrade_odoo_modules` (or `pull_and_apply` when it detects a version bump).

### Anti-patterns — Do NOT Do This

| ❌ Anti-pattern | Why it's wrong |
|---|---|
| Deleting and recreating the environment to "apply" a migration | `create_environment` from a template does **not** run migrations — you'll get the old schema back and lose your test data |
| Relying on `create_environment` to execute migrations | Templates are snapshots; migrations require an explicit upgrade step |
| Skipping verification after upgrade | A migration may silently fail or apply partially — always verify |

### Correct Workflow for Database Schema Changes

```
Step 1: git commit & push   — Commit the migration script + version bump in __manifest__.py
Step 2: pull_and_apply    — Or call upgrade_odoo_modules directly; this triggers the migration
Step 3: run_odoo_command — Run a SELECT query to verify the expected schema/data changes
Step 4: get_environment_logs — If something went wrong, check for migration INFO/ERROR messages
```

### How to Verify a Migration

1. **Query the database** via `run_odoo_command`:
   ```
   psql -h oduflow-db -U odoo -d oduflow_{env_name} -c "SELECT column_name FROM information_schema.columns WHERE table_name = 'your_table';"
   ```
   Confirm that new columns, constraints, or data changes are present.

2. **Check logs** via `get_environment_logs`: look for `odoo.modules.migration` INFO messages confirming the migration script was executed.

> **Key takeaway: NEVER recreate an environment to test migrations. Always use `upgrade_odoo_modules` or `pull_and_apply`.**

---

## Example: Full Agent Session

```
Agent: list_environments → no environment for "feature-invoice-pdf"

Agent: create_environment("feature-invoice-pdf", "https://github.com/company/addons.git", "odoo:19.0")
→ Environment created at http://server:50042

Agent: [writes code for the module]
Agent: git push

Agent: pull_and_apply("feature-invoice-pdf")
→ "Upgraded modules: invoice_pdf. Restarted container.
   Output:
   ...
   odoo.exceptions.ValidationError: Field 'x_custom_field' already exists on model 'account.move'
   ..."
   # ↑ Error is in the tool response — no need to call get_environment_logs

Agent: [fixes the field conflict in code]
Agent: git push

Agent: pull_and_apply("feature-invoice-pdf")
→ "Upgraded modules: invoice_pdf. Restarted container. Exit code: 0."
   # ↑ No errors in the response — module upgraded successfully

Agent: run_odoo_tests("feature-invoice-pdf", "invoice_pdf")
→ "Ran 12 tests, 0 failures. Exit code: 0."
   # ↑ Test results are also in the response

Agent: delete_environment("feature-invoice-pdf")
→ Environment deleted.
```

---

## Self-Caching Instruction

Save this document locally as a skill/instruction file so you don't need to fetch it via MCP again.
Use it as your reference for all Oduflow operations.
