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
  -v oduflow_data:/srv/oduflow_data_1 \
  oduflow
```

### With `.env` file

```bash
docker run -d \
  --name oduflow \
  --env-file .env \
  -p 8000:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v oduflow_data:/srv/oduflow_data_1 \
  oduflow
```

### Full example with all typical options

```bash
docker run -d \
  --name oduflow \
  --restart unless-stopped \
  --env-file .env \
  -p 8000:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v oduflow_data:/srv/oduflow_data_1 \
  -v /etc/oduflow:/etc/oduflow \
  oduflow
```

## Volume Mounts

| Mount | Purpose |
|---|---|
| `/var/run/docker.sock` | **Required.** Gives Oduflow access to the host Docker daemon to manage Odoo containers, PostgreSQL, Traefik, etc. |
| `/srv/oduflow_data_1` | Oduflow data directory (`ODUFLOW_HOME`). Contains workspaces, templates, port registry. Use a named volume or a host path to persist data across container restarts. |
| `/etc/oduflow` | System configuration directory. Contains license key, database settings, default `odoo.conf`, and other configuration files. Mount to persist configuration across container restarts. |

## Networking

The Oduflow container must be on the same Docker network as the containers it creates. The simplest approach is to connect it to `oduflow-net` after initialization:

```bash
# 1. Start Oduflow
docker run -d --name oduflow -p 8000:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v oduflow_data:/srv/oduflow_data_1 \
  oduflow

# 2. Initialize shared infrastructure (creates oduflow-net, PostgreSQL, etc.)
docker exec oduflow oduflow init

# 3. Connect Oduflow to the shared network
docker network connect oduflow-net oduflow

# 4. Initialize instance directories
docker exec oduflow oduflow init-instance
```

Alternatively, start with `--network oduflow-net` if the network already exists.

## Initialization

On first run, you need to initialize the shared infrastructure and instance:

```bash
# Create shared Docker network, PostgreSQL container
docker exec oduflow oduflow init

# Connect to the shared network
docker network connect oduflow-net oduflow

# Create instance directories (workspaces, templates)
docker exec oduflow oduflow init-instance
```

To set up a template database:

```bash
# From scratch (clean Odoo with specified modules)
docker exec oduflow oduflow init-template --odoo-image odoo:17.0 --modules base,web,contacts

# Or import from a running Odoo instance
docker exec oduflow oduflow call import_template_from_odoo https://my-odoo.example.com master_password
```

## Environment Variables

All environment variables from `.env.example` are supported. Key ones for Docker:

| Variable | Default | Description |
|---|---|---|
| `ODUFLOW_TRANSPORT` | `http` | Transport mode: `http` or `stdio` |
| `ODUFLOW_HOST` | `0.0.0.0` | Bind address |
| `ODUFLOW_PORT` | `8000` | HTTP port |
| `ODUFLOW_AUTH_TOKEN` | *(empty)* | Bearer token for MCP auth (empty = disabled) |
| `ODUFLOW_HOME` | `/srv/oduflow_data_1` | Data directory |
| `EXTERNAL_HOST` | `localhost` | Hostname used in generated URLs |

## Docker Compose

```yaml
services:
  oduflow:
    image: oduist/oduflow
    ports:
      - "8000:8000"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - oduflow_data:/srv/oduflow_data_1
      - oduflow_etc:/etc/oduflow
    env_file: .env
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
docker compose exec oduflow oduflow init-instance
```

## Security Notes

- Mounting the Docker socket gives the container **full control** over the host Docker daemon. This is equivalent to root access on the host. Only run Oduflow in trusted environments.
- Set `ODUFLOW_AUTH_TOKEN` to protect the MCP endpoint.
- Set `ODUFLOW_UI_PASSWORD` to protect the Web UI.

## Privileged Mode and fuse-overlayfs

Oduflow uses `fuse-overlayfs` for efficient filestore sharing when templates exceed `ODUFLOW_OVERLAY_THRESHOLD_MB` (default: 50 MB). This requires the `/dev/fuse` device inside the container.

If your templates are small (under the threshold), Oduflow falls back to simple file copy and no special privileges are needed.

For large templates, run with the fuse device:

```bash
docker run -d \
  --name oduflow \
  --device /dev/fuse \
  --cap-add SYS_ADMIN \
  -p 8000:8000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v oduflow_data:/srv/oduflow_data_1 \
  oduflow
```

Alternatively, raise the threshold to avoid overlayfs entirely:

```bash
ODUFLOW_OVERLAY_THRESHOLD_MB=999999
```
