"""Managed local NATS/JetStream runtime used by the operation queue."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path

import docker
from docker import DockerClient
from oduflow.errors import PrerequisiteNotMetError
from oduflow.settings import Settings

logger = logging.getLogger("oduflow")

_SECRETS_FILE = "nats-secrets.json"
_CONFIG_FILE = "nats.conf"
_CONFIG_HASH_LABEL = "oduflow.nats.config-hash"


@dataclass(frozen=True)
class NatsCredentials:
    username: str
    password: str


def _private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _load_or_create_secrets(settings: Settings) -> tuple[NatsCredentials, str]:
    path = Path(settings.etc_dir) / _SECRETS_FILE
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            password = str(raw["password"])
            encryption_key = str(raw["encryption_key"])
            if password and encryption_key:
                os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
                return NatsCredentials("oduflow", password), encryption_key
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PrerequisiteNotMetError(
                f"Cannot read managed NATS secrets at {path}: {exc}"
            ) from exc

    password = secrets.token_urlsafe(32)
    encryption_key = secrets.token_urlsafe(32)
    _private_write(
        path,
        json.dumps(
            {"password": password, "encryption_key": encryption_key},
            indent=2,
        )
        + "\n",
    )
    return NatsCredentials("oduflow", password), encryption_key


def _render_config(credentials: NatsCredentials, encryption_key: str) -> str:
    # JSON string encoding is valid for quoted NATS config values and avoids
    # hand-written escaping of generated secrets.
    password = json.dumps(credentials.password)
    key = json.dumps(encryption_key)
    return (
        "server_name: oduflow\n"
        "port: 4222\n"
        "jetstream {\n"
        '  store_dir: "/data/jetstream"\n'
        '  sync_interval: "1s"\n'
        "  cipher: chachapoly\n"
        f"  key: {key}\n"
        "}\n"
        "authorization {\n"
        f"  user: {json.dumps(credentials.username)}\n"
        f"  password: {password}\n"
        "}\n"
    )


def _wait_ready(container: object, container_name: str, timeout: float = 15) -> None:
    """Wait until NATS has parsed config and opened JetStream."""
    deadline = time.monotonic() + timeout
    last_logs = ""
    while time.monotonic() < deadline:
        container.reload()  # type: ignore[attr-defined]
        status = str(container.status)  # type: ignore[attr-defined]
        try:
            raw_logs = container.logs(tail=100)  # type: ignore[attr-defined]
            last_logs = (
                raw_logs.decode("utf-8", errors="replace")
                if isinstance(raw_logs, bytes)
                else str(raw_logs)
            )
        except Exception:
            last_logs = ""
        if status == "running" and "Server is ready" in last_logs:
            return
        if status in {"exited", "dead"}:
            break
        time.sleep(0.1)
    detail = last_logs[-500:].strip()
    suffix = f" Last logs: {detail}" if detail else ""
    raise PrerequisiteNotMetError(
        f"Managed NATS container '{container_name}' did not become ready.{suffix}"
    )


def ensure_nats(
    client: DockerClient, settings: Settings, system_labels: dict[str, str]
) -> NatsCredentials:
    """Create or reconcile the managed single-node NATS container."""
    credentials, encryption_key = _load_or_create_secrets(settings)
    config = _render_config(credentials, encryption_key)
    config_path = Path(settings.etc_dir) / _CONFIG_FILE
    current = ""
    try:
        current = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        pass
    if current != config:
        _private_write(config_path, config)
    else:
        os.chmod(config_path, stat.S_IRUSR | stat.S_IWUSR)
    config_mount_path = str(config_path.resolve())

    runtime_fingerprint = json.dumps(
        {
            "config": config,
            "image": settings.nats_image,
            "network": settings.shared_network,
            "port": settings.nats_port,
            "volume": settings.nats_volume,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    config_hash = hashlib.sha256(runtime_fingerprint.encode("utf-8")).hexdigest()

    try:
        client.volumes.get(settings.nats_volume)
    except docker.errors.NotFound:
        client.volumes.create(settings.nats_volume, labels=system_labels)
        logger.info("Created volume %s", settings.nats_volume)

    recreate = False
    try:
        container = client.containers.get(settings.nats_container)
        recreate = container.labels.get(_CONFIG_HASH_LABEL) != config_hash or bool(
            container.image.tags and settings.nats_image not in container.image.tags
        )
        if recreate:
            container.remove(force=True)
        elif container.status != "running":
            container.start()
            container.reload()
        if not recreate:
            _wait_ready(container, settings.nats_container)
            return credentials
    except docker.errors.NotFound:
        pass

    try:
        client.images.pull(settings.nats_image)
        container = client.containers.run(
            settings.nats_image,
            name=settings.nats_container,
            detach=True,
            network=settings.shared_network,
            volumes={
                settings.nats_volume: {"bind": "/data", "mode": "rw"},
                config_mount_path: {"bind": "/etc/nats/nats.conf", "mode": "ro"},
            },
            command=["-c", "/etc/nats/nats.conf"],
            ports={"4222/tcp": ("127.0.0.1", settings.nats_port)},
            labels={**system_labels, _CONFIG_HASH_LABEL: config_hash},
            restart_policy={"Name": "unless-stopped"},
        )
        _wait_ready(container, settings.nats_container)
    except docker.errors.DockerException as exc:
        raise PrerequisiteNotMetError(
            f"Cannot start managed NATS container '{settings.nats_container}': {exc}"
        ) from exc
    logger.info("Created container %s", settings.nats_container)
    return credentials


def load_credentials(settings: Settings) -> NatsCredentials:
    credentials, _encryption_key = _load_or_create_secrets(settings)
    return credentials


def connection_urls(settings: Settings) -> list[str]:
    """Addresses for host and Docker-out-of-Docker deployments."""
    explicit = os.getenv("ODUFLOW_NATS_URL", "").strip()
    if explicit:
        return [explicit]
    return [
        f"nats://127.0.0.1:{settings.nats_port}",
        f"nats://{settings.nats_container}:4222",
    ]
