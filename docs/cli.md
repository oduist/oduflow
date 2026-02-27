# CLI Reference

[TOC]

## Global Options

```bash
# Show version
oduflow --version

# Use a custom .env file
oduflow --env /path/to/.env <command>
```

## Running the Server

```bash
# Start the MCP server (instance 1 by default)
oduflow run-instance

# Start a specific instance
oduflow run-instance --instance 2

# Start with a custom .env file
oduflow --env /path/to/.env run-instance
```

By default, `run-instance` loads the environment file from `/etc/oduflow/instance_{ID}.env`. Use the global `--env` flag to override.

## System Commands

```bash
# Initialize shared infrastructure (network, DB, Traefik)
oduflow init

# Initialize and install a license in one step
oduflow init --license /path/to/license.key

# Initialize per-instance directories (workspaces, templates)
oduflow init-instance --instance 1

# Update agent guides to the latest bundled versions (overwrites existing)
oduflow init-instance --instance 1 --update-guides

# Destroy all shared infrastructure (requires no active environments)
oduflow destroy
```

## Template Commands

```bash
# Generate a clean template from a Docker image
oduflow init-template --odoo-image odoo:17.0 --template-name myproject [--modules base,web,sale] [--force]

# Start interactive template editor
oduflow template-up --odoo-image odoo:17.0 --template-name myproject

# Stop template editor and save changes
oduflow template-down --template-name myproject

# Reload template DB from a dump file
oduflow reload-template <template_name> [--dump-path /path/to/new.dump]

# Save a branch environment as the new template
oduflow template-from-env <branch> --template-name myproject

# List all template profiles
oduflow list-templates

# Delete a template profile
oduflow delete-template <template_name>

# Import a template from a running Odoo instance
oduflow import-template <odoo_url> <master_pwd> --template-name myproject [--db-name <db>]
```

## Service Commands

```bash
# List all managed services
oduflow list-services
```

## Maintenance Commands

```bash
# Show orphaned databases, workspaces, and port entries (dry-run by default)
oduflow cleanup

# Same as above — only show what would be removed
oduflow cleanup --dry-run

# Actually remove orphaned resources
oduflow cleanup --force
```

The `cleanup` command detects and removes resources that no longer have a corresponding running or stopped container:

- **Orphan databases** — PostgreSQL databases with the `oduflow_` prefix that have no matching environment container
- **Orphan workspaces** — workspace directories on disk that have no matching environment container
- **Orphan port entries** — entries in `ports.json` that have no matching environment container

By default, `cleanup` runs in **dry-run mode** and only reports what would be removed. Use `--force` to actually delete the orphaned resources.

## Systemd Service

```bash
# Install and enable systemd service for instance 1
oduflow systemd-install --instance 1

# For multi-instance setups
oduflow systemd-install --instance 2

# Remove the systemd service
oduflow systemd-uninstall --instance 1
```

The `systemd-install` command generates a unit file at `/etc/systemd/system/oduflow.service` (or `oduflow-{ID}.service` for instances 2-9), runs `daemon-reload`, and enables the service. The unit reads configuration from `/etc/oduflow/instance_{ID}.env`.

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
oduflow call create_environment dev https://github.com/owner/repo.git odoo:17.0
oduflow call delete_environment dev
oduflow call list_environments
oduflow call get_environment_logs main 50
oduflow call exec_in_odoo dev "ls /mnt/extra-addons"
oduflow call create_service redis redis:7 6379

# Call a tool with JSON-encoded arguments
oduflow call create_environment '{"branch_name":"dev","repo_url":"https://github.com/owner/repo.git","odoo_image":"odoo:17.0","template_name":"myproject"}'

# Type coercion is automatic: int, bool, and float parameters are cast from strings
oduflow call get_environment_logs dev 500
```
