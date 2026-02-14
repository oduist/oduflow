import io
import json
import logging
import os
import pathlib
import shutil
import subprocess
import tarfile
import time

import docker
from docker import DockerClient

from oduflow.docker_ops.client import get_client, get_odoo_uid_gid
from oduflow.errors import ConflictError, ExternalCommandError, NotFoundError, PrerequisiteNotMetError
from oduflow.naming import get_db_name, get_template_db_name
from oduflow.settings import Settings

logger = logging.getLogger("oduflow")

_PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BUNDLED_PG_CONF = _PACKAGE_ROOT / "templates" / "postgresql.conf"
_BUNDLED_ODOO_CONF = _PACKAGE_ROOT / "templates" / "odoo.conf"
_ETC_DIR = pathlib.Path("/etc/oduflow")


def _normalize_extra_addons(raw_addons, fallback_branch: str) -> dict[str, str]:
    """Convert old list format or new dict format to {name: branch} dict."""
    if isinstance(raw_addons, dict):
        return raw_addons
    if isinstance(raw_addons, list):
        return {name: fallback_branch for name in raw_addons}
    return {}


def _resolve_conf(name: str) -> pathlib.Path:
    """Return /etc/oduflow/{name} if present, otherwise the bundled copy."""
    etc_path = _ETC_DIR / name
    if etc_path.is_file():
        return etc_path
    bundled = _PACKAGE_ROOT / "templates" / name
    return bundled


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

    dynamic_dir = "/etc/oduflow/traefik"
    os.makedirs(dynamic_dir, exist_ok=True)

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
            dynamic_dir: {"bind": "/etc/traefik/dynamic/", "mode": "ro"},
        },
        command=[
            "--log.level=INFO",
            "--providers.docker=true",
            "--providers.docker.exposedbydefault=false",
            f"--providers.docker.network={settings.shared_network}",
            "--providers.file.directory=/etc/traefik/dynamic/",
            "--providers.file.watch=true",
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


def _ensure_iptables_accept(client: DockerClient, network_name: str) -> None:
    try:
        net = client.networks.get(network_name)
        bridge_iface = "br-" + net.id[:12]
    except docker.errors.NotFound:
        return
    try:
        subprocess.run(
            ["iptables", "-C", "INPUT", "-i", bridge_iface, "-j", "ACCEPT"],
            check=True, capture_output=True,
        )
        logger.debug("iptables ACCEPT rule already exists for %s", bridge_iface)
    except (subprocess.CalledProcessError, FileNotFoundError):
        try:
            subprocess.run(
                ["iptables", "-I", "INPUT", "-i", bridge_iface, "-j", "ACCEPT"],
                check=True, capture_output=True,
            )
            logger.info("Added iptables ACCEPT rule for interface %s", bridge_iface)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            logger.warning("Could not add iptables rule for %s: %s", bridge_iface, exc)


def init_system(
    settings: Settings,
) -> dict[str, str]:
    client = get_client()
    logger.info("Initializing system")

    system_labels = {settings.managed_label: "true", settings.system_label: "true"}

    try:
        client.networks.get(settings.shared_network)
    except docker.errors.NotFound:
        client.networks.create(settings.shared_network, labels=system_labels)
        logger.info("Created network %s", settings.shared_network)

    _ensure_iptables_accept(client, settings.shared_network)

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
                str(_resolve_conf("postgresql.conf")): {"bind": "/etc/postgresql/postgresql.conf", "mode": "ro"},
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

    logger.info("System initialized")
    return {"status": "initialized"}


def reload_template(
    settings: Settings,
    template_name: str,
    dump_path: str | None = None,
) -> dict[str, str]:
    client = get_client()
    tpl_db = get_template_db_name(template_name, settings.instance_id)
    resolved_dump = dump_path or settings.get_template_sql_path(template_name)

    if not os.path.isfile(resolved_dump):
        raise NotFoundError(f"Dump file not found: {resolved_dump}")

    _wait_pg_ready(client, settings)

    if _db_exists(client, settings, tpl_db):
        _exec_sql(
            client,
            settings,
            f"UPDATE pg_database SET datistemplate=false WHERE datname='{tpl_db}';",
        )
        _exec_sql(client, settings, f'DROP DATABASE "{tpl_db}" WITH (FORCE);')
        logger.info("Dropped template DB %s", tpl_db)

    _exec_sql(client, settings, f'CREATE DATABASE "{tpl_db}";')

    db_container = client.containers.get(settings.shared_db_container)
    tmp_name = os.path.basename(resolved_dump)

    _copy_file_to_container(db_container, resolved_dump, "/tmp")

    use_psql = _is_text_dump(resolved_dump)

    if use_psql:
        restore_cmd = ["psql", "-U", settings.db_user, "-d", tpl_db, "-f", f"/tmp/{tmp_name}"]
    else:
        restore_cmd = ["pg_restore", "-U", settings.db_user, "-d", tpl_db, f"/tmp/{tmp_name}"]

    logger.info("DB restore started, template_db=%s, dump=%s", tpl_db, resolved_dump)
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
        f"UPDATE pg_database SET datistemplate=true WHERE datname='{tpl_db}';",
    )

    logger.info("Template DB reloaded, template_db=%s, restore_time=%.1fs", tpl_db, restore_elapsed)
    return {"status": "reloaded", "template_db": tpl_db, "restore_seconds": round(restore_elapsed, 1)}


def init_template(
    settings: Settings,
    template_name: str,
    odoo_image: str = "odoo:17.0",
    modules: str = "base",
    force: bool = False,
) -> dict[str, str]:
    template_sql_path = settings.get_template_sql_path(template_name)
    template_filestore_path = settings.get_template_filestore_path(template_name)

    existing_dump = os.path.exists(template_sql_path)
    existing_filestore = (
        os.path.exists(template_filestore_path)
        and any(True for _ in pathlib.Path(template_filestore_path).rglob("*") if _.is_file())
    )

    if (existing_dump or existing_filestore) and not force:
        parts = []
        if existing_dump:
            parts.append(f"dump.sql ({template_sql_path})")
        if existing_filestore:
            parts.append(f"filestore ({template_filestore_path})")
        raise RuntimeError(
            f"Existing data found: {', '.join(parts)}. "
            "Use --force to overwrite."
        )

    if force:
        if existing_dump:
            os.remove(template_sql_path)
            logger.info("Removed existing %s", template_sql_path)
        if existing_filestore:
            shutil.rmtree(template_filestore_path)
            logger.info("Removed existing %s", template_filestore_path)

    client = get_client()
    logger.info("Generating template dump from clean Odoo", extra={"image": odoo_image, "modules": modules})

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
                str(_resolve_conf("postgresql.conf")): {"bind": "/etc/postgresql/postgresql.conf", "mode": "ro"},
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
    temp_container_name = "flow-template-builder"

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
    odoo_conf = _resolve_conf("odoo.conf")
    if odoo_conf.exists():
        volumes[str(odoo_conf)] = {"bind": "/etc/odoo/odoo.conf", "mode": "ro"}

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

    os.makedirs(os.path.dirname(template_sql_path), exist_ok=True)

    db_container = client.containers.get(settings.shared_db_container)
    dump_cmd = [
        "pg_dump", "-U", settings.db_user, "-Fp", "-f", f"/tmp/{build_db}.dump", build_db,
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
        with open(template_sql_path, "wb") as out:
            out.write(f.read())

    logger.info("Dump saved to %s", template_sql_path)

    if os.path.exists(template_filestore_path):
        shutil.rmtree(template_filestore_path)
    os.makedirs(template_filestore_path, exist_ok=True)

    odoo_data_container_path = "/var/lib/odoo/.local/share/Odoo"
    try:
        chunks_fs, _ = temp_container.get_archive(odoo_data_container_path)
        raw_fs = b"".join(chunks_fs)
        tar_fs = io.BytesIO(raw_fs)
        extracted = 0
        src_fs_prefix = f"Odoo/filestore/{build_db}/"
        with tarfile.open(fileobj=tar_fs, mode="r") as tar:
            for member in tar.getmembers():
                if not member.name.startswith(src_fs_prefix) and member.name != f"Odoo/filestore/{build_db}":
                    continue
                rel = member.name[len(src_fs_prefix):]
                if not rel:
                    continue
                member.name = rel
                tar.extract(member, template_filestore_path)
                if not member.isdir():
                    extracted += 1
        logger.info("Filestore extracted to %s (%d files)", template_filestore_path, extracted)

    except docker.errors.NotFound:
        logger.info(
            "Odoo did not create data dir during init (normal for --stop-after-init). "
            "The template filestore at %s is empty; environments will start with an empty filestore.",
            template_filestore_path,
        )

    odoo_uid_gid = get_odoo_uid_gid(client, odoo_image)
    uid_str, gid_str = odoo_uid_gid.split(":")
    uid, gid = int(uid_str), int(gid_str)
    for root, dirs, files in os.walk(template_filestore_path):
        os.chown(root, uid, gid)
        for name in dirs + files:
            os.chown(os.path.join(root, name), uid, gid)

    temp_container.remove(v=True)
    logger.info("Temporary container removed")

    _exec_sql(client, settings, f"DROP DATABASE IF EXISTS {build_db} WITH (FORCE);")
    logger.info("Temporary database dropped")

    logger.info("Template generation complete, loading into template DB")
    result = reload_template(settings, template_name=template_name)

    metadata = {"odoo_image": odoo_image}
    metadata_path = settings.get_template_metadata_path(template_name)
    with open(metadata_path, "w") as f:
        json.dump(metadata, f, indent=2)
    logger.info("Template metadata saved to %s", metadata_path)

    result["generated_dump"] = template_sql_path
    result["generated_filestore"] = template_filestore_path
    result["filestore_files"] = sum(1 for _ in pathlib.Path(template_filestore_path).rglob("*") if _.is_file())
    return result


_TEMPLATE_EDITOR_CONTAINER = "flow-template-editor"
_TEMPLATE_EDITOR_BRANCH = "__template__"


def template_up(
    settings: Settings,
    odoo_image: str,
    template_name: str,
) -> dict[str, str]:
    client = get_client()
    tpl_db = get_template_db_name(template_name, settings.instance_id)

    try:
        existing = client.containers.get(_TEMPLATE_EDITOR_CONTAINER)
        if existing.status == "running":
            existing.reload()
            ports = existing.ports.get("8069/tcp")
            host_port = ports[0]["HostPort"] if ports else "?"
            url = f"http://{settings.external_host}:{host_port}"
            raise ConflictError(
                f"Template editor is already running at {url}. "
                f"Use --template-down to stop it first."
            )
        existing.remove(force=True)
    except docker.errors.NotFound:
        pass

    try:
        db_container = client.containers.get(settings.shared_db_container)
        if db_container.status != "running":
            raise PrerequisiteNotMetError(
                f"{settings.shared_db_container} is not running. Run init first."
            )
    except docker.errors.NotFound:
        raise PrerequisiteNotMetError(
            f"{settings.shared_db_container} not found. Run init first."
        )

    _wait_pg_ready(client, settings)

    if not _db_exists(client, settings, tpl_db):
        raise PrerequisiteNotMetError(
            f"Template database '{tpl_db}' not found. "
            f"Run init-template first."
        )

    _exec_sql(
        client,
        settings,
        f"UPDATE pg_database SET datistemplate=false WHERE datname='{tpl_db}';",
    )
    logger.info("Template flag removed from %s", tpl_db)

    template_filestore_path = settings.get_template_filestore_path(template_name)
    os.makedirs(template_filestore_path, exist_ok=True)

    odoo_uid_gid = get_odoo_uid_gid(client, odoo_image)
    uid_str, gid_str = odoo_uid_gid.split(":")
    uid, gid = int(uid_str), int(gid_str)
    for root, dirs, files in os.walk(template_filestore_path):
        os.chown(root, uid, gid)
        for name in dirs + files:
            os.chown(os.path.join(root, name), uid, gid)

    sessions_path = os.path.join(settings.get_template_dir(template_name), "sessions")
    os.makedirs(sessions_path, mode=0o777, exist_ok=True)
    os.chmod(sessions_path, 0o777)
    for root_dir, dirs, files in os.walk(sessions_path):
        os.chown(root_dir, uid, gid)
        for name in dirs + files:
            os.chown(os.path.join(root_dir, name), uid, gid)

    from oduflow.port_registry import allocate_port
    from oduflow.docker_ops.env_ops import _get_used_ports

    used_ports = _get_used_ports(client, settings)
    host_port = allocate_port(
        settings.port_registry_path,
        _TEMPLATE_EDITOR_BRANCH,
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
        template_filestore_path: {
            "bind": f"/var/lib/odoo/.local/share/Odoo/filestore/{tpl_db}",
            "mode": "rw",
        },
        sessions_path: {
            "bind": "/var/lib/odoo/.local/share/Odoo/sessions",
            "mode": "rw",
        },
    }
    odoo_conf = _resolve_conf("odoo.conf")
    if odoo_conf.exists():
        odoo_volumes[str(odoo_conf)] = {"bind": "/etc/odoo/odoo.conf", "mode": "ro"}

    container = client.containers.run(
        odoo_image,
        name=_TEMPLATE_EDITOR_CONTAINER,
        detach=True,
        network=settings.shared_network,
        ports={"8069/tcp": host_port},
        environment=odoo_env,
        volumes=odoo_volumes,
        labels={settings.managed_label: "true"},
        command=f"odoo -d {tpl_db} --dev=xml",
    )

    metadata_path = settings.get_template_metadata_path(template_name)
    metadata = {}
    if os.path.isfile(metadata_path):
        with open(metadata_path) as f:
            metadata = json.load(f)
    metadata["odoo_image"] = odoo_image
    with open(metadata_path, "w") as f:
        json.dump(metadata, f, indent=2)

    url = f"http://{settings.external_host}:{host_port}"
    logger.info("Template editor started at %s (container=%s)", url, _TEMPLATE_EDITOR_CONTAINER)

    return {
        "status": "running",
        "url": url,
        "container": _TEMPLATE_EDITOR_CONTAINER,
        "database": tpl_db,
        "filestore": template_filestore_path,
    }


def template_down(settings: Settings, template_name: str) -> dict[str, str]:
    client = get_client()
    tpl_db = get_template_db_name(template_name, settings.instance_id)

    try:
        container = client.containers.get(_TEMPLATE_EDITOR_CONTAINER)
    except docker.errors.NotFound:
        raise NotFoundError(
            f"Template editor container '{_TEMPLATE_EDITOR_CONTAINER}' is not running."
        )

    if container.status == "running":
        container.stop()
    container.remove(v=True)
    logger.info("Template editor container removed")

    from oduflow.port_registry import release_port
    release_port(settings.port_registry_path, _TEMPLATE_EDITOR_BRANCH)

    _wait_pg_ready(client, settings)

    template_sql_path = settings.get_template_sql_path(template_name)
    os.makedirs(os.path.dirname(template_sql_path), exist_ok=True)

    db_container = client.containers.get(settings.shared_db_container)
    dump_file = f"/tmp/{tpl_db}.dump"
    dump_cmd = [
        "pg_dump", "-U", settings.db_user, "-Fc", "-f", dump_file, tpl_db,
    ]

    logger.info("Dumping template database %s", tpl_db)
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
        with open(template_sql_path, "wb") as out:
            out.write(f.read())

    logger.info("Dump saved to %s", template_sql_path)

    _exec_sql(
        client,
        settings,
        f"UPDATE pg_database SET datistemplate=true WHERE datname='{tpl_db}';",
    )
    logger.info("Template flag restored on %s", tpl_db)

    return {
        "status": "stopped",
        "dump": template_sql_path,
        "filestore": settings.get_template_filestore_path(template_name),
        "database": tpl_db,
    }


def publish_env_as_template(settings: Settings, branch_name: str, template_name: str) -> dict[str, str]:
    from oduflow.docker_ops import env_ops
    from oduflow.naming import get_db_name, get_filestore_paths

    client = get_client()
    tpl_db = get_template_db_name(template_name, settings.instance_id)
    env_db = get_db_name(branch_name, settings.instance_id)

    if not _db_exists(client, settings, env_db):
        raise NotFoundError(f"Database '{env_db}' for branch '{branch_name}' not found.")

    _wait_pg_ready(client, settings)
    db_container = client.containers.get(settings.shared_db_container)

    # 1. pg_dump branch DB → new template dump
    dump_path = settings.get_template_sql_path(template_name)
    os.makedirs(os.path.dirname(dump_path), exist_ok=True)
    dump_file = f"/tmp/{env_db}.dump"
    dump_cmd = ["pg_dump", "-U", settings.db_user, "-Fc", "-f", dump_file, env_db]

    logger.info("Dumping branch database %s", env_db)
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

    logger.info("Branch dump saved to %s", dump_path)

    # 2. Reload template DB from new dump
    reload_template(settings, template_name=template_name, dump_path=dump_path)

    # 3. Collect active branches (excluding the promoted one is fine, it keeps working)
    active_envs = env_ops.list_environments(settings)
    active_branches = [e["branch"] for e in active_envs]

    # 4. Snapshot the promoted branch's merged filestore (while overlay is still mounted)
    branch_paths = get_filestore_paths(branch_name, settings.workspaces_dir)
    branch_merged = branch_paths["merged"]
    template_filestore_path = settings.get_template_filestore_path(template_name)

    if os.path.isdir(branch_merged) and os.path.ismount(branch_merged):
        # Overlay-mounted filestore — snapshot while still mounted
        snapshot_dir = branch_merged + "_snapshot"
        if os.path.exists(snapshot_dir):
            shutil.rmtree(snapshot_dir)
        shutil.copytree(branch_merged, snapshot_dir)
        logger.info("Snapshot of merged filestore created for branch %s", branch_name)
    elif os.path.isdir(branch_merged) and not os.path.ismount(branch_merged):
        # Plain (non-overlay) filestore — e.g. env created with template=none
        snapshot_dir = branch_merged + "_snapshot"
        if os.path.exists(snapshot_dir):
            shutil.rmtree(snapshot_dir)
        shutil.copytree(branch_merged, snapshot_dir)
        logger.info("Snapshot of plain filestore created for branch %s", branch_name)
    else:
        snapshot_dir = None
        logger.warning("Branch filestore %s not found, skipping filestore update", branch_merged)

    # 5. Unmount all overlays
    for branch in active_branches:
        try:
            env_ops._unmount_filestore(branch, settings)
            logger.info("Unmounted overlay for branch %s", branch)
        except Exception as e:
            logger.warning("Could not unmount overlay for %s: %s", branch, e)

    # 6. Replace template filestore with snapshot
    if snapshot_dir and os.path.isdir(snapshot_dir):
        if os.path.exists(template_filestore_path):
            shutil.rmtree(template_filestore_path)

        os.makedirs(os.path.dirname(template_filestore_path), exist_ok=True)
        try:
            os.rename(snapshot_dir, template_filestore_path)
        except OSError:
            shutil.copytree(snapshot_dir, template_filestore_path)
            shutil.rmtree(snapshot_dir)
        logger.info("Template filestore replaced from branch %s", branch_name)

        promoted_container_name = f"{settings.prefix}{branch_name.replace('/', '-')}-odoo"
        try:
            pc = client.containers.get(promoted_container_name)
            promoted_image = pc.image.tags[0] if pc.image.tags else "odoo:17.0"
        except (docker.errors.NotFound, IndexError):
            promoted_image = "odoo:17.0"
        odoo_uid_gid = get_odoo_uid_gid(client, promoted_image)
        uid_str, gid_str = odoo_uid_gid.split(":")
        uid, gid = int(uid_str), int(gid_str)
        for root, dirs, files in os.walk(template_filestore_path):
            os.chown(root, uid, gid)
            for name in dirs + files:
                os.chown(os.path.join(root, name), uid, gid)
        logger.info("Template filestore chowned to %s", odoo_uid_gid)

    # 7. Remount overlays (clear upper dirs so branches start fresh from new template)
    for branch in active_branches:
        try:
            bp = get_filestore_paths(branch, settings.workspaces_dir)

            if os.path.ismount(bp["merged"]):
                env_ops._unmount_filestore(branch, settings)
                deadline = time.time() + 3.0
                while time.time() < deadline and os.path.ismount(bp["merged"]):
                    time.sleep(0.1)
                if os.path.ismount(bp["merged"]):
                    logger.warning(
                        "Overlay for %s still mounted after retry, skipping remount",
                        branch,
                    )
                    continue

            for key in ("upper", "work"):
                d = bp[key]
                if os.path.isdir(d):
                    shutil.rmtree(d)
                    os.makedirs(d, mode=0o777, exist_ok=True)

            odoo_container_name = f"{settings.prefix}{branch.replace('/', '-')}-odoo"
            try:
                container = client.containers.get(odoo_container_name)
                image = container.image.tags[0] if container.image.tags else "odoo:17.0"
            except (docker.errors.NotFound, IndexError):
                image = "odoo:17.0"

            env_db = get_db_name(branch, settings.instance_id)
            env_ops._mount_filestore(client, settings, branch, env_db, image, {}, template_name=template_name)
            logger.info("Remounted overlay for branch %s", branch)

            try:
                container = client.containers.get(odoo_container_name)
                container.restart(timeout=10)
                logger.info("Restarted container %s", odoo_container_name)
            except docker.errors.NotFound:
                pass
        except Exception as e:
            logger.warning("Could not remount overlay for %s: %s", branch, e)

    # Save template metadata from promoted environment
    metadata_path = settings.get_template_metadata_path(template_name)
    promoted_container_name = f"{settings.prefix}{branch_name.replace('/', '-')}-odoo"
    metadata = {}
    try:
        pc = client.containers.get(promoted_container_name)
        metadata["odoo_image"] = pc.labels.get(settings.image_label, "")
        metadata["repo_url"] = pc.labels.get(settings.repo_label, "")
        raw_extras = pc.labels.get("oduflow.extra_addons", "")
        if raw_extras:
            try:
                parsed = json.loads(raw_extras)
                metadata["extra_addons"] = _normalize_extra_addons(parsed, settings.default_branch)
            except (json.JSONDecodeError, TypeError):
                pass
    except docker.errors.NotFound:
        pass
    with open(metadata_path, "w") as f:
        json.dump(metadata, f, indent=2)
    logger.info("Template metadata saved to %s", metadata_path)

    return {
        "status": "promoted",
        "branch": branch_name,
        "dump": dump_path,
        "filestore": template_filestore_path,
        "template_db": tpl_db,
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
    svc_containers = [
        c for c in containers
        if c.labels.get("oduflow.service") and c.name not in system_names
    ]
    blocking = env_containers + svc_containers
    if blocking:
        names = [c.name for c in blocking]
        from oduflow.errors import ConflictError
        if svc_containers and not env_containers:
            raise ConflictError(
                f"Active environments/services exist: {', '.join(names)}. Delete them first."
            )
        elif env_containers and not svc_containers:
            raise ConflictError(
                f"Active environments exist: {', '.join(names)}. Delete them first."
            )
        else:
            raise ConflictError(
                f"Active environments/services exist: {', '.join(names)}. Delete them first."
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


def drop_template(settings: Settings, template_name: str) -> dict[str, str]:
    client = get_client()
    tpl_db = get_template_db_name(template_name, settings.instance_id)

    if _db_exists(client, settings, tpl_db):
        _wait_pg_ready(client, settings)
        _exec_sql(client, settings, f"UPDATE pg_database SET datistemplate=false WHERE datname='{tpl_db}';")
        _exec_sql(client, settings, f'DROP DATABASE IF EXISTS "{tpl_db}" WITH (FORCE);')
        logger.info("Dropped template DB %s", tpl_db)

    template_dir_path = settings.get_template_dir(template_name)
    if os.path.isdir(template_dir_path):
        shutil.rmtree(template_dir_path)
        logger.info("Removed template directory %s", template_dir_path)

    return {"status": "dropped", "template_name": template_name, "template_db": tpl_db}


def list_templates(settings: Settings) -> list[dict]:
    client = get_client()
    templates = settings.list_templates()
    result = []
    for template_name in templates:
        tpl_db = get_template_db_name(template_name, settings.instance_id)
        has_sql = os.path.isfile(settings.get_template_sql_path(template_name))
        has_filestore = os.path.isdir(settings.get_template_filestore_path(template_name))
        db_loaded = _db_exists(client, settings, tpl_db)
        metadata = {}
        metadata_path = settings.get_template_metadata_path(template_name)
        if os.path.isfile(metadata_path):
            with open(metadata_path) as f:
                metadata = json.load(f)
        result.append({
            "template_name": template_name,
            "template_db": tpl_db,
            "has_sql": has_sql,
            "has_filestore": has_filestore,
            "db_loaded": db_loaded,
            "odoo_image": metadata.get("odoo_image", ""),
            "repo_url": metadata.get("repo_url", ""),
            "extra_addons": _normalize_extra_addons(
                metadata.get("extra_addons", {}),
                settings.default_branch,
            ),
        })
    return result
