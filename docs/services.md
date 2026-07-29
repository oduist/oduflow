# Auxiliary Services

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

# Publish only selected HTTP prefixes, each on its own backend port
oduflow call create_service '{
  "name":"fs",
  "image":"oduist/freeswitch:latest",
  "hostname":"fs",
  "host_mode":true,
  "routes":[
    {"path":"/RPC2","port":8080,"strip_prefix":false},
    {"path":"/portal","port":8080,"strip_prefix":false}
  ]
}'

# Internal-only broker — no port, no routes, no hostname
oduflow call create_service '{"name":"nats","image":"nats:2.10","internal_only":true}'
```

Services are:

- Attached to the team's isolated Docker network `oduflow-{team_id}-net` (reachable by that team's Odoo containers and other services)
- Given an `unless-stopped` restart policy
- Automatically routed through Traefik with HTTPS when in traefik mode
- In Traefik TLS mode, automatically given the exact system mount `oduflow-traefik-acme:/etc/traefik:ro`
- Labeled for management (`oduflow.managed=true`, `oduflow.service=<name>`)
- Always created from a freshly pulled image — `create_service` and `restore_service` explicitly pull before running, so mutable tags like `:latest` get the current published version instead of a stale local cache

### Connecting from Odoo

Inside the team's Docker network the service is reachable by its **container
name** — the DNS name is exactly `oduflow-{team_id}-svc-{name}` (e.g.
`oduflow-1-svc-redis`). There is no shorter alias such as `redis` or
`oduflow-svc-redis`. This is precisely the `Container:` / `Internal hostname:`
value that `create_service` and `get_service_info` report, so configure Odoo
against that:

```
oduflow-1-svc-redis:6379
```

The `URL:` line printed by the service tools is the **external** Traefik/host
address, not the internal one — do not use it for in-cluster connections.

`host_mode` services are not on the team network, so they are not resolvable by
container name; reach them via `host.docker.internal` instead.

### Restricted HTTP Path Routes

In Traefik mode, `routes` can replace the single catch-all `port`. Each route
publishes a URL prefix on the service's hostname and forwards it to another
HTTP port of the **same service**. This works in both networking modes:

- Bridge services are reached on their private IP in the team's Docker network.
- `host_mode` services are reached through `host.docker.internal`.

Routes are prefix matches on path-segment boundaries: `/api` accepts `/api` and
`/api/...`, but not `/apix`. When routes are present Oduflow does not create the
hostname-only catch-all router, so every unlisted path receives Traefik's 404
without reaching the service. Set `strip_prefix=true` when the backend serves
from `/`; Traefik then sends `/portal/assets/app.js` as `/assets/app.js` and
adds `X-Forwarded-Prefix: /portal`.

`routes` is intentionally not an arbitrary reverse-proxy configuration: a route
contains only `path`, `port`, and optional `strip_prefix`, and always targets the
same managed service. It is available only with `[routing].mode = "traefik"`.
Raw TCP/UDP protocols cannot be routed by URL path.

### Internal-Only Services

Not every sidecar belongs on the public internet. A message broker, a cache, or
an internal API is consumed by Odoo over the team's Docker network and has no
business answering requests from the outside. Pass `internal_only=true` and
Oduflow publishes nothing at all:

```bash
oduflow call create_service '{"name":"nats","image":"nats:2.10","internal_only":true}'
```

- No public hostname, no Traefik router, service or middleware labels — the
  container never appears in Traefik's routing table.
- No Docker host port binding, so nothing is listening on the host's interfaces.
- The container still joins `oduflow-{team_id}-net` under its ordinary name, so
  Odoo and sibling services reach it exactly as they reach any other service:

```
oduflow-1-svc-nats:4222
```

There is no `internal_port` argument, and there does not need to be: containers
on a Docker network talk to each other on whatever port the process inside is
listening on, which is fixed by the image and its configuration. NATS listens on
`4222` because the image says so; Oduflow neither needs to know that nor to
declare it.

`internal_only` is mutually exclusive with `port`, `routes`, `hostname`, and
`host_mode` — passing any of them alongside it is a validation error rather than
a silently ignored argument. (`host_mode` is excluded because a host-network
container is not resolvable by container name *and* binds the host's interfaces,
which is the opposite of what this mode is for.)

`get_service_info`, `list_services`, and the dashboard report the mode
explicitly, so an internal-only service is distinguishable from a published one
that is merely misconfigured.

#### Two independent defences

The container is kept out of Traefik twice over, on purpose:

1. Oduflow's Traefik runs with `--providers.docker.exposedbydefault=false`, so a
   container without router labels gets no router.
2. The container itself carries `traefik.enable=false` — the single `traefik.*`
   label an internal-only service ever has.

The first is a global setting living in *another* container: it is not
re-applied to a Traefik that predates it, and it does not hold at all if you
point Oduflow at a proxy you manage yourself. The second makes the refusal a
property of the service, so the guarantee travels with the container.

#### Switching an existing service

`update_service` moves a service between modes; the container is recreated, so
the old router labels and port bindings genuinely disappear:

```bash
# Withdraw a published service from public routing
oduflow call update_service '{"name":"nats","internal_only":true}'

# Publish it again — a new exposure is required in the same call
oduflow call update_service '{"name":"nats","internal_only":false,"port":8222}'
```

The mode is set when a service is created or restored; switching an existing
service is done through the MCP tool, CLI or REST API shown above rather than
from the dashboard, whose Update button only pulls a fresh image.

If you previously kept a service unreachable by pointing a dummy HTTP route at
it, replace that workaround with `internal_only=true`: the dummy route still
created a real Traefik router on a real public hostname, which internet scanners
find and probe. This mode removes the router entirely.

#### When the preset and the container disagree

The exposure mode is stored in two places — the service preset and a label on
the container — and `update_service` reads the preset. Preset writes are
best-effort, so the pair can drift apart (a failed write, a hand-edited
`service_presets.json`).

When they disagree and you have not said which one is right, `update_service`
refuses with a conflict error instead of guessing. Guessing either way would
silently change public exposure: re-publishing a service you had withdrawn, or
withdrawing one that is serving traffic. Repeat the call with an explicit
`internal_only=true` or `internal_only=false` — that resolves the ambiguity and
recreates the container in the mode you named.

### Traefik Certificate Store

When Oduflow terminates TLS through Traefik, every auxiliary service can read
Traefik's certificate store at `/etc/traefik/acme.json`. The mount is implicit:
do not add `oduflow-traefik-acme` to the `volumes` argument, and do not mount a
different volume at `/etc/traefik`. Oduflow always mounts the exact system
volume read-only; there is no wildcard allowance for other `oduflow-*` volumes.
The implicit mount is runtime configuration and is not saved in the service
preset.

This deliberately makes the shared certificate and private-key material
readable to every service container. Use only trusted service images and grant
service-management access only to trusted operators. Read-only protects the
store from modification, not from disclosure.

### Linux Capabilities

Two optional flags grant additional container privileges:

- `net_admin` — adds the `NET_ADMIN` Linux capability. Required for VPN / WireGuard, `tun`/`tap` devices, and `iptables` manipulation inside the container.
- `privileged` — runs the container in privileged mode (full host access, all capabilities). Use with care — only when a service genuinely needs it (e.g. Docker-in-Docker, hardware passthrough).

Both can be enabled at the same time; on the Docker side, `privileged` implies all capabilities so `net_admin` is then redundant — but it is still recorded in the preset so disabling `privileged` later keeps `NET_ADMIN` active.

## Managing Services

```bash
# List all services with status, ports, URLs, and env vars
oduflow call list_services

# Full state of a single service — image + digest, port/routes, hostname,
# host_mode, volumes, env vars, capabilities, restart count, started_at, preset
oduflow call get_service_info redis

# View service logs
oduflow call get_service_logs redis 200

# Restart a service
oduflow call restart_service redis

# Update a service (pull latest image, recreate container with same settings)
oduflow call update_service meilisearch

# Change environment variables on a running service (fully replaces existing env_vars)
oduflow call update_service meilisearch --env_vars "MEILI_MASTER_KEY=newkey,MEILI_ENV=production"

# Change the image (tag) of a running service
oduflow call update_service meilisearch --image getmeili/meilisearch:v1.8

# Toggle Linux capabilities / privileged mode on a running service (recreates it)
oduflow call update_service wireguard --net_admin true
oduflow call update_service wireguard --privileged true

# Delete a service
oduflow call delete_service redis

# Execute a command inside a service container
oduflow call run_service_command redis "redis-cli ping"
```

### Changing a Service

`update_service` is the preferred way to change **any** setting of a running service — image, env vars, port/routes, hostname, `host_mode`, `volumes`, `privileged`, or `net_admin`. It recreates the container automatically and preserves every setting you do not override, so you rarely need to delete and recreate a service by hand. Passing `routes` fully replaces the route list. To return to a single catch-all port, pass `routes=[]` and the replacement `port` in the same call.

If you do recreate a service manually (e.g. to rename it), call `get_service_info` first and reuse its fields in the new `create_service` call. The returned dict carries the full configuration (`image`, `port` or `routes`, `hostname`, `env_vars`, `host_mode`, `volumes`, `cap_add`, `privileged`) so you do not lose anything that `list_services` truncates or that lived only inside the preset.

## Service Update Flow

The `update_service` operation:

1. Reads the saved preset (authoritative source) or inspects the running container as a legacy fallback
2. Applies any overrides passed in (`env_vars`, `image`, `port`/`routes`, `hostname`, `host_mode`, `volumes`, `privileged`, `net_admin`) — each override **fully replaces** the current value
3. Resolves the complete candidate volume configuration before touching the running container; invalid or missing volumes fail without stopping it
4. Pulls the target image (the override, or the current one)
5. Decides whether to recreate:
    - If neither the image digest nor any setting changed → reports "already up-to-date"
    - If the image digest changed, any setting was overridden, or a legacy Traefik TLS service lacks the implicit ACME mount → stops the old container, removes it, and creates a new one with the merged settings
6. Updates the saved preset so subsequent `restore_service` calls use the new configuration

Overrides are optional: calling `update_service` with only `name` pulls the current image and recreates only when its digest changed or the implicit ACME mount is missing.

## Service Presets

Every time a service is created, its configuration (image, port or routes, hostname, environment variables, volumes, `host_mode`, `cap_add`, `privileged`) is automatically saved as a **preset** in `{team_data_dir}/service_presets.json`. This allows you to restore a service after deletion without re-entering its configuration.

```bash
# List saved presets
oduflow call list_service_presets

# Restore a previously deleted service
oduflow call restore_service redis

# Remove a saved preset
oduflow call delete_service_preset redis
```
