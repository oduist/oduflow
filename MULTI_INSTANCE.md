# Multi-Instance Support for Oduflow

Oduflow now supports running multiple independent instances on a single machine. Each instance has isolated environments, templates, and services while sharing the Docker network and PostgreSQL database.

## Overview

- **Instance ID**: Numeric identifier (1, 2, 3, ...). Default: `1`
- **Database prefix**: Each instance's databases are prefixed with instance ID (e.g., `oduflow_1_main`, `oduflow_2_main`)
- **ODUFLOW_DATA_DIR**: Each instance gets its own subdirectory inside the data directory (e.g., `/srv/oduflow/instance_1`, `/srv/oduflow/instance_2`)
- **Shared resources**: Docker network (`oduflow-net`), PostgreSQL container (`oduflow-db`), Traefik (if enabled)

## Configuration

### Per-Instance Setup

Set the environment variable `ODUFLOW_INSTANCE_ID` to identify your instance:

```bash
# Instance 1 (default)
export ODUFLOW_INSTANCE_ID=1
# Data stored in /srv/oduflow/instance_1

# Instance 2
export ODUFLOW_INSTANCE_ID=2
# Data stored in /srv/oduflow/instance_2
```

Or in `.env`:

```env
# Instance 1
ODUFLOW_INSTANCE_ID=1

# ... or Instance 2
ODUFLOW_INSTANCE_ID=2
```

Optionally override the base data directory (default: `/srv/oduflow`):

```env
ODUFLOW_DATA_DIR=/custom/path
# Instance 1 data → /custom/path/instance_1
# Instance 2 data → /custom/path/instance_2
```

If `ODUFLOW_DATA_DIR` is not set, it defaults to `/srv/oduflow` with instances stored as `instance_{ID}` subdirectories.

## Lifecycle

### Step 1: Initialize Shared Infrastructure (once)

This creates the Docker network, PostgreSQL database, and (if enabled) Traefik reverse proxy.

```bash
# Run this once, before creating any instances
oduflow init
```

### Step 2: Initialize Per-Instance Directories

For each instance:

```bash
# Instance 1
export ODUFLOW_INSTANCE_ID=1
oduflow init-instance

# Instance 2
export ODUFLOW_INSTANCE_ID=2
oduflow init-instance
```

This creates workspaces and template directories for each instance.

### Step 3: Set Up Templates and Environments

Each instance manages its own templates and environments:

```bash
# Instance 1: Set up a template
export ODUFLOW_INSTANCE_ID=1
oduflow init-template --odoo-image odoo:17.0

# Instance 1: Create environments
oduflow call create_environment feature-x https://github.com/org/repo.git odoo:17.0

# Instance 2: Completely independent setup
export ODUFLOW_INSTANCE_ID=2
oduflow init-template --odoo-image odoo:15.0

oduflow call create_environment bugfix-y https://github.com/org/other-repo.git odoo:15.0
```

### Step 4: Cleanup

To remove all instances and shared infrastructure:

```bash
# First, manually delete any remaining environments (optional, but recommended)
export ODUFLOW_INSTANCE_ID=1
oduflow call delete_environment feature-x

export ODUFLOW_INSTANCE_ID=2
oduflow call delete_environment bugfix-y

# Finally, destroy shared infrastructure
oduflow destroy
```

## Database Naming

Databases are prefixed with the instance ID:

| Instance | Branch | Database Name |
|----------|--------|---------------|
| 1 | `main` | `oduflow_1_main` |
| 1 | `feature/payments` | `oduflow_1_feature-payments` |
| 2 | `main` | `oduflow_2_main` |
| 2 | `feature/payments` | `oduflow_2_feature-payments` |

Template databases follow the same pattern:

| Instance | Template | Database Name |
|----------|----------|---------------|
| 1 | `default` | `odoo_ref_1_default` |
| 1 | `v17` | `odoo_ref_1_v17` |
| 2 | `default` | `odoo_ref_2_default` |

## Container Labeling

All Oduflow-managed containers are labeled with:

- `oduflow.managed=true` — identifies Oduflow containers
- `oduflow.instance={INSTANCE_ID}` — identifies which instance owns the container

When listing environments or services, Oduflow automatically filters by instance ID:

```bash
export ODUFLOW_INSTANCE_ID=1
oduflow call list_environments  # Shows only Instance 1 environments

export ODUFLOW_INSTANCE_ID=2
oduflow call list_environments  # Shows only Instance 2 environments
```

## Shared vs. Per-Instance Resources

| Resource | Shared | Per-Instance | Notes |
|----------|--------|--------------|-------|
| Docker network (`oduflow-net`) | ✓ | | All instances communicate via this network |
| PostgreSQL (`oduflow-db`) | ✓ | | Single DB, namespaced by instance ID in database names |
| Traefik (`oduflow-traefik`) | ✓ | | Ports 80/443 shared; different domains per instance supported |
| Port registry (`ports.json`) | | ✓ | Each instance has its own port allocation file |
| Workspaces (`workspaces/`) | | ✓ | Stored in `ODUFLOW_DATA_DIR/instance_{ID}` per instance |
| Templates (`templates/`) | | ✓ | Stored in `ODUFLOW_DATA_DIR/instance_{ID}` per instance |
| Environments | | ✓ | Database, filestore, containers labeled by instance |
| Services | | ✓ | Auxiliary services (Redis, etc.) per instance |

## Example: Running 2 Instances Side-by-Side

### Terminal 1: Instance 1 (Odoo 17)

```bash
export ODUFLOW_INSTANCE_ID=1
export ODUFLOW_PORT=8000

# First time: initialize shared infrastructure
oduflow init

# Initialize instance
oduflow init-instance

# Set up template
oduflow init-template --odoo-image odoo:17.0

# Start the server
oduflow  # Runs on http://localhost:8000/mcp
```

### Terminal 2: Instance 2 (Odoo 15)

```bash
export ODUFLOW_INSTANCE_ID=2
export ODUFLOW_PORT=8001

# Initialize instance (shared infrastructure already up from Instance 1)
oduflow init-instance

# Set up template
oduflow init-template --odoo-image odoo:15.0

# Start the server
oduflow  # Runs on http://localhost:8001/mcp
```

Now both instances are running independently:
- Instance 1: Odoo 17 environments on ports 50000–50050
- Instance 2: Odoo 15 environments on ports 50051–50100

## Environment Variables Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `ODUFLOW_INSTANCE_ID` | `1` | Instance identifier (1-9) |
| `ODUFLOW_DATA_DIR` | `/srv/oduflow` | Base directory for instance data |
| `ODUFLOW_WORKSPACES_DIR` | `{ODUFLOW_DATA_DIR}/instance_{ID}/workspaces` | Per-instance workspace directory |
| `ODUFLOW_PORT` | `8000` | HTTP server port (choose different port per instance) |
| `ODUFLOW_AUTH_TOKEN` | (empty) | Bearer token for HTTP auth |
| `ODUFLOW_ROUTING_MODE` | `port` | `port` or `traefik` |
| `ODUFLOW_BASE_DOMAIN` | (empty) | Base domain for Traefik (shared) |
| `ODUFLOW_ACME_EMAIL` | (empty) | Let's Encrypt email (shared) |
