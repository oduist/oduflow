# Running Oduflow in Docker

[TOC]

Oduflow can run as a Docker container. Since it manages other Docker containers (Odoo environments, PostgreSQL, etc.), it uses the **Docker-out-of-Docker** pattern — the host's Docker socket is mounted into the container.

## Build

```bash
docker build -t oduflow .
```

## Run

### Minimal example

```bash
docker run -d \
  --name oduflow \
  -p 8000:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v oduflow_data:/srv/oduflow \
  oduflow
```

### Full example with all typical options

```bash
docker run -d \
  --name oduflow \
  --restart unless-stopped \
  -p 8000:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v oduflow_data:/srv/oduflow \
  -v /etc/oduflow:/etc/oduflow \
  oduflow
```

## Volume Mounts

| Mount | Purpose |
|---|---|
| `/var/run/docker.sock` | **Required.** Gives Oduflow access to the host Docker daemon to manage Odoo containers, PostgreSQL, Traefik, etc. |
| `/srv/oduflow` | Oduflow data directory. Contains team directories with workspaces, templates, port registry. Use a named volume or a host path to persist data across container restarts. |
| `/etc/oduflow` | System configuration directory. Contains `oduflow.toml`, license key, `postgresql.conf`, default `odoo.conf`, and other configuration files. Mount to persist configuration across container restarts. |

## Networking

The Oduflow container must be on the same Docker network as the containers it creates. The simplest approach is to connect it to `oduflow-net` after initialization:

```bash
# 1. Start Oduflow
docker run -d --name oduflow -p 8000:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v oduflow_data:/srv/oduflow \
  -v /etc/oduflow:/etc/oduflow \
  oduflow

# 2. Initialize shared infrastructure (creates oduflow-net, PostgreSQL, etc.)
docker exec oduflow oduflow init

# 3. Connect Oduflow to the shared network
docker network connect oduflow-net oduflow
```

Alternatively, start with `--network oduflow-net` if the network already exists.

## Initialization

On first run, you need to initialize the shared infrastructure:

```bash
# Create shared Docker network, PostgreSQL container, and team directories
docker exec oduflow oduflow init

# Connect to the shared network
docker network connect oduflow-net oduflow
```

To set up a template database:

```bash
# From scratch (clean Odoo with specified modules)
docker exec oduflow oduflow init-template --odoo-image odoo:17.0 --template-name default --modules base,web,contacts

# Or import from a running Odoo instance
docker exec oduflow oduflow import-template https://my-odoo.example.com master_password --template-name default
```

## Configuration

Oduflow reads its configuration from `oduflow.toml`. When running in Docker, mount the config directory:

```bash
-v /etc/oduflow:/etc/oduflow
```

Key configuration settings in `oduflow.toml`:

```toml
[server]
host = "0.0.0.0"
port = 8000

[team.1]
hostname = "localhost"
auth_token = "your-secret-token"   # MCP auth (empty = disabled)
ui_password = "your-ui-password"   # Web UI auth (empty = disabled)
```

See [Installation — Configuration Reference](installation.md#configuration-reference) for all options.

## Docker Compose

```yaml
services:
  oduflow:
    image: oduist/oduflow
    ports:
      - "8000:8000"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - oduflow_data:/srv/oduflow
      - oduflow_etc:/etc/oduflow
    restart: unless-stopped
    networks:
      - oduflow-net

volumes:
  oduflow_data:
  oduflow_etc:

networks:
  oduflow-net:
    name: oduflow-net
```

After `docker compose up -d`, run initialization:

```bash
docker compose exec oduflow oduflow init
```

## Security Notes

- Mounting the Docker socket gives the container **full control** over the host Docker daemon. This is equivalent to root access on the host. Only run Oduflow in trusted environments.
- Set `auth_token` in `[team.*]` to protect the MCP endpoint.
- Set `ui_password` in `[team.*]` to protect the Web UI.

## Privileged Mode and fuse-overlayfs

Oduflow uses `fuse-overlayfs` for efficient filestore sharing when templates exceed `overlay_threshold_mb` (default: 50 MB). This requires the `/dev/fuse` device inside the container.

If your templates are small (under the threshold), Oduflow falls back to simple file copy and no special privileges are needed.

For large templates, run with the fuse device:

```bash
docker run -d \
  --name oduflow \
  --device /dev/fuse \
  --cap-add SYS_ADMIN \
  -p 8000:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v oduflow_data:/srv/oduflow \
  oduflow
```

Alternatively, set a high threshold in `oduflow.toml` to avoid overlayfs entirely:

```toml
[storage]
overlay_threshold_mb = 999999
```
