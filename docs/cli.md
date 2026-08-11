# CLI Reference

## Global Options

```bash
# Show version
oduflow --version
```

## Running the Server

```bash
# Single-user / stdio mode (default — for local MCP clients)
oduflow
uvx oduflow

# Server / HTTP mode (for remote and multi-user deployments)
oduflow --transport http
oduflow -t http
uvx oduflow --transport http
uvx oduflow -t http
```

Shared infrastructure (Docker network, PostgreSQL, team directories) is initialized automatically on startup.

**stdio mode** — the server communicates over stdin/stdout. The MCP client starts the process directly; no network port is needed. Ideal for local clients like Claude Desktop, Windsurf, etc.

**HTTP mode** — starts a persistent HTTP server on `http://0.0.0.0:8000` by default. Exposes the MCP endpoint at `/mcp`, a Web Dashboard at `/`, and a REST API at `/api/`. MCP uses Bearer tokens; the dashboard uses a form/session cookie, while API clients may also use HTTP Basic auth.

Configuration is loaded from `oduflow.toml` (see [Installation](installation.md#configuration-reference)).
See [Quick Start](quick-start.md) for MCP client configuration examples for both modes.

To reconcile a declarative Stack before starting the server:

```bash
oduflow --stack /path/to/oduflow.yaml --stack-team 1 --transport http
```

Startup stops with a non-zero exit if Stack validation, preflight, or apply
fails. See [Declarative Stacks](stacks.md).

## Declarative Stack Commands

```bash
# Local syntax and schema validation (does not require Docker)
oduflow stack validate oduflow.yaml

# Read-only comparison with live resources
oduflow stack plan oduflow.yaml --team 1

# Reconcile under the team's lock
oduflow stack apply oduflow.yaml --team 1

# JSON status: drift plan plus the last successful apply record
oduflow stack status oduflow.yaml --team 1
```

Stack apply is additive and non-destructive in V1. Existing resources owned by
someone else and environment changes that require replacement are reported as
conflicts; no automatic deletion or pruning is performed.

## System Commands

```bash
# Destroy all shared infrastructure (requires no active environments)
oduflow destroy

# Refresh deployed files bundled with the installed Oduflow version
oduflow upgrade

# Refresh non-interactively (for scripts and automated deployments)
oduflow upgrade --force

# Preview the unified host resource plan and managed config diffs
oduflow retune-postgres

# Back up and write configs; stage production Odoo configs in containers
oduflow retune-postgres --apply
```

`retune-postgres` accounts for `[production].enabled` and does not restart
containers. For existing productions, `--apply` also regenerates `odoo.conf`
with the planned worker count and copies it into the container; the command
then lists every PostgreSQL and Odoo container that should be restarted. It
refuses to replace a custom PostgreSQL config unless `--apply --force` is
given. See [PostgreSQL resource planning](installation.md#configuration-file-overrides).

`oduflow upgrade` compares and interactively refreshes the system
`postgresql.conf` plus each team's `odoo.conf`, agent guides, and bundled
sanitize script. It prints the affected paths and asks for confirmation before
overwriting them. A deployed file whose first line is exactly `# KEEP` is left
unchanged. This command is separate from upgrading the Python package (for
example, `uv tool upgrade oduflow`). Pass `--force` to accept the listed
overwrites without reading from the terminal; the warning and affected-file
list are still printed, and `# KEEP` files are still preserved.

## Template Commands

All template commands accept `--team` to specify the team ID (default: `1`).

```bash
# Generate a clean template from a Docker image
oduflow init-template --odoo-image odoo:19.0 --template-name myproject [--modules base,web,sale] [--force] [--team 1]

# Save a branch environment as the new template.
# Other environments on this template keep their filestore changes by default;
# pass --reset-env-changes to discard them and reset to the new baseline.
oduflow template-from-env <branch> --template-name myproject [--reset-env-changes] [--team 1]

# Re-apply a template's current filestore to live overlay environments
# (non-destructive by default; --reset-env-changes discards env deltas)
oduflow refresh-template <template_name> [--reset-env-changes] [--team 1]

# Attach or replace a template filestore from a local dir, archive, rsync://, or SSH rsync source
oduflow attach-filestore <template_name> <source> [--strip-prefix auto|none|PREFIX] [--reset-env-changes] [--team 1]

# Reload template DB from a dump file
oduflow reload-template <template_name> [--dump-path /path/to/new.dump] [--team 1]

# Sync template from S3 or local path and reload DB
oduflow reload-template <template_name> --source s3://bucket/path/ [--quiet] [--team 1]
oduflow reload-template <template_name> --source /backups/prod-latest/ [--team 1]

# List all template profiles
oduflow list-templates [--team 1]

# Delete a template profile
oduflow delete-template <template_name> [--team 1]

# Import a template from a running Odoo instance
oduflow import-template <odoo_url> <master_pwd> --template-name myproject [--db-name <db>] [--without-filestore] [--team 1]
```

`template-from-env`, `refresh-template`, `attach-filestore`, and `reload-template --source` are **non-destructive** for live overlay environments: each is unmounted and remounted against the new template filestore while keeping its `upper` changes. Use `--reset-env-changes` (on `template-from-env`/`refresh-template`/`attach-filestore`) to reset environments to the clean baseline instead. `import-template` creates a new template and refuses an existing template name.

## Service Commands

```bash
# List all managed services
oduflow list-services [--team 1]
```

## Maintenance Commands

```bash
# Show orphaned databases, workspaces, and port entries (dry-run by default)
oduflow cleanup [--team 1]

# Same as above — only show what would be removed
oduflow cleanup --dry-run [--team 1]

# Actually remove orphaned resources
oduflow cleanup --force [--team 1]
```

The `cleanup` command detects and removes resources that no longer have a corresponding running or stopped container:

- **Orphan databases** — PostgreSQL databases with the `oduflow_` prefix that have no matching environment container
- **Orphan workspaces** — workspace directories on disk that have no matching environment container
- **Orphan port entries** — entries in `ports.json` that have no matching environment container

By default, `cleanup` runs in **dry-run mode** and only reports what would be removed. Use `--force` to actually delete the orphaned resources.

## Systemd Service

```bash
# Install and enable systemd service
oduflow systemd-install

# Remove the systemd service
oduflow systemd-uninstall
```

The `systemd-install` command generates a unit file at `/etc/systemd/system/oduflow.service`, runs `daemon-reload`, and enables the service.

See [Auto-start with systemd](installation.md#auto-start-with-systemd) for the full setup guide.

## Tool Introspection

```bash
# List all registered MCP tools with parameters
oduflow list [--verbose]
```

## Direct Tool Invocation

You can invoke any registered MCP tool directly from the terminal using `oduflow call`, without running the server or connecting an MCP client. This is useful for scripting, debugging, and manual operations.

```bash
# List all available tools with their parameters
oduflow call

# Call a tool with positional arguments (mapped to parameters in order)
oduflow call create_environment dev "" "" https://github.com/owner/repo.git odoo:19.0
oduflow call delete_environment dev
oduflow call list_environments
oduflow call get_environment_logs main 50
oduflow call run_odoo_command dev "ls /mnt/extra-addons"
oduflow call create_service redis redis:7 6379

# Call a tool with JSON-encoded arguments
oduflow call create_environment '{"branch":"dev","repo_url":"https://github.com/owner/repo.git","odoo_image":"odoo:19.0","template_name":"myproject"}'

# Service with NET_ADMIN capability (VPN / tun / iptables)
oduflow call create_service '{"name":"vpn","image":"linuxserver/wireguard","port":51820,"net_admin":true}'

# Type coercion is automatic: int, bool, and float parameters are cast from strings
oduflow call get_environment_logs dev 500
```
