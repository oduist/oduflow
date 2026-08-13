# Oduflow — Agentic Odoo Development
Version: 5

## Purpose

Oduflow provisions an isolated Odoo environment for each branch and gives the
agent a closed feedback loop: edit code → apply it → read the result → fix →
test. Load this guide once at the start of the session and use it as the
workflow contract for the rest of that session.

Before writing or refactoring Odoo module code, call
`get_odoo_development_guide(version="<major>")` once for the target Odoo
version (15–19).

## Choose the code-delivery mode

- **`repo_url` mode:** Oduflow owns a managed clone. Edit in your local
  checkout, commit and push, then call `pull_and_apply`.
- **`local_path` mode:** Oduflow bind-mounts a local checkout. Edit that folder
  directly; no push is required. Git commits are optional and do not control
  what Oduflow applies.

Use `list_environments` before creating anything. Reuse the environment for
the current branch when it exists; otherwise call `create_environment` with
the branch, correct Odoo image, and either `repo_url` or `local_path`. Show the
returned URL to the user.

## Core workflow

```text
1. list_environments; create_environment only if needed
2. get_odoo_development_guide before module code changes
3. edit code locally
4. repo_url: commit + push; local_path: no delivery step
5. pull_and_apply with the action required by the edit
6. read that response; if it failed, fix and repeat from step 3
7. run_odoo_tests for the changed modules
8. delete_environment when the task is done or cancelled and nobody still
   needs the URL or its test data
```

Do not delete and recreate an environment to fix an application error or run
a migration. It recreates the same starting state and discards useful test
data. Environments auto-stop when idle and container-level tools wake them
automatically; a wake-up `Note:` is informational. Use `protect_environment`
only when an environment must survive the configured idle lifecycle.

## Choose the apply action

When you authored the change, tell `pull_and_apply` what it requires. Leave
all action arguments empty only when pulling commits you did not author and
want Oduflow to classify automatically.

| Change | Action |
|---|---|
| New, not-yet-installed module | `install="module"` |
| Fields/models, `_inherit`/`_name`, security or data records, cron, mail templates, translations, manifest `data`/`depends`, or migrations | `upgrade="module"` |
| Python method/logic only, with no schema or data change | `restart=True` |
| View/QWeb XML or browser assets only | Usually no server action; refresh the browser |

A field change needs an upgrade, not merely a restart. In explicit mode,
`pull_and_apply` may return a guardrail warning when the requested action looks
incomplete; correct the action and call it again. `strict=True` turns that
warning into a refusal.

## Read the right output

`pull_and_apply`, module install/upgrade, and `run_odoo_tests` return their own
command output, errors, and tracebacks. Read that response. Do not call
`get_environment_logs` looking for the same error: it only shows the running
Odoo server process and is appropriate for failures during HTTP requests or
after a restart.

Large command responses are cached automatically. The response includes an
`output_id`; use `read_output` with `errors`, `grep`, `lines`, or `tail` only
when the returned summary does not answer the question.

Use the most specific inspection surface:

- Odoo records with real ACLs: `odoo_schema` and the `odoo_*` tools.
- Multi-step ORM logic, private methods, `sudo()`, or rollback-only inspection:
  `run_odoo_shell` (`auto_commit=false` for rollback).
- SQL and schema checks: `run_db_query`.
- Runtime process logs: `get_environment_logs`.
- Container files/source: `read_file_in_odoo` or `search_in_odoo`.

Each `odoo_*` call commits independently and talks to the already-running
server. After a Python edit it sees old code until the environment restarts;
`run_odoo_shell` starts a fresh registry. Confirm ids before `odoo_unlink` and
prefer archiving when the model supports it.

## Code and container boundary

The Odoo container is a runtime, not the agent's source workspace.

- Never edit repository source inside the container and never run Git there.
- In `repo_url` mode, edit locally → commit → push → `pull_and_apply`.
- In `local_path` mode, edit the mounted local folder → `pull_and_apply`.
- Reading Odoo/core files and running diagnostic commands inside the container
  is fine.
- Use `run_odoo_command` only when a dedicated Oduflow tool does not express
  the operation. It runs as `odoo` by default; privileged package or system
  changes require an explicit reason and user approval.

Mutexed operations on the same environment return `BusyError` while earlier
server-side work is still running, even if its client timed out. Wait for that
operation; do not restart or recreate the environment to clear the lock.

## Per-environment Odoo configuration

Use `/etc/odoo/odoo.conf` for a temporary setting that should affect only the
current environment:

1. Read the complete file with
   `read_file_in_odoo(env_name, "/etc/odoo/odoo.conf")`.
2. Change only the required option and preserve every other line.
3. Write the complete file back with
   `write_file_in_odoo(env_name, "/etc/odoo/odoo.conf", content, user="odoo")`.
4. Call `restart_environment(env_name)`; Odoo reads these settings at startup.
5. Re-read the file and verify the behavior or runtime logs.

Common scoped changes:

| Goal | Change | Important limit |
|---|---|---|
| Recover from `psycopg2.pool.PoolError: The Connection Pool Is Full` | `db_maxconn = 8` → `16` | Raise once; if it recurs, investigate leaked connections. Do not raise it for PostgreSQL's `FATAL: sorry, too many clients already`—that is a shared-cluster limit and a higher value makes it worse. |
| Exercise scheduled jobs | `max_cron_threads = 0` → `1` | Cron is disabled by default in disposable dev environments; enable it only when the task needs cron execution. |
| Reproduce worker-mode behavior | `workers = 0` → a small value such as `2` | Workers consume additional RAM and database connections. This is a focused test setting, not production sizing; keep `db_maxconn` and the shared PostgreSQL limit in mind. |

Explain the scoped runtime change before making it unless the user already
requested that condition explicitly. The existing `db_maxconn` 8 → 16 repair
for the exact Odoo pool-exhaustion error is a sanctioned diagnostic fix.

A direct `/etc/odoo/odoo.conf` edit survives `restart_environment` and normal
code sync, but is lost when `update_environment` recreates the container or a
source config is reapplied. For a durable project setting, edit
`.oduflow/odoo.conf` in the repository and deliver it through the normal
workflow; call `pull_and_apply(..., restart=True)` so Oduflow regenerates the
merged config. A team-wide default belongs in the team's server-side
`odoo.conf` and is an operator change, not a branch task.

## Templates, schema changes, and migrations

A template database is a snapshot of code at a particular commit.
`create_environment` reports whether the branch is aligned, ahead, behind, or
diverged from that snapshot.

- **Behind/diverged:** merge the template's source branch into the working
  branch before applying old code to newer database state.
- **Ahead:** apply the reported module installs/upgrades, dependencies first.
- **Aligned:** proceed normally.

Environment creation clones the database as-is; it does not run module
migrations. Deliver the migration and manifest version bump, then execute
`pull_and_apply(..., upgrade="module")` (or `upgrade_odoo_modules`) and verify
the expected schema/data with `run_db_query`. Read the upgrade response for
errors. Never recreate the environment as a substitute for the upgrade.

## Translations

After loading translation changes, call `translation_status`. It compares the
module catalogue, committed `.po`, and database, then returns a verdict and a
`Next:` action. Follow that action instead of deriving a diagnosis from raw
counts. Use `export_module_translations` when it asks for a fresh catalogue.

Write English source strings; localized text belongs in `i18n/*.po`.

## Finish the task

Run the relevant Odoo tests after the last successful apply. Keep the
environment only for a concrete reason: ongoing work, user testing through its
URL, or irreplaceable test data. Otherwise delete it as the final workflow
step and report the verification result to the user.
