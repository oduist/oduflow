import io
import logging
import os
import tarfile
import time

import docker
from docker import DockerClient

from flow.docker_ops.client import get_client
from flow.errors import ExternalCommandError, NotFoundError, PrerequisiteNotMetError
from flow.settings import Settings

logger = logging.getLogger("flow")


def _ensure_traefik(client: DockerClient, settings: Settings) -> None:
    if settings.routing_mode != "traefik":
        return

    system_labels = {settings.managed_label: "true", settings.system_label: "true"}

    try:
        client.volumes.get(settings.traefik_acme_volume)
    except docker.errors.NotFound:
        client.volumes.create(settings.traefik_acme_volume, labels=system_labels)
        logger.info("Created volume %s", settings.traefik_acme_volume)

    try:
        t = client.containers.get(settings.traefik_container)
        if t.status != "running":
            t.start()
        return
    except docker.errors.NotFound:
        pass

    client.containers.run(
        "traefik:v3.4",
        name=settings.traefik_container,
        detach=True,
        network=settings.shared_network,
        ports={"80/tcp": 80, "443/tcp": 443},
        volumes={
            "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "ro"},
            settings.traefik_acme_volume: {"bind": "/acme", "mode": "rw"},
        },
        command=[
            "--providers.docker=true",
            "--providers.docker.exposedbydefault=false",
            f"--providers.docker.network={settings.shared_network}",
            "--entrypoints.web.address=:80",
            "--entrypoints.websecure.address=:443",
            "--entrypoints.web.http.redirections.entrypoint.to=websecure",
            "--entrypoints.web.http.redirections.entrypoint.scheme=https",
            "--certificatesresolvers.le.acme.httpchallenge=true",
            "--certificatesresolvers.le.acme.httpchallenge.entrypoint=web",
            f"--certificatesresolvers.le.acme.email={settings.acme_email}",
            "--certificatesresolvers.le.acme.storage=/acme/acme.json",
        ],
        labels=system_labels,
        restart_policy={"Name": "unless-stopped"},
    )
    logger.info("Created container %s", settings.traefik_container)


def _destroy_traefik(client: DockerClient, settings: Settings, removed: list[str]) -> None:
    try:
        t = client.containers.get(settings.traefik_container)
        t.stop()
        t.remove(v=True)
        removed.append(settings.traefik_container)
    except docker.errors.NotFound:
        pass

    try:
        v = client.volumes.get(settings.traefik_acme_volume)
        v.remove()
        removed.append(settings.traefik_acme_volume)
    except docker.errors.NotFound:
        pass


def _wait_pg_ready(client: DockerClient, settings: Settings, timeout: int = 30) -> None:
    container = client.containers.get(settings.shared_db_container)
    for i in range(timeout):
        exit_code, _ = container.exec_run(["pg_isready", "-U", settings.db_user])
        if exit_code == 0:
            exit_code2, _ = container.exec_run(
                ["psql", "-U", settings.db_user, "-d", "postgres", "-tAc", "SELECT 1;"]
            )
            if exit_code2 == 0:
                return
        time.sleep(1)
    raise PrerequisiteNotMetError(
        f"PostgreSQL did not become ready within {timeout}s"
    )


def _exec_sql(client: DockerClient, settings: Settings, sql: str, db: str = "postgres") -> str:
    container = client.containers.get(settings.shared_db_container)
    exit_code, output = container.exec_run(
        ["psql", "-U", settings.db_user, "-d", db, "-tAc", sql]
    )
    result = output.decode("utf-8") if isinstance(output, bytes) else str(output)
    if exit_code != 0:
        raise ExternalCommandError("psql", exit_code, result)
    return result.strip()


def _db_exists(client: DockerClient, settings: Settings, db_name: str) -> bool:
    result = _exec_sql(
        client,
        settings,
        f"SELECT 1 FROM pg_database WHERE datname='{db_name}';",
    )
    return result == "1"


def _is_text_dump(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            header = f.read(5)
        return header != b"PGDMP"
    except OSError:
        return False


def _copy_file_to_container(container: docker.models.containers.Container, src_path: str, dest_dir: str) -> None:
    with open(src_path, "rb") as f:
        data = f.read()
    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        info = tarfile.TarInfo(name=os.path.basename(src_path))
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    tar_stream.seek(0)
    container.put_archive(dest_dir, tar_stream)


def init_system(
    settings: Settings,
    dump_path: str | None = None,
    version: str = "15.0",
    force: bool = False,
) -> dict[str, str]:
    client = get_client()
    logger.info("Initializing system", extra={"version": version, "force": force})

    system_labels = {settings.managed_label: "true", settings.system_label: "true"}

    try:
        client.networks.get(settings.shared_network)
    except docker.errors.NotFound:
        client.networks.create(settings.shared_network, labels=system_labels)
        logger.info("Created network %s", settings.shared_network)

    _ensure_traefik(client, settings)

    try:
        client.volumes.get(settings.shared_db_volume)
    except docker.errors.NotFound:
        client.volumes.create(settings.shared_db_volume, labels=system_labels)
        logger.info("Created volume %s", settings.shared_db_volume)

    try:
        db_container = client.containers.get(settings.shared_db_container)
        if db_container.status != "running":
            db_container.start()
    except docker.errors.NotFound:
        client.containers.run(
            settings.postgres_image,
            name=settings.shared_db_container,
            detach=True,
            network=settings.shared_network,
            volumes={settings.shared_db_volume: {"bind": "/var/lib/postgresql/data", "mode": "rw"}},
            environment={
                "POSTGRES_USER": settings.db_user,
                "POSTGRES_PASSWORD": settings.db_password,
            },
            labels=system_labels,
            restart_policy={"Name": "unless-stopped"},
        )
        logger.info("Created container %s", settings.shared_db_container)

    _wait_pg_ready(client, settings)

    if _db_exists(client, settings, settings.template_db_name):
        if not force:
            return {"status": "already initialized", "template_db": settings.template_db_name}
        _exec_sql(
            client,
            settings,
            f"UPDATE pg_database SET datistemplate=false WHERE datname='{settings.template_db_name}';",
        )
        _exec_sql(client, settings, f"DROP DATABASE {settings.template_db_name};")

    resolved_dump = dump_path or settings.dump_file_path
    if not os.path.isfile(resolved_dump):
        raise NotFoundError(f"Dump file not found: {resolved_dump}")

    _exec_sql(client, settings, f"CREATE DATABASE {settings.template_db_name};")

    db_container = client.containers.get(settings.shared_db_container)
    tmp_name = os.path.basename(resolved_dump)

    _copy_file_to_container(db_container, resolved_dump, "/tmp")

    use_psql = resolved_dump.endswith(".sql") or _is_text_dump(resolved_dump)

    if use_psql:
        restore_cmd = ["psql", "-U", settings.db_user, "-d", settings.template_db_name, "-f", f"/tmp/{tmp_name}"]
    else:
        restore_cmd = ["pg_restore", "-U", settings.db_user, "-d", settings.template_db_name, f"/tmp/{tmp_name}"]

    logger.info("DB restore started, template_db=%s, dump=%s", settings.template_db_name, resolved_dump)
    restore_start = time.monotonic()

    exit_code, output = db_container.exec_run(restore_cmd)

    restore_elapsed = time.monotonic() - restore_start

    if exit_code != 0:
        logger.error("DB restore failed after %.1fs", restore_elapsed)
        msg = output.decode("utf-8") if isinstance(output, bytes) else str(output)
        cmd_name = "psql" if use_psql else "pg_restore"
        raise ExternalCommandError(cmd_name, exit_code, msg)

    logger.info("DB restore finished in %.1fs", restore_elapsed)

    _exec_sql(
        client,
        settings,
        f"UPDATE pg_database SET datistemplate=true WHERE datname='{settings.template_db_name}';",
    )

    logger.info("System initialized, template_db=%s, restore_time=%.1fs", settings.template_db_name, restore_elapsed)
    return {"status": "initialized", "template_db": settings.template_db_name, "restore_seconds": round(restore_elapsed, 1)}


def destroy_system(settings: Settings) -> dict[str, str]:
    client = get_client()
    logger.info("Destroying system")

    filters = {"label": [f"{settings.managed_label}=true"]}
    containers = client.containers.list(all=True, filters=filters)
    system_names = {settings.shared_db_container, settings.traefik_container}
    env_containers = [
        c for c in containers
        if c.labels.get(settings.branch_label) and c.name not in system_names
    ]
    if env_containers:
        names = [c.name for c in env_containers]
        from flow.errors import ConflictError
        raise ConflictError(
            f"Active environments exist: {', '.join(names)}. Delete them first."
        )

    removed: list[str] = []

    _destroy_traefik(client, settings, removed)

    try:
        db = client.containers.get(settings.shared_db_container)
        db.stop()
        db.remove(v=True)
        removed.append(settings.shared_db_container)
    except docker.errors.NotFound:
        pass

    try:
        vol = client.volumes.get(settings.shared_db_volume)
        vol.remove()
        removed.append(settings.shared_db_volume)
    except docker.errors.NotFound:
        pass

    try:
        net = client.networks.get(settings.shared_network)
        net.remove()
        removed.append(settings.shared_network)
    except docker.errors.NotFound:
        pass

    logger.info("System destroyed, removed=%s", removed)
    return {"status": "destroyed", "removed": ", ".join(removed)}
