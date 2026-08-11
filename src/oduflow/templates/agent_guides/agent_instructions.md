# Oduflow — Agentic Odoo Development
Version: 4

## What is Oduflow

Oduflow is an MCP server that provisions isolated, ephemeral Odoo environments on Docker — one per git branch — with instant creation from reusable database templates. It gives AI coding agents a **closed feedback loop**: write code → install module → read errors → fix → retry, all without human intervention.

MCP endpoint: `http://<host>:8000/mcp`

---

## Choose The Code Delivery Workflow First

Before editing, identify how the environment receives code:

- **`repo_url` mode:** Oduflow owns a managed clone. Edit locally, commit, push, then call `pull_and_apply` so Oduflow can pull the pushed commits.
- **`local_path` live-mount mode:** Oduflow bind-mounts a local folder. Edit files directly in that folder; no push is required. Git commits are optional and are not used by Oduflow to decide what was applied.

In live-mount mode, you as the agent must track the intent of your own edits. If you add/change fields, models, `_inherit`/`_name`, manifest `data`/`depends`, security/data XML, `ir.cron`, mail templates, `i18n/*.po` translations, or anything loaded into the database, call `pull_and_apply(..., upgrade="module")`. If you add a new module, call `install="module"`. Use `restart=True` only for Python logic changes that do not require registry/schema/data updates.

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
8. delete_environment          — Tear down when done; environments are disposable and cheap to recreate
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
7. delete_environment         — Tear down when done; environments are disposable and cheap to recreate
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
| `run_odoo_tests(env_name, modules, test_tags?, upgrade?)` | Run Odoo tests (`--test-enable`) for specific modules. **Returns full test output directly in the response.** Narrow a run with `test_tags="/my_module:TestClass.test_method"` instead of re-running a whole module. `upgrade=False` skips the `-u` for a fast re-run — but Odoo then collects **only `post_install` tests**, so a plain `TransactionCase` (at_install by default) reports "0 tests"; re-run with the default `upgrade=True` if a class you expect does not appear. With `upgrade=False`, positive tags must name a requested module (`slow/my_module`); exclusion-only tags such as `-slow` are scoped automatically. |
| `list_installed_modules(env_name, name_filter?, state_filter?)` | List Odoo modules with name/state filtering. Default: installed modules only. |

### Translations (i18n)

| Tool | When to use |
|---|---|
| `export_module_translations(env_name, module, lang?)` | Export the module's catalogue with Odoo's own exporter. No `lang` → the `.pot` template (all translatable terms, including `_()` messages from Python). With `lang` → a `.po` filled from the database. Writes into the module's `i18n/` and returns a **summary plus a one-time download URL** over HTTP or a temporary host path under stdio, never the file body. |
| `translation_status(env_name, module, langs?)` | Compare the module's terms, the database, and the committed `i18n/*.po`. **Run this after loading translations** — Odoo is silent about both ways a `.po` fails. |

> **Odoo does not warn you when translations fail to load.** A `.po` entry with
> no `#:` reference line is read and discarded — a whole valid file can import as
> zero translations with nothing in the log. An entry with no `#. module:`
> comment aborts the import. Odoo first merges a sibling `<module>.pot`, when
> present, and that template can supply both kinds of metadata;
> `translation_status` models that effective import instead of raising a false
> warning. Do not assume a `.po` worked because the upgrade succeeded.

> Write English source strings (`string=`, `help=`, `_()`), never a national
> language: Odoo's whole translation mechanism assumes an English `msgid`.

### ORM & Scripting

The `odoo_*` tools are the XML-RPC `execute_kw` surface: structured, fast, and
they run as a real user so access rights and record rules apply. Reach for them
first when reading or changing data. `run_odoo_shell` stays the escape hatch.

| Need | Tool |
|---|---|
| Read or change data, as a specific user, with real ACLs | `odoo_*` |
| Multi-step logic in one transaction, a dry run, `sudo()`, private methods, registry internals | `run_odoo_shell` |
| See freshly edited Python **without** restarting the environment | `run_odoo_shell` |
| Raw SQL, `EXPLAIN`, schema-level checks | `run_db_query` |

> ⚠️ The `odoo_*` tools talk to the **live** Odoo HTTP server, exactly like an
> external RPC client. Edited **Python** code is invisible to them until the
> environment restarts (`pull_and_apply` / `restart_environment`); XML views do
> reload. Each call is its own committed transaction — there is no dry run and
> no atomicity across calls.

| Tool | When to use |
|---|---|
| `odoo_search_read(env_name, model, domain?, fields?, limit?, offset?, order?, count_only?, as_user?, context?)` | Search and read records. `domain` is JSON (a Python literal also works); a bare leaf is wrapped for you. Always pass `fields`. `count_only=true` runs `search_count`. |
| `odoo_create(env_name, model, values, as_user?, context?)` | Create one record (JSON object) or many (JSON array of objects). Returns the new ids. |
| `odoo_write(env_name, model, ids, values, as_user?, context?)` | Update records. `ids` accepts `"42"`, `"1,2,3"` or `"[1,2,3]"`. |
| `odoo_unlink(env_name, model, ids, as_user?, context?)` | Delete records. Not recoverable — confirm with `odoo_search_read` first, and prefer archiving (`active=false` via `odoo_write`). |
| `odoo_call(env_name, model, method, ids?, args?, kwargs?, as_user?, context?)` | Public methods not covered by the dedicated CRUD tools: `read_group`, `name_search`, `default_get`, `action_confirm`, custom addon methods. `create`, `write`, and `unlink` must use their named tools. `ids` is prepended as the first positional argument. Private (`_`-prefixed) methods are rejected — use `run_odoo_shell`. |
| `odoo_schema(env_name, model?, name_filter?, attributes?, as_user?, limit?, offset?)` | Paged model discovery and `fields_get`. Call it before writing a domain or a values dict — guessing field names is the top cause of empty results. |
| `run_odoo_shell(env_name, python_code, auto_commit?)` | Execute Python in Odoo shell with full ORM access (`self.env`, models, registry). Use `print()` to produce output. Commits on success by default; pass `auto_commit=false` for a read-only dry run (changes rolled back). |
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
| `run_odoo_command(env_name, command, user?, shell?)` | Run shell commands inside the container. Runs through `sh -c`, so pipes, redirections, `&&`, `cd x && y` and `$VAR` behave as written. Pass `shell=False` for exact argv semantics (metacharacters stay literal). Use `user="root"` for privileged ops (pip install, apt). For reading files, prefer `read_file_in_odoo` instead |

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
| `create_service(name, image, *, port=<catch-all> \| routes=[...], ...)` | Spin up a sidecar using exactly one exposure mode. `port=8080` publishes every path to one port and is required outside Traefik. Alternatively, Traefik `routes=[{"path":"/api","port":8080,"strip_prefix":false}]` publishes only those paths. Never pass `port` and `routes` together |
| `get_service_info(name)` | **Use this before recreating a service.** Returns full live state, including whichever exposure model is active: `port` or `routes` |
| `update_service(name, ..., port? \| routes?)` | Change any setting. `routes` is a full replacement; pass `routes=[]` together with `port` to switch back to catch-all mode. Otherwise never pass `port` and `routes` together. Unset settings are preserved |
| `list_services` / `get_service_logs(name)` / `restart_service(name)` / `delete_service(name)` | Manage auxiliary services |
| `restore_service(name)` | Recreate a service from its saved preset after deletion (volumes, host_mode, cap_add, env are all preserved) |
| `list_service_presets` | List saved service presets (configurations that can be restored after a service is deleted) |
| `delete_service_preset(name)` | Remove a saved service preset |
| `run_service_command(name, command, user?, shell?)` | Execute a shell command inside a service container. Default user is `root`. Runs through `sh -c` like `run_odoo_command`; `shell=False` for exact argv (or for an image that ships no shell). Output is cached if large — use `read_output` for drill-down |

> **Connecting to a service from Odoo:** use the container name reported as `Container:` / `Internal hostname:` by `create_service` and `get_service_info` — that is the exact DNS name (`oduflow-{team}-svc-{name}`) resolvable from Odoo and every other container on the team network. There is no shorter alias, and the `URL:` line is the *external* Traefik/host address, not the internal one. Host-mode services are not on the team network — reach them via `host.docker.internal`.

> **Changing a service:** use `update_service` for any change — image, env vars, port/routes, hostname, host mode, volumes or capabilities. If recreating manually, call `get_service_info` first and replay its complete user-controlled configuration. Omit implicit system mounts.

> **Traefik TLS certificate store:** every service receives the exact system volume at `/etc/traefik` read-only. It is implicit and not part of the preset or the user-supplied `volumes` replacement. `/etc/traefik` is reserved, and all other raw `oduflow-*` volumes remain forbidden. `update_service` validates candidate volumes before stopping the current container.

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
| `list_templates` | List available database template profiles, including `Source=<branch> @ <commit> @ snapshot <date>` — the code each database snapshot was taken from (see "Template ↔ Code Lineage") |
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
| `report_issue(details, kind="feedback", title?)` | Build a prefilled `github.com/oduist/oduflow` issue link when the user hits a bug **in Oduflow itself**, wants a feature, or has feedback about the tool — not for bugs in their own Odoo code. It files nothing: show the returned link and let the user submit it from their own GitHub account. Never put hostnames, repo URLs, branch/database names, credentials, or customer data in the text |

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

**Cache lifetime:** configured by the server operator (1 hour by default).
After that, the output expires.

### Durable operations

Every mutating tool accepts `wait` (default `true`). Oduflow generates the
operation ID; do not generate one yourself.

- With `wait=true`, a quick operation returns its normal result in the same
  call. If it exceeds the server's safe wait window, the response becomes an
  operation ticket and the work continues.
- With `wait=false`, the tool immediately returns a ticket containing
  `operation_id`, `state`, and resource keys.
- Use `wait_operation(operation_id)` repeatedly to wait without running the
  mutation again. Use `get_operation`, `list_operations`,
  `read_operation_output`, and `cancel_operation` for status, result retrieval,
  live output when available, and best-effort cancellation.
- Never repeat the original mutating call merely because an MCP request timed
  out. First use `list_operations` to find its server-generated ticket.

---

## Rules for Agents

### Initialization
1. **Check first**: Call `list_environments`. If an environment for the current branch exists, reuse it.
2. **Create if needed**: Call `create_environment` with the current branch name, repository HTTPS URL, and the correct Odoo Docker image.
3. **Auth errors**: On 401/403 from `create_environment`, suggest `setup_repo_auth` to the user.
4. **Show the URL**: Always display the environment URL to the user after creation.

### Environment Lifecycle (automatic)
1. Idle environments **auto-stop** after 48 hours without any env-scoped tool call; stopped environments **auto-delete** after another 72 hours (both configurable, both skip protected environments).
2. Mutating container tools (`pull_and_apply`, `run_odoo_shell`,
   `run_odoo_tests`, installs/upgrades, file writes, and commands) start a
   stopped environment automatically. Read-only tools never wake it; they
   return the stopped state, after which call `start_environment` explicitly
   when the read is needed.
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

### Out of Database Connections

Symptoms — in `get_environment_logs` or in a tool response:
`psycopg2.pool.PoolError: The Connection Pool Is Full`, connections timing
out, or requests hanging during a heavy import or a parallel test run.

That means this environment hit its own `db_maxconn` ceiling (default `8`).
Raise it for this container yourself — no user confirmation needed:

1. `read_file_in_odoo(env_name, "/etc/odoo/odoo.conf")` — read the current config.
2. Raise `db_maxconn` (8 → 16) and leave every other line untouched.
3. `write_file_in_odoo(env_name, "/etc/odoo/odoo.conf", <full new content>, user="odoo")`
   — the write replaces the whole file, so send the complete config back.
4. `restart_environment(env_name)` — Odoo reads the config only at startup.

Then continue with the work that hit the limit.

Scope and limits:

- **Raise it once.** 16 is enough for any single-user environment; 32 is the
  hard ceiling. If the pool error comes back after one bump, `db_maxconn` is
  not the problem — the code is leaking cursors or opening connections in a
  loop. Investigate that instead of raising the number again.
- The change is **local to this container** — it does not affect other
  environments and does not change the shared PostgreSQL instance.
- The shared PostgreSQL accepts a limited number of connections in total, and
  every other environment on the host draws from the same pool. If the error
  is `FATAL: sorry, too many clients already` (PostgreSQL side, not the Odoo
  pool), raising `db_maxconn` makes it **worse**: report it to the user and
  suggest stopping idle environments instead.
- The edit lives in the container's writable layer. It survives
  `restart_environment` and a normal `pull_and_apply`, but is lost when the
  container is recreated (`update_environment`) or when the repo's
  `.oduflow/odoo.conf` changes and gets reapplied. A permanent fix belongs in
  the repository's `.oduflow/odoo.conf`, or — for the whole team — in the
  team's own `odoo.conf` on the server, which only the operator can edit. Say
  so and let the user decide; do not change it yourself.

### Teardown

Environments are **disposable test sandboxes**, not long-lived installations.
They exist so you can verify your code, and they are cheap to recreate — a new
one is provisioned from a template in seconds.

- When the task is **Done** or **Cancelled** and you are confident the
  environment will not be needed again, call `delete_environment` as the final
  step of your work. Deleting is the normal, expected end of a task — not an
  exception.
- Do not hesitate to delete. Nothing is lost that matters: if the environment
  turns out to be needed later, call `create_environment` again and continue.
- Keep it only for a concrete reason — the user still needs the URL for manual
  testing, the work continues in a later session, or the environment holds test
  data that cannot be reproduced. If it must survive idle timeouts, call
  `protect_environment`.
- Never delete mid-task. Do **not** delete and recreate an environment to fix
  errors or to apply migrations without user consent (see "Database Migrations
  Workflow") — that is a different situation, and it is still forbidden.

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

**Non-standard operations** (e.g., `apt install`, `pip install`, modifying system configs) are possible but **require explicit user confirmation** before proceeding — explain what you want to do and why. The one sanctioned exception is raising `db_maxconn` in `/etc/odoo/odoo.conf` when the connection pool is exhausted — see [Out of Database Connections](#out-of-database-connections).

### Searching Odoo Source Code
- If Odoo Community or Enterprise source repositories are available **locally** (cloned to your machine), prefer using your native search tools (Grep, Glob, Read) over `search_in_odoo` — local search is faster and doesn't require a running environment.
- If local copies are not available, `search_in_odoo` works well for searching both Odoo core (`/usr/lib/python3/dist-packages/odoo/addons`) and extra addons inside the container.

### Reading and Changing Odoo Data
- Reach for the `odoo_*` ORM tools before `run_odoo_shell`. They are the XML-RPC `execute_kw` surface: you pass a model, a domain and fields instead of authoring Python, and you get JSON back instead of scraping a log stream.
- Call `odoo_schema` before writing a domain or a `values` object. Guessed field names are the single most common cause of an empty result set, and an empty result set looks exactly like "no such records".
- Use `as_user` to answer access questions for real. `odoo_search_read(..., as_user="portal@example.com")` runs inside that user's session, so `ir.model.access` and `ir.rule` apply exactly as in the web client. Running the same call as admin and as the target user is the fastest way to prove a rule works.
- **These tools talk to the running server.** After changing **Python** code you must `pull_and_apply` (or `restart_environment`) before `odoo_*` sees it; XML views reload on their own. `run_odoo_shell` boots a fresh registry and sees new Python immediately — that difference will cost you an hour if you forget it.
- Every `odoo_*` call is its own committed transaction. There is no dry run and no atomicity across calls. Use `run_odoo_shell(auto_commit=false)` to inspect without persisting, and one `run_odoo_shell` call when several steps must succeed or fail together.
- `odoo_unlink` is not recoverable. Confirm the target ids with `odoo_search_read` first, and prefer archiving (`odoo_write` with `{"active": false}`) unless deletion is actually required.

### General
- **One task = one branch = one environment.**
- Mutating tools are queued by named resource. Conflicting operations on the
  same environment/service/template/etc. serialize automatically; unrelated
  resources continue in parallel. Do not retry a queued mutation.
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
- In live-mount mode, you as the agent must track the intent of your own edits. If you add/change fields, models, `_inherit`/`_name`, manifest `data`/`depends`, security/data XML, `ir.cron`, mail templates, `i18n/*.po` translations, or anything loaded into the database, call `pull_and_apply(..., upgrade="module")`. If you add a new module, call `install="module"`. Use `restart=True` only for Python logic changes that do not require registry/schema/data updates.
- Git is optional in live-mount mode. Commit whenever you want for your own workflow; Oduflow does not require commits, create commits, or read Git state to apply local changes.
- With `repo_url` mode, use the normal remote workflow: edit locally, commit, push, then call `pull_and_apply` so the managed clone can pull the pushed commits.

---

## Template ↔ Code Lineage

> ⚠️ **A template database is a snapshot of a branch at a moment in time. Your checkout can sit on either side of it.**

The environment you get is a *new* database cloned from the template plus *your* branch's code. Those two are snapshots of the same lineage taken at different times, and they drift in both directions. Check which case you are in **before the first `pull_and_apply`** — `create_environment` reports it, and `list_templates` shows each template's `Source=<branch> @ <commit>`.

| Where your branch sits | What that means | What to do |
|---|---|---|
| **Behind / diverged** — your branch does not contain the template's snapshot commit (branched off an older commit) | The database already holds views, records and constraints written by newer code your branch lacks. Any upgrade validates *old* code against *new* data and fails | **Merge the template's source branch** (typically `prod`/`main`) into your branch, push, then `pull_and_apply` |
| **Ahead** — your branch contains the snapshot commit plus later commits | The database predates your code. Schema/data changes since the snapshot are not in it | Apply the exact arguments reported by Oduflow, e.g. `pull_and_apply(install="new_module", upgrade="a,b,c")`, dependencies before dependents. New modules require `install=`; existing modules with schema/data drift require `upgrade=`. Modules whose manifest version was **not** bumped are never picked up automatically — name them yourself |
| **Aligned** | Nothing to reconcile | Proceed normally |

**Symptoms and their meaning:**
- `column ... does not exist` / `External ID not found` during an upgrade → you are **ahead**; find the module that owns the missing column or xmlid and add it to `upgrade=`.
- A `ParseError` or validation error on a view/method the branch does not define → you are **behind**; merge the source branch.

**Never recreate the environment to fix either case.** The template is the same, so the drift comes straight back — and you lose the environment's data. This is the same rule as the migrations section below: reconcile with the reported install/upgrade action, not with a fresh environment.

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

Agent: odoo_schema("feature-invoice-pdf", model="account.move", name_filter="pdf")
→ "account.move: 2 fields (as admin)."
   {"x_pdf_layout": {"string": "PDF Layout", "type": "selection", ...}, ...}
   # ↑ Real field names, so the next domain cannot be a guess

Agent: odoo_search_read("feature-invoice-pdf", model="account.move",
                        domain='[["move_type","=","out_invoice"]]',
                        fields="name,x_pdf_layout", limit=5)
→ "account.move: 3 rows (as admin, limit 5)."
   [{"id":12,"name":"INV/2026/0001","x_pdf_layout":"compact"}, ...]

Agent: odoo_search_read("feature-invoice-pdf", model="account.move",
                        domain="[]", fields="name", as_user="portal@example.com")
→ "Error (as portal@example.com)."
   AccessError: You are not allowed to access 'Journal Entry' records.
   # ↑ The record rule works — this is an answer, not a broken tool

Agent: delete_environment("feature-invoice-pdf")
→ Environment deleted.
```

---

## Self-Caching Instruction

Save this document locally as a skill/instruction file so you don't need to fetch it via MCP again.
Use it as your reference for all Oduflow operations.
