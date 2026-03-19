from __future__ import annotations

import logging
import re

import docker

from oduflow.docker_ops.client import get_client
from oduflow.docker_ops import service_presets
from oduflow.docker_ops import volume_ops
from oduflow.errors import ConflictError, NotFoundError, PrerequisiteNotMetError
from oduflow.settings import Settings, TeamSettings

logger = logging.getLogger("oduflow")

_SYSTEM_ENV_KEYS = {
    "PATH",
    "HOME",
    "HOSTNAME",
    "TERM",
    "LANG",
    "LC_ALL",
    "GOPATH",
    "JAVA_HOME",
    "SHLVL",
    "PWD",
    "OLDPWD",
    "SHELL",
    "USER",
    "LOGNAME",
    "MAIL",
    "EDITOR",
    "VISUAL",
    "PAGER",
    "LESS",
    "LESSOPEN",
    "LESSCLOSE",
    "LS_COLORS",
    "XDG_RUNTIME_DIR",
    "XDG_DATA_DIRS",
    "XDG_CONFIG_DIRS",
    "XDG_CACHE_HOME",
    "DISPLAY",
    "GPG_AGENT_INFO",
    "SSH_AUTH_SOCK",
    "SSH_AGENT_PID",
    "DBUS_SESSION_BUS_ADDRESS",
}


def create_service(
    settings: Settings,
    team: TeamSettings,
    name: str,
    image: str,
    port: int,
    hostname: str | None = None,
    env_vars: dict[str, str] | None = None,
    host_mode: bool = False,
    volumes: list[dict[str, str]] | None = None,
) -> dict[str, str]:
    client = get_client()
    container_name = f"oduflow-svc-{name}"

    # Check that the shared network exists (not needed for host mode)
    if not host_mode:
        try:
            client.networks.get(settings.shared_network)
        except docker.errors.NotFound:
            raise PrerequisiteNotMetError(
                f"Shared network '{settings.shared_network}' not found. "
                "System not initialized. Restart oduflow."
            )

    # Check for existing container
    try:
        existing = client.containers.get(container_name)
        if existing.status == "running":
            raise ConflictError(f"Service '{name}' already exists and is running.")
    except docker.errors.NotFound:
        pass

    labels = {
        "oduflow.managed": "true",
        "oduflow.team": team.team_id,
        "oduflow.service": name,
    }

    run_kwargs: dict = {
        "image": image,
        "name": container_name,
        "detach": True,
        "labels": labels,
        "restart_policy": {"Name": "unless-stopped"},
    }

    if host_mode:
        run_kwargs["network_mode"] = "host"
        labels["oduflow.host_mode"] = "true"
    else:
        run_kwargs["network"] = settings.shared_network

    if settings.routing_mode == "traefik":
        if not hostname:
            hostname = f"{name}.{team.hostname}"
        elif "." not in hostname:
            hostname = f"{hostname}.{team.hostname}"
        labels["traefik.enable"] = "true"
        labels[f"traefik.http.routers.{container_name}.rule"] = f"Host(`{hostname}`)"
        labels[f"traefik.http.routers.{container_name}.entrypoints"] = "websecure"
        labels[f"traefik.http.routers.{container_name}.tls.certresolver"] = (
            "letsencrypt"
        )
        if host_mode:
            labels[
                f"traefik.http.services.{container_name}.loadbalancer.server.url"
            ] = f"http://host.docker.internal:{port}"
        else:
            labels[
                f"traefik.http.services.{container_name}.loadbalancer.server.port"
            ] = str(port)
        url = f"https://{hostname}"
    else:
        if not host_mode:
            run_kwargs["ports"] = {f"{port}/tcp": port}
        url = f"http://{team.hostname}:{port}"

    if env_vars:
        run_kwargs["environment"] = env_vars

    if volumes:
        vol_binds = volume_ops.resolve_volume_binds(team, volumes)
        if vol_binds:
            run_kwargs["volumes"] = vol_binds

    client.containers.run(**run_kwargs)
    logger.info("Created service container %s from image %s", container_name, image)

    # Auto-save preset for future restore
    try:
        service_presets.save_preset(
            team,
            name,
            image,
            port,
            hostname=hostname,
            env_vars=env_vars,
            base_hostname=team.hostname,
            host_mode=host_mode,
            volumes=volumes,
        )
    except Exception:
        logger.warning("Failed to save service preset for %s", name, exc_info=True)

    return {
        "name": name,
        "container_name": container_name,
        "image": image,
        "url": url,
    }


def restart_service(settings: Settings, name: str) -> dict[str, str]:
    client = get_client()
    container_name = f"oduflow-svc-{name}"

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Service '{name}' not found")

    container.restart()
    logger.info("Restarted service container %s", container_name)

    return {
        "name": name,
        "container_name": container_name,
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


def list_services(settings: Settings, team: TeamSettings) -> list[dict]:
    client = get_client()
    containers = client.containers.list(
        all=True,
        filters={
            "label": [
                f"{settings.managed_label}=true",
                f"{settings.team_label}={team.team_id}",
            ]
        },
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
        is_host_mode = container.labels.get("oduflow.host_mode") == "true"

        if settings.routing_mode == "traefik":
            rule_label = f"traefik.http.routers.oduflow-svc-{svc_name}.rule"

            rule_value = container.labels.get(rule_label, "")
            match = re.search(r"Host\(`([^`]+)`\)", rule_value)
            if match:
                traefik_hostname = match.group(1)
                url = f"https://{traefik_hostname}"

            if is_host_mode:
                url_label = f"traefik.http.services.oduflow-svc-{svc_name}.loadbalancer.server.url"
                url_value = container.labels.get(url_label, "")
                port_match = re.search(r":(\d+)$", url_value)
                if port_match:
                    port_num = int(port_match.group(1))
            else:
                port_label = f"traefik.http.services.oduflow-svc-{svc_name}.loadbalancer.server.port"
                label_port = container.labels.get(port_label)
                if label_port:
                    port_num = int(label_port)
        else:
            if is_host_mode:
                # In host mode without traefik, port is not mapped via Docker
                # Try to get it from the preset
                try:
                    preset = service_presets.get_preset(team, svc_name)
                    port_num = preset.get("port")
                except Exception:
                    pass
                if port_num:
                    url = f"http://{team.hostname}:{port_num}"
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
                                    url = f"http://{team.hostname}:{host_port}"
                                    break
                        break  # only process first port entry

        # Extract volume mounts
        svc_volumes: list[dict[str, str]] = []
        mounts = container.attrs.get("Mounts", [])
        for mount in mounts:
            if mount.get("Type") == "volume":
                vol_docker_name = mount.get("Name", "")
                # Extract short name from oduflow-vol-{team}-{name}
                prefix = f"oduflow-vol-{team.team_id}-"
                short_name = (
                    vol_docker_name[len(prefix) :]
                    if vol_docker_name.startswith(prefix)
                    else vol_docker_name
                )
                svc_volumes.append(
                    {
                        "volume": short_name,
                        "mount_path": mount.get("Destination", ""),
                        "mode": "ro" if not mount.get("RW", True) else "rw",
                    }
                )

        result.append(
            {
                "name": svc_name,
                "container_name": container_name,
                "image": image,
                "status": status,
                "port": port_num,
                "url": url,
                "env_vars": env_vars,
                "host_mode": is_host_mode,
                "volumes": svc_volumes,
            }
        )

    return result


def update_service(settings: Settings, team: TeamSettings, name: str) -> dict[str, str]:
    """Pull the latest image for a service and re-create it with the same settings."""
    client = get_client()
    container_name = f"oduflow-svc-{name}"

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Service '{name}' not found")

    # Capture current image name
    old_image = container.image.tags[0] if container.image.tags else None
    if not old_image:
        # Fall back to Config.Image (may be a digest reference)
        old_image = container.attrs.get("Config", {}).get("Image")
    if not old_image:
        raise NotFoundError(
            f"Cannot determine image for service '{name}'. "
            "The container has no image tag or Config.Image."
        )

    # Capture current environment variables (filtering system keys)
    raw_env = container.attrs.get("Config", {}).get("Env", [])
    env_vars: dict[str, str] = {}
    for entry in raw_env:
        if "=" in entry:
            key, value = entry.split("=", 1)
            if key not in _SYSTEM_ENV_KEYS:
                env_vars[key] = value

    # Detect host mode
    is_host_mode = container.labels.get("oduflow.host_mode") == "true"

    # Capture port and hostname depending on routing mode
    port: int | None = None
    hostname: str | None = None

    if settings.routing_mode == "traefik":
        rule_label = f"traefik.http.routers.{container_name}.rule"

        rule_value = container.labels.get(rule_label, "")
        match = re.search(r"Host\(`([^`]+)`\)", rule_value)
        if match:
            hostname = match.group(1)

        if is_host_mode:
            url_label = (
                f"traefik.http.services.{container_name}.loadbalancer.server.url"
            )
            url_value = container.labels.get(url_label, "")
            port_match = re.search(r":(\d+)$", url_value)
            if port_match:
                port = int(port_match.group(1))
        else:
            port_label = (
                f"traefik.http.services.{container_name}.loadbalancer.server.port"
            )
            label_port = container.labels.get(port_label)
            if label_port:
                port = int(label_port)
    else:
        if is_host_mode:
            try:
                preset = service_presets.get_preset(team, name)
                port = preset.get("port")
            except Exception:
                pass
        else:
            ports_dict = container.attrs.get("NetworkSettings", {}).get("Ports", {})
            if ports_dict:
                for port_key in ports_dict:
                    port_match = re.match(r"(\d+)/", port_key)
                    if port_match:
                        port = int(port_match.group(1))
                        break

    if port is None:
        raise NotFoundError(f"Cannot determine port for service '{name}'.")

    # Capture volume mounts from old container
    old_volumes: list[dict[str, str]] = []
    mounts = container.attrs.get("Mounts", [])
    for mount in mounts:
        if mount.get("Type") == "volume":
            vol_docker_name = mount.get("Name", "")
            prefix = f"oduflow-vol-{team.team_id}-"
            if vol_docker_name.startswith(prefix):
                short_name = vol_docker_name[len(prefix) :]
                old_volumes.append(
                    {
                        "volume": short_name,
                        "mount_path": mount.get("Destination", ""),
                        "mode": "ro" if not mount.get("RW", True) else "rw",
                    }
                )

    # Capture old image digest
    old_digest = container.image.id  # e.g. sha256:abc...

    # Pull the latest image
    logger.info("Pulling latest image %s for service %s", old_image, name)
    new_image_obj = client.images.pull(old_image)
    new_digest = new_image_obj.id
    image_updated = old_digest != new_digest

    if not image_updated:
        logger.info("Image unchanged for service %s: %s", name, new_digest[:19])
        return {
            "name": name,
            "container_name": container_name,
            "image": old_image,
            "image_updated": False,
            "old_digest": old_digest,
            "new_digest": new_digest,
        }

    logger.info(
        "Image changed for service %s: %s -> %s", name, old_digest[:19], new_digest[:19]
    )

    # Stop and remove the old container
    container.stop()
    container.remove(v=True)
    logger.info("Removed old container %s for update", container_name)

    # Re-create with the same settings
    result = create_service(
        settings,
        team,
        name=name,
        image=old_image,
        port=port,
        hostname=hostname,
        env_vars=env_vars or None,
        host_mode=is_host_mode,
        volumes=old_volumes or None,
    )

    result["image_updated"] = True
    result["old_digest"] = old_digest
    result["new_digest"] = new_digest

    logger.info("Service %s updated with image %s", name, old_image)
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


def run_command_in_service(
    settings: Settings, name: str, command: str, user: str = "root"
) -> dict[str, object]:
    client = get_client()
    container_name = f"oduflow-svc-{name}"

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Service '{name}' not found")

    logger.info(
        "Executing command in service",
        extra={"service": name, "command": command, "user": user},
    )
    exit_code, output = container.exec_run(command, user=user)
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)

    return {
        "exit_code": exit_code,
        "output": output_str,
    }
