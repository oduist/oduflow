import io
import logging
import os
import pathlib
import shutil
import subprocess
import tarfile
import time

import docker
from docker import DockerClient

from flow.docker_ops.client import get_client, get_odoo_uid_gid
from flow.errors import ConflictError, ExternalCommandError, NotFoundError, PrerequisiteNotMetError
from flow.settings import Settings

logger = logging.getLogger("flow")

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
_PG_CONF_TEMPLATE = _PROJECT_ROOT / "templates" / "postgresql.conf"
_ODOO_CONF_TEMPLATE = _PROJECT_ROOT / "templates" / "odoo.conf"


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

    dynamic_cfg = (
        "http:\n"
        "  routers:\n"
        "    flow-server:\n"
        "      rule: \"Host(`{host}`)\"\n"
        "      entryPoints: [websecure]\n"
        "      service: flow-server\n"
        "      tls:\n"
        "        certResolver: le\n"
        "  services:\n"
        "    flow-server:\n"
        "      loadBalancer:\n"
        "        servers:\n"
        "          - url: \"http://host.docker.internal:{port}\"\n"
    ).format(host=settings.base_domain, port=settings.flow_server_port)

    import tempfile
    dynamic_file = os.path.join(
        tempfile.gettempdir(), "flow-traefik-dynamic.yml"
    )
    with open(dynamic_file, "w") as f:
        f.write(dynamic_cfg)

    client.containers.run(
        "traefik:v3.6",
        name=settings.traefik_container,
        detach=True,
        network=settings.shared_network,
        ports={"80/tcp": 80, "443/tcp": 443},
        extra_hosts={"host.docker.internal": "host-gateway"},
        volumes={
            "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "ro"},
            settings.traefik_acme_volume: {"bind": "/acme", "mode": "rw"},
            dynamic_file: {"bind": "/etc/traefik/dynamic.yml", "mode": "ro"},
        },
        command=[
            "--log.level=INFO",
            "--providers.docker=true",
            "--providers.docker.exposedbydefault=false",
            f"--providers.docker.network={settings.shared_network}",
            "--providers.file.filename=/etc/traefik/dynamic.yml",
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
            volumes={
                settings.shared_db_volume: {"bind": "/var/lib/postgresql/data", "mode": "rw"},
                str(_PG_CONF_TEMPLATE): {"bind": "/etc/postgresql/postgresql.conf", "mode": "ro"},
            },
            command=["postgres", "-c", "config_file=/etc/postgresql/postgresql.conf"],
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
        _exec_sql(client, settings, f"DROP DATABASE {settings.template_db_name} WITH (FORCE);")

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


def reload_template_db(
    settings: Settings,
    dump_path: str | None = None,
) -> dict[str, str]:
    client = get_client()
    resolved_dump = dump_path or settings.dump_file_path

    if not os.path.isfile(resolved_dump):
        raise NotFoundError(f"Dump file not found: {resolved_dump}")

    _wait_pg_ready(client, settings)

    if _db_exists(client, settings, settings.template_db_name):
        _exec_sql(
            client,
            settings,
            f"UPDATE pg_database SET datistemplate=false WHERE datname='{settings.template_db_name}';",
        )
        _exec_sql(client, settings, f"DROP DATABASE {settings.template_db_name} WITH (FORCE);")
        logger.info("Dropped template DB %s", settings.template_db_name)

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

    logger.info("Template DB reloaded, template_db=%s, restore_time=%.1fs", settings.template_db_name, restore_elapsed)
    return {"status": "reloaded", "template_db": settings.template_db_name, "restore_seconds": round(restore_elapsed, 1)}


def generate_ref(
    settings: Settings,
    odoo_image: str = "odoo:17.0",
    modules: str = "base",
) -> dict[str, str]:
    client = get_client()
    logger.info("Generating reference dump from clean Odoo", extra={"image": odoo_image, "modules": modules})

    system_labels = {settings.managed_label: "true", settings.system_label: "true"}

    try:
        client.networks.get(settings.shared_network)
    except docker.errors.NotFound:
        client.networks.create(settings.shared_network, labels=system_labels)
        logger.info("Created network %s", settings.shared_network)

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
            volumes={
                settings.shared_db_volume: {"bind": "/var/lib/postgresql/data", "mode": "rw"},
                str(_PG_CONF_TEMPLATE): {"bind": "/etc/postgresql/postgresql.conf", "mode": "ro"},
            },
            command=["postgres", "-c", "config_file=/etc/postgresql/postgresql.conf"],
            environment={
                "POSTGRES_USER": settings.db_user,
                "POSTGRES_PASSWORD": settings.db_password,
            },
            labels=system_labels,
            restart_policy={"Name": "unless-stopped"},
        )
        logger.info("Created container %s", settings.shared_db_container)

    _wait_pg_ready(client, settings)

    build_db = "odoo_ref_build"
    temp_container_name = "flow-ref-builder"

    if _db_exists(client, settings, build_db):
        _exec_sql(client, settings, f"DROP DATABASE {build_db} WITH (FORCE);")

    _exec_sql(client, settings, f"CREATE DATABASE {build_db};")
    logger.info("Created temporary database %s", build_db)

    try:
        old = client.containers.get(temp_container_name)
        old.remove(force=True)
    except docker.errors.NotFound:
        pass

    logger.info("Starting Odoo container for base init (image=%s, modules=%s)", odoo_image, modules)
    init_start = time.monotonic()

    volumes = {}
    if _ODOO_CONF_TEMPLATE.exists():
        volumes[str(_ODOO_CONF_TEMPLATE)] = {"bind": "/etc/odoo/odoo.conf", "mode": "ro"}

    temp_container = client.containers.run(
        odoo_image,
        name=temp_container_name,
        detach=True,
        network=settings.shared_network,
        environment={
            "HOST": settings.shared_db_container,
            "USER": settings.db_user,
            "PASSWORD": settings.db_password,
        },
        volumes=volumes,
        command=f"odoo -d {build_db} -i {modules} --stop-after-init --without-demo=all",
        labels={settings.managed_label: "true"},
    )

    exit_info = temp_container.wait(timeout=600)
    init_elapsed = time.monotonic() - init_start
    exit_code = exit_info.get("StatusCode", -1)

    if exit_code != 0:
        logs = temp_container.logs(tail=50).decode("utf-8", errors="replace")
        temp_container.remove(v=True)
        _exec_sql(client, settings, f"DROP DATABASE IF EXISTS {build_db} WITH (FORCE);")
        raise ExternalCommandError(
            "odoo --stop-after-init", exit_code,
            f"Odoo init failed after {init_elapsed:.1f}s.\nLast logs:\n{logs}",
        )

    logger.info("Odoo init completed in %.1fs", init_elapsed)

    dump_path = settings.dump_file_path
    os.makedirs(os.path.dirname(dump_path), exist_ok=True)

    db_container = client.containers.get(settings.shared_db_container)
    dump_cmd = [
        "pg_dump", "-U", settings.db_user, "-Fc", "-f", f"/tmp/{build_db}.dump", build_db,
    ]
    exit_code_dump, output_dump = db_container.exec_run(dump_cmd)
    if exit_code_dump != 0:
        msg = output_dump.decode("utf-8") if isinstance(output_dump, bytes) else str(output_dump)
        temp_container.remove(v=True)
        _exec_sql(client, settings, f"DROP DATABASE IF EXISTS {build_db} WITH (FORCE);")
        raise ExternalCommandError("pg_dump", exit_code_dump, msg)

    logger.info("pg_dump completed, extracting dump file")

    chunks, _ = db_container.get_archive(f"/tmp/{build_db}.dump")
    raw = b"".join(chunks)
    tar_stream = io.BytesIO(raw)
    with tarfile.open(fileobj=tar_stream, mode="r") as tar:
        member = tar.getmembers()[0]
        f = tar.extractfile(member)
        if f is None:
            raise ExternalCommandError("get_archive", -1, "Could not extract dump from tar")
        with open(dump_path, "wb") as out:
            out.write(f.read())

    logger.info("Dump saved to %s", dump_path)

    ref_data_path = settings.ref_filestore_path
    if os.path.exists(ref_data_path):
        try:
            subprocess.run(
                ["sudo", "-n", "rm", "-rf", ref_data_path],
                check=True,
                capture_output=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            shutil.rmtree(ref_data_path, ignore_errors=True)
    os.makedirs(ref_data_path, exist_ok=True)

    odoo_data_container_path = "/var/lib/odoo/.local/share/Odoo"
    try:
        chunks_fs, _ = temp_container.get_archive(odoo_data_container_path)
        raw_fs = b"".join(chunks_fs)
        tar_fs = io.BytesIO(raw_fs)
        extracted = 0
        with tarfile.open(fileobj=tar_fs, mode="r") as tar:
            for member in tar.getmembers():
                if member.isdir() and member.name == os.path.basename(odoo_data_container_path):
                    continue
                rel = member.name
                prefix = os.path.basename(odoo_data_container_path) + "/"
                if rel.startswith(prefix):
                    rel = rel[len(prefix):]
                if not rel:
                    continue
                member.name = rel
                tar.extract(member, ref_data_path)
                if not member.isdir():
                    extracted += 1
        logger.info("Odoo data extracted to %s (%d files)", ref_data_path, extracted)
    except docker.errors.NotFound:
        logger.info(
            "Odoo did not create data dir during init (normal for --stop-after-init). "
            "The reference data at %s is empty; environments will start with an empty filestore.",
            ref_data_path,
        )

    odoo_uid_gid = get_odoo_uid_gid(client, odoo_image)
    try:
        subprocess.run(
            ["sudo", "-n", "chown", "-R", odoo_uid_gid, ref_data_path],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.warning("Could not chown ref data to %s: %s", odoo_uid_gid, e)

    temp_container.remove(v=True)
    logger.info("Temporary container removed")

    _exec_sql(client, settings, f"DROP DATABASE IF EXISTS {build_db} WITH (FORCE);")
    logger.info("Temporary database dropped")

    logger.info("Reference generation complete, running init_system")
    result = init_system(settings, dump_path=dump_path, force=True)
    result["generated_dump"] = dump_path
    result["generated_filestore"] = ref_data_path
    result["filestore_files"] = sum(1 for _ in pathlib.Path(ref_data_path).rglob("*") if _.is_file())
    return result


_REF_EDITOR_CONTAINER = "flow-ref-editor"
_REF_EDITOR_BRANCH = "__ref__"


def ref_up(
    settings: Settings,
    odoo_image: str,
) -> dict[str, str]:
    client = get_client()

    try:
        existing = client.containers.get(_REF_EDITOR_CONTAINER)
        if existing.status == "running":
            existing.reload()
            ports = existing.ports.get("8069/tcp")
            host_port = ports[0]["HostPort"] if ports else "?"
            url = f"http://{settings.external_host}:{host_port}"
            raise ConflictError(
                f"Reference editor is already running at {url}. "
                f"Use --ref-down to stop it first."
            )
        existing.remove(force=True)
    except docker.errors.NotFound:
        pass

    try:
        db_container = client.containers.get(settings.shared_db_container)
        if db_container.status != "running":
            raise PrerequisiteNotMetError(
                f"{settings.shared_db_container} is not running. Run --init or --generate-ref first."
            )
    except docker.errors.NotFound:
        raise PrerequisiteNotMetError(
            f"{settings.shared_db_container} not found. Run --init or --generate-ref first."
        )

    _wait_pg_ready(client, settings)

    if not _db_exists(client, settings, settings.template_db_name):
        raise PrerequisiteNotMetError(
            f"Template database '{settings.template_db_name}' not found. "
            f"Run --init or --generate-ref first."
        )

    _exec_sql(
        client,
        settings,
        f"UPDATE pg_database SET datistemplate=false WHERE datname='{settings.template_db_name}';",
    )
    logger.info("Template flag removed from %s", settings.template_db_name)

    ref_data_path = settings.ref_filestore_path
    os.makedirs(ref_data_path, exist_ok=True)

    odoo_uid_gid = get_odoo_uid_gid(client, odoo_image)
    try:
        subprocess.run(
            ["sudo", "-n", "chown", "-R", odoo_uid_gid, ref_data_path],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.warning("Could not chown ref data to %s: %s", odoo_uid_gid, e)

    from flow.port_registry import allocate_port
    from flow.docker_ops.env_ops import _get_used_ports

    used_ports = _get_used_ports(client, settings)
    host_port = allocate_port(
        settings.port_registry_path,
        _REF_EDITOR_BRANCH,
        settings.port_range_start,
        settings.port_range_end,
        used_ports=used_ports,
    )

    odoo_env = {
        "HOST": settings.shared_db_container,
        "USER": settings.db_user,
        "PASSWORD": settings.db_password,
    }
    odoo_volumes = {
        ref_data_path: {
            "bind": "/var/lib/odoo/.local/share/Odoo",
            "mode": "rw",
        },
    }
    if _ODOO_CONF_TEMPLATE.exists():
        odoo_volumes[str(_ODOO_CONF_TEMPLATE)] = {"bind": "/etc/odoo/odoo.conf", "mode": "ro"}

    container = client.containers.run(
        odoo_image,
        name=_REF_EDITOR_CONTAINER,
        detach=True,
        network=settings.shared_network,
        ports={"8069/tcp": host_port},
        environment=odoo_env,
        volumes=odoo_volumes,
        labels={settings.managed_label: "true"},
        command=f"odoo -d {settings.template_db_name} --dev=xml",
    )

    url = f"http://{settings.external_host}:{host_port}"
    logger.info("Reference editor started at %s (container=%s)", url, _REF_EDITOR_CONTAINER)

    return {
        "status": "running",
        "url": url,
        "container": _REF_EDITOR_CONTAINER,
        "database": settings.template_db_name,
        "filestore": ref_data_path,
    }


def ref_down(settings: Settings) -> dict[str, str]:
    client = get_client()

    try:
        container = client.containers.get(_REF_EDITOR_CONTAINER)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Reference editor container '{_REF_EDITOR_CONTAINER}' is not running."
        )

    if container.status == "running":
        container.stop()
    container.remove(v=True)
    logger.info("Reference editor container removed")

    from flow.port_registry import release_port
    release_port(settings.port_registry_path, _REF_EDITOR_BRANCH)

    _wait_pg_ready(client, settings)

    dump_path = settings.dump_file_path
    os.makedirs(os.path.dirname(dump_path), exist_ok=True)

    db_container = client.containers.get(settings.shared_db_container)
    dump_file = f"/tmp/{settings.template_db_name}.dump"
    dump_cmd = [
        "pg_dump", "-U", settings.db_user, "-Fc", "-f", dump_file, settings.template_db_name,
    ]

    logger.info("Dumping reference database %s", settings.template_db_name)
    exit_code, output = db_container.exec_run(dump_cmd)
    if exit_code != 0:
        msg = output.decode("utf-8") if isinstance(output, bytes) else str(output)
        raise ExternalCommandError("pg_dump", exit_code, msg)

    chunks, _ = db_container.get_archive(dump_file)
    raw = b"".join(chunks)
    tar_stream = io.BytesIO(raw)
    with tarfile.open(fileobj=tar_stream, mode="r") as tar:
        member = tar.getmembers()[0]
        f = tar.extractfile(member)
        if f is None:
            raise ExternalCommandError("get_archive", -1, "Could not extract dump from tar")
        with open(dump_path, "wb") as out:
            out.write(f.read())

    logger.info("Dump saved to %s", dump_path)

    _exec_sql(
        client,
        settings,
        f"UPDATE pg_database SET datistemplate=true WHERE datname='{settings.template_db_name}';",
    )
    logger.info("Template flag restored on %s", settings.template_db_name)

    return {
        "status": "stopped",
        "dump": dump_path,
        "filestore": settings.ref_filestore_path,
        "database": settings.template_db_name,
    }


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
