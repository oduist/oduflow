import gzip
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

from oduflow.docker_ops.client import chown_recursive, get_client, get_odoo_uid_gid
from oduflow.errors import ConflictError, ExternalCommandError, NotFoundError, PrerequisiteNotMetError
from oduflow.naming import get_db_name, get_template_db_name
from oduflow.settings import Settings

logger = logging.getLogger("oduflow")

_PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BUNDLED_PG_CONF = _PACKAGE_ROOT / "templates" / "postgresql.conf"
_BUNDLED_ODOO_CONF = _PACKAGE_ROOT / "templates" / "odoo.conf"
_BUNDLED_SANITIZE_DIR = _PACKAGE_ROOT / "templates"
def _get_etc_dir() -> pathlib.Path:
    from oduflow.settings import Settings
    return pathlib.Path(Settings._default_etc_dir())


def _file_size_mb(path: str) -> float:
    """Return file size in MB, or 0.0 if file does not exist."""
    try:
        return os.path.getsize(path) / (1024 * 1024)
    except OSError:
        return 0.0


def _update_template_sizes(settings: Settings, template_name: str, metadata: dict | None = None) -> dict:
    """Compute filestore/dump sizes and persist them into template metadata."""
    from oduflow.docker_ops.env_ops import _dir_size_mb

    metadata_path = settings.get_template_metadata_path(template_name)
    if metadata is None:
        if os.path.isfile(metadata_path):
            with open(metadata_path) as f:
                metadata = json.load(f)
        else:
            metadata = {}

    fs_path = settings.get_template_filestore_path(template_name)
    dump_path = settings.get_template_sql_path(template_name)
    fs_size = round(_dir_size_mb(fs_path), 1) if os.path.isdir(fs_path) else 0.0
    metadata["filestore_size_mb"] = fs_size
    metadata["dump_size_mb"] = round(_file_size_mb(dump_path), 1)
    if "use_overlay" not in metadata or metadata["use_overlay"] is None:
        metadata["use_overlay"] = fs_size >= settings.overlay_threshold_mb

    with open(metadata_path, "w") as f:
        json.dump(metadata, f, indent=2)
    return metadata


def _normalize_extra_addons(raw_addons) -> dict[str, str]:
    """Convert old list format or new dict format to {name: branch} dict."""
    if isinstance(raw_addons, dict):
        return raw_addons
    if isinstance(raw_addons, list):
        logger.warning("Legacy list format for extra_addons (no branch info), skipping: %s", raw_addons)
        return {}
    return {}


def _resolve_conf(name: str) -> pathlib.Path:
    """Return /etc/oduflow/{name} if present, otherwise the bundled copy."""
    etc_path = _get_etc_dir() / name
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


def _create_pg_role(client: DockerClient, settings: Settings, username: str, password: str, db_name: str) -> None:
    safe_pw = password.replace("'", "''")
    _exec_sql(
        client, settings,
        f"DO $$ BEGIN "
        f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{username}') THEN "
        f"CREATE ROLE \"{username}\" LOGIN PASSWORD '{safe_pw}'; "
        f"END IF; "
        f"END $$;",
    )
    _exec_sql(client, settings, f'ALTER DATABASE "{db_name}" OWNER TO "{username}";')
    logger.info("Created/ensured PG role '%s' for database '%s'", username, db_name)


def _drop_pg_role(client: DockerClient, settings: Settings, username: str) -> None:
    if username == settings.db_user:
        return
    try:
        _exec_sql(client, settings, f'REASSIGN OWNED BY "{username}" TO "{settings.db_user}";')
    except Exception:
        pass
    try:
        _exec_sql(client, settings, f'DROP OWNED BY "{username}";')
    except Exception:
        pass
    try:
        _exec_sql(client, settings, f'DROP ROLE IF EXISTS "{username}";')
        logger.info("Dropped PG role '%s'", username)
    except Exception as exc:
        logger.warning("Failed to drop PG role '%s': %s", username, exc)


def _db_exists(client: DockerClient, settings: Settings, db_name: str) -> bool:
    result = _exec_sql(
        client,
        settings,
        f"SELECT 1 FROM pg_database WHERE datname='{db_name}';",
    )
    return result == "1"


def _decompress_if_gzip(path: str) -> str:
    """Decompress .gz file to temporary location if needed. Return path to uncompressed file."""
    if not path.endswith(".gz"):
        return path
    
    import tempfile
    with tempfile.NamedTemporaryFile(delete=False, suffix=".sql") as tmp:
        tmp_path = tmp.name
    
    try:
        with gzip.open(path, "rb") as f_in:
            with open(tmp_path, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
        logger.info("Decompressed gzip dump from %s to %s", path, tmp_path)
        return tmp_path
    except Exception as e:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise ExternalCommandError("gzip", 1, f"Failed to decompress {path}: {e}")


def _is_text_dump(path: str) -> bool:
    """Check if dump is text format (SQL) or binary (pgdump). Handles gzip files."""
    try:
        # Handle gzip files by reading header directly
        if path.endswith(".gz"):
            with gzip.open(path, "rb") as f:
                header = f.read(5)
        else:
            with open(path, "rb") as f:
                header = f.read(5)
        return header != b"PGDMP"
    except OSError:
        return False


def _copy_file_to_container(container: docker.models.containers.Container, src_path: str, dest_dir: str) -> None:
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        with tarfile.open(tmp_path, mode="w") as tar:
            tar.add(src_path, arcname=os.path.basename(src_path))
        with open(tmp_path, "rb") as f:
            container.put_archive(dest_dir, f)
    finally:
        os.remove(tmp_path)


def _copy_file_from_container(container: docker.models.containers.Container, container_path: str, dest_path: str) -> None:
    import tempfile

    chunks, _ = container.get_archive(container_path)
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
        for chunk in chunks:
            tmp.write(chunk)
        tmp_path = tmp.name
    try:
        with tarfile.open(tmp_path, mode="r") as tar:
            member = tar.getmembers()[0]
            f = tar.extractfile(member)
            if f is None:
                raise ExternalCommandError("get_archive", -1, f"Could not extract {container_path} from tar")
            with open(dest_path, "wb") as out:
                shutil.copyfileobj(f, out)
    finally:
        os.remove(tmp_path)


def _extract_archive_from_container(
    container: docker.models.containers.Container,
    container_path: str,
    dest_dir: str,
    prefix: str,
) -> int:
    import tempfile

    chunks, _ = container.get_archive(container_path)
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tmp:
        for chunk in chunks:
            tmp.write(chunk)
        tmp_path = tmp.name
    try:
        extracted = 0
        with tarfile.open(tmp_path, mode="r") as tar:
            for member in tar.getmembers():
                if not member.name.startswith(prefix) and member.name != prefix.rstrip("/"):
                    continue
                rel = member.name[len(prefix):]
                if not rel:
                    continue
                member.name = rel
                tar.extract(member, dest_dir)
                if not member.isdir():
                    extracted += 1
        return extracted
    finally:
        os.remove(tmp_path)


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

    # --- Seed system-wide sanitization scripts ---
    sanitize_dest = os.path.join(settings.etc_dir, "odoo_sanitize")
    os.makedirs(sanitize_dest, exist_ok=True)
    bundled_sql = _BUNDLED_SANITIZE_DIR / "01_disable_mail.sql"
    dest_sql = os.path.join(sanitize_dest, "01_disable_mail.sql")
    if not os.path.isfile(dest_sql):
        shutil.copy2(str(bundled_sql), dest_sql)
        logger.info("Copied default sanitize script to %s", dest_sql)

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

    # Decompress gzip dump if needed
    decompressed_dump = _decompress_if_gzip(resolved_dump)
    cleanup_decompressed = decompressed_dump != resolved_dump

    try:
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
        tmp_name = os.path.basename(decompressed_dump)

        _copy_file_to_container(db_container, decompressed_dump, "/tmp")

        use_psql = _is_text_dump(decompressed_dump)

        if use_psql:
            restore_cmd = ["psql", "-U", settings.db_user, "-d", tpl_db, "-f", f"/tmp/{tmp_name}"]
        else:
            restore_cmd = ["pg_restore", "-U", settings.db_user, "-d", tpl_db, f"/tmp/{tmp_name}"]

        logger.info("DB restore started, template_db=%s, dump=%s", tpl_db, resolved_dump)
        restore_start = time.monotonic()

        exit_code, output = db_container.exec_run(restore_cmd)

        restore_elapsed = time.monotonic() - restore_start
        output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)

        if exit_code != 0:
            logger.error("DB restore failed after %.1fs: %s", restore_elapsed, output_str)
            cmd_name = "psql" if use_psql else "pg_restore"
            raise ExternalCommandError(cmd_name, exit_code, output_str)

        # Log restore output even on success (may contain warnings/info)
        if output_str.strip():
            logger.info("DB restore output: %s", output_str)

        logger.info("DB restore finished in %.1fs", restore_elapsed)

        # Verify that tables were actually restored
        table_count = _exec_sql(
            client,
            settings,
            f"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';",
            tpl_db,
        ).strip()

        try:
            num_tables = int(table_count)
        except ValueError:
            num_tables = 0

        if num_tables == 0:
            error_msg = f"Dump restore succeeded but no tables found in database {tpl_db}"
            logger.error(error_msg)
            raise ExternalCommandError("restore", 1, error_msg)

        logger.info("Verified %d tables in restored database %s", num_tables, tpl_db)

        if dump_path:
            tpl_dir = settings.get_template_dir(template_name)
            os.makedirs(tpl_dir, exist_ok=True)
            dest_name = "dump.sql" if use_psql else "dump.pgdump"
            other_name = "dump.pgdump" if use_psql else "dump.sql"
            dest_path = os.path.join(tpl_dir, dest_name)
            other_path = os.path.join(tpl_dir, other_name)
            if not os.path.exists(dest_path) or not os.path.samefile(dump_path, dest_path):
                shutil.copy2(dump_path, dest_path)
            if os.path.isfile(other_path):
                os.remove(other_path)
                logger.info("Removed old dump %s", other_path)
            logger.info("Saved dump to workspace: %s", dest_path)

        _exec_sql(
            client,
            settings,
            f"UPDATE pg_database SET datistemplate=true WHERE datname='{tpl_db}';",
        )

        _update_template_sizes(settings, template_name)
        logger.info("Template DB reloaded, template_db=%s, restore_time=%.1fs, tables=%d", tpl_db, restore_elapsed, num_tables)
        return {
            "status": "reloaded",
            "template_db": tpl_db,
            "restore_seconds": round(restore_elapsed, 1),
            "tables": str(num_tables),
            "message": output_str,
        }
    finally:
        # Cleanup decompressed temp file
        if cleanup_decompressed and os.path.exists(decompressed_dump):
            try:
                os.remove(decompressed_dump)
                logger.debug("Cleaned up decompressed dump at %s", decompressed_dump)
            except OSError as e:
                logger.warning("Failed to cleanup decompressed dump %s: %s", decompressed_dump, e)


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

    build_db = "oduflow_template_build"
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

    _copy_file_from_container(db_container, f"/tmp/{build_db}.dump", template_sql_path)

    logger.info("Dump saved to %s", template_sql_path)

    if os.path.exists(template_filestore_path):
        shutil.rmtree(template_filestore_path)
    os.makedirs(template_filestore_path, exist_ok=True)

    odoo_data_container_path = "/var/lib/odoo/.local/share/Odoo"
    try:
        src_fs_prefix = f"Odoo/filestore/{build_db}/"
        extracted = _extract_archive_from_container(
            temp_container, odoo_data_container_path, template_filestore_path, src_fs_prefix,
        )
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
    chown_recursive(template_filestore_path, uid, gid, client, odoo_image)

    temp_container.remove(v=True)
    logger.info("Temporary container removed")

    _exec_sql(client, settings, f"DROP DATABASE IF EXISTS {build_db} WITH (FORCE);")
    logger.info("Temporary database dropped")

    logger.info("Template generation complete, loading into template DB")
    result = reload_template(settings, template_name=template_name)

    metadata = {"odoo_image": odoo_image}
    from oduflow.docker_ops.env_ops import _dir_size_mb
    fs_size = _dir_size_mb(template_filestore_path)
    metadata["use_overlay"] = fs_size >= settings.overlay_threshold_mb
    metadata = _update_template_sizes(settings, template_name, metadata)
    logger.info("Template metadata saved (use_overlay=%s, filestore=%.0f MB)", metadata["use_overlay"], fs_size)

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
    chown_recursive(template_filestore_path, uid, gid, client, odoo_image)

    sessions_path = os.path.join(settings.get_template_dir(template_name), "sessions")
    os.makedirs(sessions_path, mode=0o777, exist_ok=True)
    os.chmod(sessions_path, 0o777)
    chown_recursive(sessions_path, uid, gid, client, odoo_image)

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

    _copy_file_from_container(db_container, dump_file, template_sql_path)

    logger.info("Dump saved to %s", template_sql_path)

    _exec_sql(
        client,
        settings,
        f"UPDATE pg_database SET datistemplate=true WHERE datname='{tpl_db}';",
    )
    logger.info("Template flag restored on %s", tpl_db)

    _update_template_sizes(settings, template_name)

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

    _copy_file_from_container(db_container, dump_file, dump_path)

    logger.info("Branch dump saved to %s", dump_path)

    # 2. Reload template DB from new dump
    reload_template(settings, template_name=template_name, dump_path=dump_path)

    # 3. Collect active branches that use THIS template AND overlay mount
    active_envs = env_ops.list_environments(settings)
    active_branches = [
        e["branch"] for e in active_envs
        if e.get("template_name") == template_name
        and os.path.ismount(get_filestore_paths(e["branch"], settings.workspaces_dir)["merged"])
    ]

    # 4. Snapshot the source branch's merged filestore (while overlay is still mounted)
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
        chown_recursive(template_filestore_path, int(uid_str), int(gid_str), client, promoted_image)
        logger.info("Template filestore chowned to %s", odoo_uid_gid)

    # 7. Reset filestores to new template baseline
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
            logger.info("Filestore reset for branch %s", branch)

            try:
                container = client.containers.get(odoo_container_name)
                container.restart(timeout=10)
                logger.info("Restarted container %s", odoo_container_name)
            except docker.errors.NotFound:
                pass
        except Exception as e:
            logger.warning("Could not reset filestore for %s: %s", branch, e)

    # Save template metadata from source environment
    metadata_path = settings.get_template_metadata_path(template_name)
    promoted_container_name = f"{settings.prefix}{branch_name.replace('/', '-')}-odoo"
    metadata = {}
    try:
        pc = client.containers.get(promoted_container_name)
        metadata["odoo_image"] = pc.labels.get(settings.image_label, "")
        metadata["repo_url"] = pc.labels.get(settings.repo_label, "")
        metadata["git_user"] = pc.labels.get("oduflow.git_user", "")
        raw_extras = pc.labels.get("oduflow.extra_addons", "")
        if raw_extras:
            try:
                parsed = json.loads(raw_extras)
                metadata["extra_addons"] = _normalize_extra_addons(parsed)
            except (json.JSONDecodeError, TypeError):
                pass
    except docker.errors.NotFound:
        pass
    fs_size = env_ops._dir_size_mb(template_filestore_path) if os.path.isdir(template_filestore_path) else 0.0
    metadata["use_overlay"] = fs_size >= settings.overlay_threshold_mb
    metadata = _update_template_sizes(settings, template_name, metadata)
    logger.info("Template metadata saved (use_overlay=%s, filestore=%.0f MB)", metadata["use_overlay"], fs_size)

    return {
        "status": "promoted",
        "branch": branch_name,
        "dump": dump_path,
        "filestore": template_filestore_path,
        "template_db": tpl_db,
        "affected_branches": active_branches,
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


def import_from_odoo(
    settings: Settings,
    odoo_url: str,
    master_pwd: str,
    db_name: str = "",
    template_name: str = "",
) -> dict[str, object]:
    """Import a template from a running Odoo instance via its database manager API.

    Downloads a full backup (SQL + filestore), extracts it into the template
    directory, reads manifest.json to determine the Odoo version, saves
    metadata.json, and loads the dump into PostgreSQL as a template DB.
    """
    import urllib.request
    import urllib.parse
    import zipfile

    base = odoo_url.rstrip("/")

    # 1. Resolve database name
    if not db_name:
        req = urllib.request.Request(
            f"{base}/web/database/list",
            data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": {}}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
        databases = body.get("result", [])
        if not databases:
            raise NotFoundError(f"No databases found on {base}")
        if len(databases) > 1:
            raise PrerequisiteNotMetError(
                f"Multiple databases found: {', '.join(databases)}. "
                f"Specify db_name explicitly."
            )
        db_name = databases[0]
        logger.info("Auto-detected database: %s", db_name)

    # 2. Download backup
    logger.info("Downloading backup from %s (db=%s)...", base, db_name)
    boundary = "----OduflowBoundary"
    parts = []
    for field_name, field_value in [("master_pwd", master_pwd), ("name", db_name), ("backup_format", "zip")]:
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field_name}"\r\n\r\n'
            f"{field_value}\r\n"
        )
    parts.append(f"--{boundary}--\r\n")
    multipart_body = "".join(parts).encode("utf-8")

    req = urllib.request.Request(
        f"{base}/web/database/backup",
        data=multipart_body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )

    download_start = time.monotonic()
    tmp_zip = os.path.join(settings.data_dir, "tmp_odoo_backup.zip")
    os.makedirs(settings.data_dir, exist_ok=True)
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "zip" not in content_type and "octet" not in content_type:
                body = resp.read(2000).decode("utf-8", errors="replace")
                raise ExternalCommandError(
                    "odoo backup", -1,
                    f"Unexpected response (Content-Type: {content_type}): {body}"
                )
            with open(tmp_zip, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
    except urllib.error.HTTPError as e:
        body = e.read(2000).decode("utf-8", errors="replace")
        raise ExternalCommandError("odoo backup", e.code, f"HTTP {e.code}: {body}")

    download_elapsed = time.monotonic() - download_start
    zip_size_mb = os.path.getsize(tmp_zip) / (1024 * 1024)
    logger.info("Backup downloaded in %.1fs (%.1f MB)", download_elapsed, zip_size_mb)

    # 3. Extract ZIP
    template_dir = settings.get_template_dir(template_name)
    template_sql_path = os.path.join(template_dir, "dump.sql")
    template_filestore_path = settings.get_template_filestore_path(template_name)

    os.makedirs(template_dir, exist_ok=True)

    manifest = {}
    try:
        with zipfile.ZipFile(tmp_zip, "r") as zf:
            # Extract manifest.json
            if "manifest.json" in zf.namelist():
                with zf.open("manifest.json") as mf:
                    manifest = json.load(mf)

            # Extract dump.sql
            with zf.open("dump.sql") as src, open(template_sql_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            logger.info("Extracted dump.sql to %s", template_sql_path)

            # Extract filestore
            if os.path.exists(template_filestore_path):
                shutil.rmtree(template_filestore_path)
            os.makedirs(template_filestore_path, exist_ok=True)

            fs_prefix = "filestore/"
            for member in zf.namelist():
                if not member.startswith(fs_prefix):
                    continue
                rel = member[len(fs_prefix):]
                if not rel:
                    continue
                # Skip checklist/ symlink-like entries
                if rel.startswith("checklist/"):
                    continue
                target = os.path.join(template_filestore_path, rel)
                if member.endswith("/"):
                    os.makedirs(target, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(member) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)

            fs_count = sum(1 for f in pathlib.Path(template_filestore_path).rglob("*") if f.is_file())
            logger.info("Extracted filestore to %s (%d files)", template_filestore_path, fs_count)
    finally:
        if os.path.exists(tmp_zip):
            os.remove(tmp_zip)

    # 4. Determine Odoo image from manifest
    major_version = manifest.get("major_version", "")
    odoo_image = f"odoo:{major_version}" if major_version else ""

    # 5. Save metadata.json
    metadata = {
        "odoo_image": odoo_image,
        "repo_url": "",
        "source_url": base,
        "source_db": db_name,
        "odoo_version": manifest.get("version", ""),
        "pg_version": manifest.get("pg_version", ""),
        "modules": manifest.get("modules", {}),
    }
    _update_template_sizes(settings, template_name, metadata)
    logger.info("Metadata saved for template %s", template_name)

    # 6. Load dump into PostgreSQL
    result = reload_template(settings, template_name=template_name)

    return {
        "status": "imported",
        "template_name": template_name,
        "source_url": base,
        "source_db": db_name,
        "odoo_image": odoo_image,
        "odoo_version": manifest.get("version", ""),
        "template_db": result["template_db"],
        "restore_seconds": result.get("restore_seconds", 0),
        "zip_size_mb": round(zip_size_mb, 1),
    }


def cleanup_orphans(settings: Settings, dry_run: bool = True) -> dict:
    """Find and remove orphaned databases, workspaces, and port registry entries.

    An orphan is a resource whose branch has no corresponding Docker container.
    Template databases (oduflow_template_*) are always excluded.

    Returns a dict with keys: orphan_databases, orphan_workspaces, orphan_ports,
    each a list of removed (or would-be-removed) names.
    """
    from oduflow.port_registry import _load_registry, _save_registry

    client = get_client()

    # 1. Collect branches that have live containers
    filters = {
        "label": [
            f"{settings.managed_label}=true",
            f"{settings.instance_label}={settings.instance_id}",
        ]
    }
    live_branches: set[str] = set()
    for c in client.containers.list(all=True, filters=filters):
        branch = c.labels.get(settings.branch_label)
        if branch:
            live_branches.add(branch)

    db_prefix = f"oduflow_{settings.instance_id}_"

    # 2. Orphan databases
    rows = _exec_sql(
        client, settings,
        "SELECT datname FROM pg_database WHERE datistemplate=false AND datname NOT IN ('postgres','template0','template1');",
    )
    all_dbs = [r for r in rows.splitlines() if r]

    orphan_dbs: list[str] = []
    for db_name in all_dbs:
        if not db_name.startswith(db_prefix):
            continue
        # Reverse-map: strip prefix to get the slug, then check if any live branch produces this db name
        matched = any(get_db_name(b, settings.instance_id) == db_name for b in live_branches)
        if not matched:
            orphan_dbs.append(db_name)

    # 3. Orphan workspace directories
    orphan_workspaces: list[str] = []
    if os.path.isdir(settings.workspaces_dir):
        for entry in os.listdir(settings.workspaces_dir):
            entry_path = os.path.join(settings.workspaces_dir, entry)
            if not os.path.isdir(entry_path):
                continue
            # Protected workspaces are never cleaned up
            if os.path.exists(os.path.join(entry_path, ".protected")):
                continue
            matched = any(
                entry == b.replace("/", "-") for b in live_branches
            )
            if not matched:
                orphan_workspaces.append(entry)

    # 4. Orphan port registry entries
    orphan_ports: list[str] = []
    registry = _load_registry(settings.port_registry_path)
    for branch in list(registry.keys()):
        if branch not in live_branches:
            orphan_ports.append(branch)

    # 5. Orphan PG roles
    from oduflow.env_credentials import generate_pg_username
    role_prefix = f"u_{settings.instance_id}_"
    roles_raw = _exec_sql(client, settings, "SELECT rolname FROM pg_roles WHERE rolcanlogin=true;")
    all_roles = [r for r in roles_raw.splitlines() if r.startswith(role_prefix)]
    orphan_roles: list[str] = []
    for role in all_roles:
        matched = any(
            role == generate_pg_username(b, settings.instance_id)
            for b in live_branches
        )
        if not matched:
            orphan_roles.append(role)

    if dry_run:
        logger.info(
            "Cleanup dry-run: %d orphan DBs, %d orphan workspaces, %d orphan ports, %d orphan roles",
            len(orphan_dbs), len(orphan_workspaces), len(orphan_ports), len(orphan_roles),
        )
        return {
            "dry_run": True,
            "orphan_databases": orphan_dbs,
            "orphan_workspaces": orphan_workspaces,
            "orphan_ports": orphan_ports,
            "orphan_roles": orphan_roles,
        }

    # --- Actually remove ---
    removed_dbs: list[str] = []
    for db_name in orphan_dbs:
        try:
            _exec_sql(client, settings, f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE);')
            removed_dbs.append(db_name)
            logger.info("Dropped orphan database %s", db_name)
        except Exception as exc:
            logger.warning("Failed to drop orphan database %s: %s", db_name, exc)

    removed_workspaces: list[str] = []
    for entry in orphan_workspaces:
        entry_path = os.path.join(settings.workspaces_dir, entry)
        try:
            # Unmount any overlay before removing
            from oduflow.docker_ops.env_ops import _unmount_filestore
            _unmount_filestore(entry, settings)
            shutil.rmtree(entry_path)
            removed_workspaces.append(entry)
            logger.info("Removed orphan workspace %s", entry_path)
        except Exception as exc:
            logger.warning("Failed to remove orphan workspace %s: %s", entry_path, exc)

    removed_ports: list[str] = []
    for branch in orphan_ports:
        registry.pop(branch, None)
        removed_ports.append(branch)
        logger.info("Released orphan port for branch '%s'", branch)
    if removed_ports:
        _save_registry(settings.port_registry_path, registry)

    removed_roles: list[str] = []
    for role in orphan_roles:
        try:
            _drop_pg_role(client, settings, role)
            removed_roles.append(role)
        except Exception as exc:
            logger.warning("Failed to drop orphan PG role %s: %s", role, exc)

    return {
        "dry_run": False,
        "orphan_databases": removed_dbs,
        "orphan_workspaces": removed_workspaces,
        "orphan_ports": removed_ports,
        "orphan_roles": removed_roles,
    }


def delete_template(settings: Settings, template_name: str) -> dict[str, str]:
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
        if "filestore_size_mb" not in metadata or "dump_size_mb" not in metadata:
            metadata = _update_template_sizes(settings, template_name, metadata)
        result.append({
            "template_name": template_name,
            "template_db": tpl_db,
            "has_sql": has_sql,
            "has_filestore": has_filestore,
            "db_loaded": db_loaded,
            "odoo_image": metadata.get("odoo_image", ""),
            "repo_url": metadata.get("repo_url", ""),
            "git_user": metadata.get("git_user", ""),
            "extra_addons": _normalize_extra_addons(
                metadata.get("extra_addons", {}),
            ),
            "use_overlay": metadata.get("use_overlay"),
            "filestore_size_mb": metadata.get("filestore_size_mb"),
            "dump_size_mb": metadata.get("dump_size_mb"),
        })
    return result
