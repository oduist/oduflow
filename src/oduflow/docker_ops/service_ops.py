from __future__ import annotations

import datetime
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
    cap_add: list[str] | None = None,
    privileged: bool = False,
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
        "oduflow.created_at": datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(),
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

    if privileged:
        run_kwargs["privileged"] = True
    elif cap_add:
        run_kwargs["cap_add"] = list(cap_add)

    logger.info("Pulling image %s for service %s", image, name)
    client.images.pull(image)

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
            cap_add=cap_add,
            privileged=privileged,
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


def _describe_service_container(
    settings: Settings, team: TeamSettings, container
) -> dict:
    """Extract a normalized state dict for a managed service container.

    Used by both ``list_services`` and ``get_service_info``.
    """
    svc_name = container.labels.get("oduflow.service")
    container_name = container.name
    image = container.image.tags[0] if container.image.tags else "unknown"
    status = container.status

    raw_env = container.attrs.get("Config", {}).get("Env", [])
    env_vars: dict[str, str] = {}
    for entry in raw_env:
        if "=" in entry:
            key, value = entry.split("=", 1)
            if key not in _SYSTEM_ENV_KEYS:
                env_vars[key] = value

    port_num: int | None = None
    url: str | None = None
    hostname: str | None = None
    is_host_mode = container.labels.get("oduflow.host_mode") == "true"

    if settings.routing_mode == "traefik":
        rule_label = f"traefik.http.routers.oduflow-svc-{svc_name}.rule"
        rule_value = container.labels.get(rule_label, "")
        match = re.search(r"Host\(`([^`]+)`\)", rule_value)
        if match:
            hostname = match.group(1)
            url = f"https://{hostname}"

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
            # In host mode without traefik, port is not mapped via Docker.
            # Try to get it from the preset.
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

    svc_volumes: list[dict[str, str]] = []
    mounts = container.attrs.get("Mounts", [])
    for mount in mounts:
        if mount.get("Type") == "volume":
            vol_docker_name = mount.get("Name", "")
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

    host_config = container.attrs.get("HostConfig", {}) or {}
    svc_cap_add = list(host_config.get("CapAdd") or [])
    svc_privileged = bool(host_config.get("Privileged", False))

    return {
        "name": svc_name,
        "container_name": container_name,
        "image": image,
        "status": status,
        "port": port_num,
        "hostname": hostname,
        "url": url,
        "env_vars": env_vars,
        "host_mode": is_host_mode,
        "volumes": svc_volumes,
        "cap_add": svc_cap_add,
        "privileged": svc_privileged,
        "created_at": container.labels.get("oduflow.created_at", "")
        or container.attrs.get("Created", ""),
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
        if not container.labels.get("oduflow.service"):
            continue
        result.append(_describe_service_container(settings, team, container))

    return result


def get_service_info(settings: Settings, team: TeamSettings, name: str) -> dict:
    """Return full state and configuration of a single managed service.

    Combines live container state (image, digest, ports, volumes, env, caps,
    runtime status) with preset presence flag. Raises :class:`NotFoundError`
    if the service container is missing.
    """
    client = get_client()
    container_name = f"oduflow-svc-{name}"

    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound:
        raise NotFoundError(f"Service '{name}' not found")

    if not container.labels.get("oduflow.service"):
        raise NotFoundError(f"Service '{name}' not found")

    info = _describe_service_container(settings, team, container)

    info["image_digest"] = container.image.id

    state = container.attrs.get("State", {}) or {}
    info["started_at"] = state.get("StartedAt")
    info["restart_count"] = int(container.attrs.get("RestartCount", 0) or 0)

    has_preset = False
    try:
        service_presets.get_preset(team, name)
        has_preset = True
    except NotFoundError:
        pass
    except Exception:
        logger.warning("Failed to check preset for service %s", name, exc_info=True)
    info["has_preset"] = has_preset

    return info


def update_service(
    settings: Settings,
    team: TeamSettings,
    name: str,
    *,
    env_override: dict[str, str] | None = None,
    image_override: str | None = None,
    port_override: int | None = None,
    hostname_override: str | None = None,
    host_mode_override: bool | None = None,
    volume_override: list[dict[str, str]] | None = None,
    cap_add_override: list[str] | None = None,
    privileged_override: bool | None = None,
) -> dict[str, str]:
    """Pull the latest image for a service and re-create it with the same settings.

    Optional overrides replace the corresponding setting from the saved preset.
    When any override differs from the current config the container is recreated
    even if the image digest has not changed.
    """
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

    # Read service options from saved preset (authoritative source).
    # Fall back to container inspection for legacy services without a preset.
    preset = None
    try:
        preset = service_presets.get_preset(team, name)
    except Exception:
        pass

    if preset:
        port = preset.get("port")
        hostname = preset.get("hostname") or None
        env_vars = preset.get("env_vars") or None
        is_host_mode = preset.get("host_mode", False)
        old_volumes = preset.get("volumes") or None
        cap_add = preset.get("cap_add") or None
        privileged = preset.get("privileged", False)
    else:
        # Legacy fallback: extract from running container
        raw_env = container.attrs.get("Config", {}).get("Env", [])
        env_vars: dict[str, str] = {}
        for entry in raw_env:
            if "=" in entry:
                key, value = entry.split("=", 1)
                if key not in _SYSTEM_ENV_KEYS:
                    env_vars[key] = value

        is_host_mode = container.labels.get("oduflow.host_mode") == "true"

        host_config = container.attrs.get("HostConfig", {}) or {}
        cap_add = list(host_config.get("CapAdd") or []) or None
        privileged = bool(host_config.get("Privileged", False))

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
                pass  # Cannot determine port without preset in host mode
            else:
                ports_dict = container.attrs.get("NetworkSettings", {}).get("Ports", {})
                if ports_dict:
                    for port_key in ports_dict:
                        port_match = re.match(r"(\d+)/", port_key)
                        if port_match:
                            port = int(port_match.group(1))
                            break

        old_volumes = None
        mounts_data = container.attrs.get("Mounts", [])
        vols: list[dict[str, str]] = []
        for mount in mounts_data:
            if mount.get("Type") == "volume":
                vol_docker_name = mount.get("Name", "")
                prefix = f"oduflow-vol-{team.team_id}-"
                if vol_docker_name.startswith(prefix):
                    short_name = vol_docker_name[len(prefix):]
                    vols.append(
                        {
                            "volume": short_name,
                            "mount_path": mount.get("Destination", ""),
                            "mode": "ro" if not mount.get("RW", True) else "rw",
                        }
                    )
        if vols:
            old_volumes = vols

        env_vars = env_vars or None

    if port is None and port_override is None:
        raise NotFoundError(f"Cannot determine port for service '{name}'.")

    # Apply overrides and track whether config changed
    config_changed = False
    if env_override is not None and env_override != (env_vars or {}):
        env_vars = env_override or None
        config_changed = True
    if port_override is not None and port_override != port:
        port = port_override
        config_changed = True
    if hostname_override is not None and hostname_override != hostname:
        hostname = hostname_override or None
        config_changed = True
    if host_mode_override is not None and host_mode_override != is_host_mode:
        is_host_mode = host_mode_override
        config_changed = True
    if volume_override is not None and volume_override != (old_volumes or []):
        old_volumes = volume_override or None
        config_changed = True
    if cap_add_override is not None and cap_add_override != (cap_add or []):
        cap_add = cap_add_override or None
        config_changed = True
    if privileged_override is not None and privileged_override != privileged:
        privileged = privileged_override
        config_changed = True

    # Determine the image to pull (override or current)
    target_image = image_override if image_override else old_image
    if image_override and image_override != old_image:
        config_changed = True

    # Capture old image digest
    old_digest = container.image.id  # e.g. sha256:abc...

    # Pull the latest image
    logger.info("Pulling latest image %s for service %s", target_image, name)
    new_image_obj = client.images.pull(target_image)
    new_digest = new_image_obj.id
    image_updated = old_digest != new_digest

    needs_recreate = image_updated or config_changed

    if not needs_recreate:
        logger.info("No changes for service %s: %s", name, new_digest[:19])
        # Compute URL for return
        if settings.routing_mode == "traefik":
            h = hostname or f"{name}.{team.hostname}"
            if "." not in h:
                h = f"{h}.{team.hostname}"
            url = f"https://{h}"
        else:
            url = f"http://{team.hostname}:{port}"
        return {
            "name": name,
            "container_name": container_name,
            "image": target_image,
            "url": url,
            "image_updated": False,
            "config_updated": False,
            "old_digest": old_digest,
            "new_digest": new_digest,
        }

    logger.info(
        "Recreating service %s (image_updated=%s, config_changed=%s)",
        name,
        image_updated,
        config_changed,
    )

    # Stop and remove the old container
    container.stop()
    container.remove(v=True)
    logger.info("Removed old container %s for update", container_name)

    # Re-create with the (possibly overridden) settings
    result = create_service(
        settings,
        team,
        name=name,
        image=target_image,
        port=port,
        hostname=hostname,
        env_vars=env_vars or None,
        host_mode=is_host_mode,
        volumes=old_volumes or None,
        cap_add=cap_add,
        privileged=privileged,
    )

    result["image_updated"] = image_updated
    result["config_updated"] = config_changed
    result["old_digest"] = old_digest
    result["new_digest"] = new_digest

    logger.info("Service %s updated with image %s", name, target_image)
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
