import logging
import re

import docker

from oduflow.docker_ops.client import get_client
from oduflow.errors import ConflictError, NotFoundError, PrerequisiteNotMetError
from oduflow.settings import Settings

logger = logging.getLogger("oduflow")

_SYSTEM_ENV_KEYS = {
    "PATH", "HOME", "HOSTNAME", "TERM", "LANG", "LC_ALL",
    "GOPATH", "JAVA_HOME", "SHLVL", "PWD", "OLDPWD", "SHELL",
    "USER", "LOGNAME", "MAIL", "EDITOR", "VISUAL", "PAGER",
    "LESS", "LESSOPEN", "LESSCLOSE", "LS_COLORS",
    "XDG_RUNTIME_DIR", "XDG_DATA_DIRS", "XDG_CONFIG_DIRS", "XDG_CACHE_HOME",
    "DISPLAY", "GPG_AGENT_INFO", "SSH_AUTH_SOCK", "SSH_AGENT_PID",
    "DBUS_SESSION_BUS_ADDRESS",
}


def create_service(
    settings: Settings,
    name: str,
    image: str,
    port: int,
    hostname: str | None = None,
    env_vars: dict[str, str] | None = None,
) -> dict[str, str]:
    client = get_client()
    container_name = f"oduflow-svc-{name}"

    # Check that the shared network exists
    try:
        client.networks.get(settings.shared_network)
    except docker.errors.NotFound:
        raise PrerequisiteNotMetError(
            f"Shared network '{settings.shared_network}' not found. "
            "Run init_system first."
        )

    # Check for existing container
    try:
        existing = client.containers.get(container_name)
        if existing.status == "running":
            raise ConflictError(
                f"Service '{name}' already exists and is running."
            )
    except docker.errors.NotFound:
        pass

    labels = {
        "oduflow.managed": "true",
        "oduflow.service": name,
    }

    run_kwargs: dict = {
        "image": image,
        "name": container_name,
        "detach": True,
        "network": settings.shared_network,
        "labels": labels,
        "restart_policy": {"Name": "unless-stopped"},
    }

    if settings.routing_mode == "traefik":
        if not hostname:
            hostname = f"{name}.{settings.base_domain}"
        labels["traefik.enable"] = "true"
        labels[f"traefik.http.routers.{container_name}.rule"] = f"Host(`{hostname}`)"
        labels[f"traefik.http.routers.{container_name}.entrypoints"] = "websecure"
        labels[f"traefik.http.routers.{container_name}.tls.certresolver"] = "le"
        labels[f"traefik.http.services.{container_name}.loadbalancer.server.port"] = str(port)
        url = f"https://{hostname}"
    else:
        run_kwargs["ports"] = {f"{port}/tcp": port}
        url = f"http://{settings.external_host}:{port}"

    if env_vars:
        run_kwargs["environment"] = env_vars

    client.containers.run(**run_kwargs)
    logger.info("Created service container %s from image %s", container_name, image)

    return {
        "name": name,
        "container_name": container_name,
        "image": image,
        "url": url,
    }


def delete_service(settings: Settings, name: str) -> dict[str, str]:
    client = get_client()
    container_name = f"oduflow-svc-{name}"

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Service '{name}' not found")

    container.stop()
    container.remove(v=True)
    logger.info("Deleted service container %s", container_name)

    return {
        "name": name,
        "container_name": container_name,
    }


def list_services(settings: Settings) -> list[dict]:
    client = get_client()
    containers = client.containers.list(
        all=True,
        filters={"label": [f"{settings.managed_label}=true"]},
    )

    result = []
    for container in containers:
        svc_name = container.labels.get("oduflow.service")
        if not svc_name:
            continue

        container_name = container.name
        image = container.image.tags[0] if container.image.tags else "unknown"
        status = container.status

        # Parse environment variables, filtering out system keys
        raw_env = container.attrs.get("Config", {}).get("Env", [])
        env_vars = {}
        for entry in raw_env:
            if "=" in entry:
                key, value = entry.split("=", 1)
                if key not in _SYSTEM_ENV_KEYS:
                    env_vars[key] = value

        # Extract port and URL
        port_num = None
        url = None

        if settings.routing_mode == "traefik":
            rule_label = f"traefik.http.routers.oduflow-svc-{svc_name}.rule"
            port_label = f"traefik.http.services.oduflow-svc-{svc_name}.loadbalancer.server.port"

            rule_value = container.labels.get(rule_label, "")
            match = re.search(r"Host\(`([^`]+)`\)", rule_value)
            if match:
                traefik_hostname = match.group(1)
                url = f"https://{traefik_hostname}"

            label_port = container.labels.get(port_label)
            if label_port:
                port_num = int(label_port)
        else:
            ports_dict = container.attrs.get("NetworkSettings", {}).get("Ports", {})
            if ports_dict:
                for port_key, mappings in ports_dict.items():
                    # Extract port number from key like "6379/tcp"
                    port_match = re.match(r"(\d+)/", port_key)
                    if port_match:
                        port_num = int(port_match.group(1))
                    if mappings:
                        for mapping in mappings:
                            host_port = mapping.get("HostPort")
                            if host_port:
                                url = f"http://{settings.external_host}:{host_port}"
                                break
                    break  # only process first port entry

        result.append({
            "name": svc_name,
            "container_name": container_name,
            "image": image,
            "status": status,
            "port": port_num,
            "url": url,
            "env_vars": env_vars,
        })

    return result


def get_service_logs(settings: Settings, name: str, lines: int = 100) -> str:
    client = get_client()
    container_name = f"oduflow-svc-{name}"

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Service '{name}' not found")

    output = container.logs(tail=lines, timestamps=True)
    return output.decode("utf-8")
