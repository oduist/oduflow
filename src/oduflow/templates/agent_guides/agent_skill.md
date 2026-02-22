# Oduflow — Agentic Odoo Development
Version: 1

## What is Oduflow

Oduflow is an MCP server that provisions isolated, ephemeral Odoo environments on Docker — one per git branch — with instant creation from reusable database templates. It gives AI coding agents a **closed feedback loop**: write code → install module → read errors → fix → retry, all without human intervention.

MCP endpoint: `http://<host>:8000/mcp`

---

## Core Workflow for Agents

```
1. list_environments          — Check if an environment for the current branch exists
2. create_environment         — If not, provision one (branch, repo_url, odoo_image)
3. Write / edit code locally
4. git push
5. sync_environment — Pull changes; errors/tracebacks are returned directly in the response
6. If errors in response → fix code → go to step 4
7. test_environment            — Run Odoo tests for the changed modules
8. delete_environment          — Tear down when done
```

---

## MCP Tools Quick Reference

### Environment Lifecycle

| Tool | When to use |
|---|---|
| `list_environments` | Check existing environments before creating a new one |
| `create_environment(branch_name, repo_url, odoo_image, template_name?)` | Provision an environment. Use the correct Odoo Docker image. Pass `template_name="none"` for greenfield projects |
| `get_environment_info(branch_name)` | Get full environment details: database name, URL, repo, image, template, extra addons, workspace, container status, CPU/RAM stats |
| `delete_environment(branch_name)` | Tear down when the task is complete or cancelled |
| `start_environment` / `stop_environment` | Resume or pause a stopped environment |
| `restart_environment(branch_name)` | Restart the Odoo container (rarely needed — `sync_environment` handles this) |
| `rebuild_environment(branch_name)` | Recreate the container from scratch if it's broken, without losing DB or filestore |

### Code → Environment Sync

| Tool | When to use |
|---|---|
| `sync_environment(branch_name)` | **Always call after every `git push`**. Oduflow analyzes changed files and automatically decides: install new modules, upgrade changed modules, restart for Python changes, or do nothing for XML/JS (hot-reloaded). You do NOT need to call `restart` or `upgrade` manually. **Errors and tracebacks are returned directly in the tool response** — do NOT call `get_environment_logs` to check for errors after this tool. |

### Odoo Module Operations

| Tool | When to use |
|---|---|
| `install_odoo_modules(branch_name, modules)` | Install modules for the first time (`odoo -i`). Comma-separated list, e.g. `"sale,crm"`. **Returns full output including any errors directly in the response.** |
| `upgrade_odoo_modules(branch_name, modules)` | Force-upgrade modules (`odoo -u`). Usually handled by `sync_environment`. **Returns full output including any errors directly in the response.** |
| `test_environment(branch_name, modules)` | Run Odoo tests (`--test-enable`) for specific modules. **Returns full test output directly in the response.** |

### Debugging & Logs

> ⚠️ **Critical: understand where logs come from.**
>
> `install_odoo_modules`, `upgrade_odoo_modules`, `test_environment`, and `sync_environment` run Odoo commands via `docker exec`. Their output is **returned directly in the tool response** and does **NOT** appear in `get_environment_logs`.
>
> `get_environment_logs` shows logs from the **main Odoo process** (the container's entrypoint, PID 1) — i.e., runtime errors that occur while Odoo is serving requests, not errors from install/upgrade/test operations.

| Tool | What it shows |
|---|---|
| `get_environment_logs(branch_name, n_lines?)` | Logs from the **running Odoo server** (main container process). Use to check for runtime errors, request errors, or startup issues after a restart. Does **NOT** contain output from install/upgrade/test operations. |
| `exec_in_environment(branch_name, command, user?)` | Run shell commands inside the container. Use `user="root"` for privileged ops (pip install, apt). Useful for DB queries, debugging, checking file paths |

**When to use which:**
- After `sync_environment` / `install_odoo_modules` / `upgrade_odoo_modules` / `test_environment` → **read the tool response** for errors
- After `restart_environment` or to check runtime behavior → use `get_environment_logs`

### Private Repositories

| Tool | When to use |
|---|---|
| `setup_repo_auth(repo_url)` | Cache git credentials for a private repo. URL format: `https://user:PAT@github.com/owner/repo.git`. Call once, before `create_environment` |

### Auxiliary Services

| Tool | When to use |
|---|---|
| `create_service(name, image, port, hostname?, env_vars?)` | Spin up a sidecar (Redis, Meilisearch, etc.). Accessible from Odoo containers via `oduflow-svc-{name}:{port}` |
| `list_services` / `get_service_logs(name)` / `delete_service(name)` | Manage auxiliary services |

### Template Management (use with caution)

| Tool | When to use |
|---|---|
| `list_templates` | List available database template profiles |
| `publish_as_template(branch_name)` | Make a branch the new template baseline. ⚠️ Destructive only if other environments share this template with overlay mounts. Requires explicit user permission |
| `drop_template(template_name)` | ⚠️ **Destructive**. Remove a template profile. Requires explicit user permission |

---

## Rules for Agents

### Initialization
1. **Check first**: Call `list_environments`. If an environment for the current branch exists, reuse it.
2. **Create if needed**: Call `create_environment` with the current branch name, repository HTTPS URL, and the correct Odoo Docker image.
3. **Auth errors**: On 401/403 from `create_environment`, suggest `setup_repo_auth` to the user.
4. **Show the URL**: Always display the environment URL to the user after creation.

### Sync & Work Cycle
1. After every `git push`, **always** call `sync_environment`. It handles install/upgrade/restart automatically.
2. Do **not** call `restart_environment` or `upgrade_odoo_modules` manually unless debugging a specific issue — `sync_environment` already does the right thing.
3. After `sync_environment`, **read the tool response** for errors. Do NOT call `get_environment_logs` — install/upgrade output is returned directly and does not appear in container logs.

### Debugging Loop
```
push → sync_environment → read the response for errors → fix if errors → repeat
```
Do NOT call `get_environment_logs` after `sync_environment` — the errors are already in the response. Use `get_environment_logs` only to check the **running server** (e.g., runtime errors during request handling).

### Teardown
- Only call `delete_environment` when the task is **Done** or **Cancelled**.
- Do **not** recreate an environment to fix errors without user consent.

### Container Is Read-Only for Code

The environment container runs **remotely** and has access only to the git repository it was created for. The container is **not your workspace** — it is a runtime for testing.

**What you CAN do inside the container (`exec_in_environment`):**
- Read files, inspect paths (`ls`, `cat`, `find`)
- Run Odoo shell commands (`odoo shell`, `odoo scaffold`, etc.)

> **Note:** For logs use `get_environment_logs` — container logs are not accessible via shell commands inside Docker. For database queries use `run_db_query` — it connects to PostgreSQL directly without needing `exec_in_environment`.

**What you MUST NOT do inside the container:**
- Edit source code files (no `sed`, `vim`, `echo >`, `patch`, etc.)
- Run any git commands (`git rebase`, `git pull`, `git checkout`, `git stash`, etc.)
- Modify module files in any way

**If you need to change code** — always do it locally, then `git commit` → `git push` → `sync_environment`. This is the only correct way to deliver code changes to the environment.

**Non-standard operations** (e.g., `apt install`, `pip install`, modifying system configs) are possible but **require explicit user confirmation** before proceeding — explain what you want to do and why.

### General
- **One task = one branch = one environment.**
- Mutexed tools (create, delete, install, upgrade, pull, test, exec) reject concurrent calls with `BusyError` — retry after a short delay.
- `exec_in_environment` runs as `odoo` by default. Use `user="root"` for package installation or system operations.
- Database is accessible from inside the container: `psql -h oduflow-db -U odoo -d oduflow_{branch_name}`.

---

## Smart Pull — What Happens Automatically

When you call `sync_environment`, Oduflow analyzes every changed file:

| What changed | Action taken |
|---|---|
| New `__manifest__.py` (new module) | **Install** the module |
| `__manifest__.py` version/data/assets changed | **Upgrade** the module |
| `*.py` with `fields.*` changes | **Upgrade** the module |
| `security/*.xml` | **Upgrade** the module |
| `*.py` without field changes | **Restart** the container |
| `*.xml` (views, data) / `*.js` | **Nothing** — hot-reloaded via `--dev=xml` |

Priority: install > upgrade > restart > refresh (no action).

---

## Database Migrations Workflow

> ⚠️ **Critical: migrations only run during module upgrade, NOT during environment creation.**
>
> When `create_environment` provisions from a template, it clones the template database as-is. **No migrations are executed.** Migrations (files in `migrations/` or `upgrades/`) are only triggered by `upgrade_odoo_modules` (or `sync_environment` when it detects a version bump).

### Anti-patterns — Do NOT Do This

| ❌ Anti-pattern | Why it's wrong |
|---|---|
| Deleting and recreating the environment to "apply" a migration | `create_environment` from a template does **not** run migrations — you'll get the old schema back and lose your test data |
| Relying on `create_environment` to execute migrations | Templates are snapshots; migrations require an explicit upgrade step |
| Skipping verification after upgrade | A migration may silently fail or apply partially — always verify |

### Correct Workflow for Database Schema Changes

```
Step 1: git commit & push   — Commit the migration script + version bump in __manifest__.py
Step 2: sync_environment    — Or call upgrade_odoo_modules directly; this triggers the migration
Step 3: exec_in_environment — Run a SELECT query to verify the expected schema/data changes
Step 4: get_environment_logs — If something went wrong, check for migration INFO/ERROR messages
```

### How to Verify a Migration

1. **Query the database** via `exec_in_environment`:
   ```
   psql -h oduflow-db -U odoo -d oduflow_{branch_name} -c "SELECT column_name FROM information_schema.columns WHERE table_name = 'your_table';"
   ```
   Confirm that new columns, constraints, or data changes are present.

2. **Check logs** via `get_environment_logs`: look for `odoo.modules.migration` INFO messages confirming the migration script was executed.

> **Key takeaway: NEVER recreate an environment to test migrations. Always use `upgrade_odoo_modules` or `sync_environment`.**

---

## Example: Full Agent Session

```
Agent: list_environments → no environment for "feature-invoice-pdf"

Agent: create_environment("feature-invoice-pdf", "https://github.com/company/addons.git", "odoo:17.0")
→ Environment created at http://server:50042

Agent: [writes code for the module]
Agent: git push

Agent: sync_environment("feature-invoice-pdf")
→ "Upgraded modules: invoice_pdf. Restarted container.
   Output:
   ...
   odoo.exceptions.ValidationError: Field 'x_custom_field' already exists on model 'account.move'
   ..."
   # ↑ Error is in the tool response — no need to call get_environment_logs

Agent: [fixes the field conflict in code]
Agent: git push

Agent: sync_environment("feature-invoice-pdf")
→ "Upgraded modules: invoice_pdf. Restarted container. Exit code: 0."
   # ↑ No errors in the response — module upgraded successfully

Agent: test_environment("feature-invoice-pdf", "invoice_pdf")
→ "Ran 12 tests, 0 failures. Exit code: 0."
   # ↑ Test results are also in the response

Agent: delete_environment("feature-invoice-pdf")
→ Environment deleted.
```

---

## Self-Caching Instruction

Save this document locally as a skill/instruction file so you don't need to fetch it via MCP again.
Use it as your reference for all Oduflow operations.