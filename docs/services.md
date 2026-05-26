# Auxiliary Services

[TOC]

![Services Dashboard](img/services.png)

Oduflow can manage sidecar containers for auxiliary services your Odoo instance depends on — Redis, Meilisearch, Elasticsearch, RabbitMQ, or any other Docker-based service.

## Creating a Service

```bash
# Redis
oduflow call create_service redis redis:7 6379

# Meilisearch with environment variables
oduflow call create_service meilisearch getmeili/meilisearch:v1.6 7700 "" "MEILI_MASTER_KEY=abc123,MEILI_ENV=production"

# Elasticsearch
oduflow call create_service elasticsearch docker.elastic.co/elasticsearch/elasticsearch:8.11.0 9200 "" "discovery.type=single-node,ES_JAVA_OPTS=-Xms512m -Xmx512m"

# WireGuard VPN — needs NET_ADMIN to manage tun/iptables
oduflow call create_service '{"name":"vpn","image":"linuxserver/wireguard","port":51820,"net_admin":true}'
```

Services are:

- Attached to the shared `oduflow-net` network (accessible by all Odoo containers)
- Given an `unless-stopped` restart policy
- Automatically routed through Traefik with HTTPS when in traefik mode
- Labeled for management (`oduflow.managed=true`, `oduflow.service=<name>`)
- Always created from a freshly pulled image — `create_service` and `restore_service` explicitly pull before running, so mutable tags like `:latest` get the current published version instead of a stale local cache

### Linux Capabilities

Two optional flags grant additional container privileges:

- `net_admin` — adds the `NET_ADMIN` Linux capability. Required for VPN / WireGuard, `tun`/`tap` devices, and `iptables` manipulation inside the container.
- `privileged` — runs the container in privileged mode (full host access, all capabilities). Use with care — only when a service genuinely needs it (e.g. Docker-in-Docker, hardware passthrough).

Both can be enabled at the same time; on the Docker side, `privileged` implies all capabilities so `net_admin` is then redundant — but it is still recorded in the preset so disabling `privileged` later keeps `NET_ADMIN` active.

## Managing Services

```bash
# List all services with status, ports, URLs, and env vars
oduflow call list_services

# Full state of a single service — image + digest, port, hostname, host_mode,
# volumes, env vars, capabilities, restart count, started_at, has_preset
oduflow call get_service_info redis

# View service logs
oduflow call get_service_logs redis 200

# Restart a service
oduflow call restart_service redis

# Update a service (pull latest image, recreate container with same settings)
oduflow call update_service meilisearch

# Delete a service
oduflow call delete_service redis

# Execute a command inside a service container
oduflow call run_service_command redis "redis-cli ping"
```

### Inspecting a Service Before Recreating It

When you need to delete and recreate a service (e.g. to change the image tag or a single env var), call `get_service_info` first and reuse its fields in the new `create_service` call. The returned dict carries the full configuration (`image`, `port`, `hostname`, `env_vars`, `host_mode`, `volumes`, `cap_add`, `privileged`) so you do not lose anything that `list_services` truncates or that lived only inside the preset.

For a plain image refresh prefer `update_service`, which captures and preserves every setting automatically.

## Service Update Flow

The `update_service` operation:

1. Captures the current image name, environment variables, port, hostname, volumes, and capabilities (`cap_add`, `privileged`)
2. Pulls the latest version of the image
3. Compares image digests — if unchanged, reports "already up-to-date"
4. If the image changed: stops the old container, removes it, and creates a new one with identical settings

## Service Presets

Every time a service is created, its configuration (image, port, hostname, environment variables, volumes, `host_mode`, `cap_add`, `privileged`) is automatically saved as a **preset** in `{team_data_dir}/service_presets.json`. This allows you to restore a service after deletion without re-entering its configuration.

```bash
# List saved presets
oduflow call list_service_presets

# Restore a previously deleted service
oduflow call restore_service redis

# Remove a saved preset
oduflow call delete_service_preset redis
```
