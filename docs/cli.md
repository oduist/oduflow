# CLI Reference

## Global Options

```bash
# Use a custom .env file (default: .env in current directory)
oduflow --env /path/to/.env <command>
```

## Server & System Commands

```bash
# Start the MCP server (default command)
oduflow

# Initialize shared infrastructure (network, DB, Traefik)
oduflow init

# Initialize and install a license in one step
oduflow init --license /path/to/license.key

# Initialize per-instance directories (workspaces, templates)
oduflow init-instance

# Destroy all shared infrastructure (requires no active environments)
oduflow destroy
```

## Template Commands

```bash
# Generate a clean template from a Docker image
oduflow init-template --odoo-image odoo:17.0 [--modules base,web,sale] [--template-name myproject] [--force]

# Start interactive template editor
oduflow template-up --odoo-image odoo:17.0 [--template-name myproject]

# Stop template editor and save changes
oduflow template-down [--template-name myproject]

# Reload template DB from a dump file
oduflow reload-template [--template-name default] [--dump-path /path/to/new.dump]

# Save a branch environment as the new template
oduflow template-from-env <branch> [--template-name default]

# List all template profiles
oduflow list-templates

# Drop a template profile
oduflow drop-template <template_name>

# Import a template from a running Odoo instance
oduflow import-template <odoo_url> <master_pwd> [--db-name <db>] [--template-name default]
```

## Service Commands

```bash
# List all managed services
oduflow list-services
```

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
oduflow call exec_in_environment dev "ls /mnt/extra-addons"
oduflow call create_service redis redis:7 6379

# Call a tool with JSON-encoded arguments
oduflow call create_environment '{"branch_name":"dev","repo_url":"https://github.com/owner/repo.git","odoo_image":"odoo:17.0","template_name":"myproject"}'

# Type coercion is automatic: int, bool, and float parameters are cast from strings
oduflow call get_environment_logs dev 500
```
