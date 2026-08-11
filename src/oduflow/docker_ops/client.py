import logging
import os
import time

import docker
from docker import DockerClient
from oduflow.errors import PrerequisiteNotMetError

logger = logging.getLogger("oduflow")

_uid_gid_cache: dict[str, str] = {}


def get_client() -> DockerClient:
    try:
        return docker.from_env()
    except docker.errors.DockerException as exc:
        # Raise a FlowError (not SystemExit/BaseException) so @handle_errors and
        # the create_environment fallback can catch it and return a clean error
        # instead of tearing down the request task or the server loop. The CLI
        # entry point translates this into a friendly process exit.
        raise PrerequisiteNotMetError(
            "Cannot connect to Docker. Please make sure Docker is installed "
            "and running. https://docs.docker.com/get-docker/"
        ) from exc


def wait_for_docker(timeout: float = 60.0, poll_seconds: float = 2.0) -> None:
    """Block until the Docker daemon answers, or raise after *timeout*.

    The unit is ordered after ``docker.service``, but a host-wide restart batch
    (unattended-upgrades restarting containerd, say) can still hand us a socket
    that exists while the daemon behind it is not answering yet. Retrying for a
    minute turns that race into a slower start instead of a failed one.
    """
    deadline = time.monotonic() + timeout
    announced = False
    while True:
        try:
            get_client().ping()
            if announced:
                logger.info("Docker daemon is answering")
            return
        except Exception as exc:
            if time.monotonic() >= deadline:
                raise PrerequisiteNotMetError(
                    f"The Docker daemon did not answer within {timeout:.0f}s: {exc}"
                ) from exc
            if not announced:
                logger.info("Waiting for the Docker daemon to answer (%s)", exc)
                announced = True
            time.sleep(poll_seconds)


def get_odoo_uid_gid(client: DockerClient, image: str) -> str:
    if image in _uid_gid_cache:
        return _uid_gid_cache[image]
    try:
        raw = (
            client.containers.run(
                image,
                "id",
                entrypoint="",
                remove=True,
            )
            .decode()
            .strip()
        )
        import re

        uid_m = re.search(r"uid=(\d+)", raw)
        gid_m = re.search(r"gid=(\d+)", raw)
        if uid_m and gid_m:
            result = f"{uid_m.group(1)}:{gid_m.group(1)}"
        else:
            raise ValueError(f"unexpected id output: {raw}")
    except Exception as e:
        logger.warning(
            "Could not detect UID:GID from image %s: %s, falling back to 100:101",
            image,
            e,
        )
        result = "100:101"
    _uid_gid_cache[image] = result
    logger.debug("Detected UID:GID for %s: %s", image, result)
    return result


def chown_recursive(
    path: str, uid: int, gid: int, client: DockerClient, image: str
) -> None:
    """Recursively chown *path* to *uid*:*gid*.

    Tries host-side ``os.chown`` first (works on Linux as root).
    On ``PermissionError`` (e.g. macOS) falls back to running
    ``chown -R`` inside a throwaway container with *path* bind-mounted.
    """
    try:
        for root, dirs, files in os.walk(path):
            os.chown(root, uid, gid)
            for name in dirs + files:
                os.chown(os.path.join(root, name), uid, gid)
    except PermissionError:
        logger.info(
            "Host chown failed (PermissionError), falling back to in-container chown for %s",
            path,
        )
        client.containers.run(
            image,
            f"chown -R {uid}:{gid} /mnt/target",
            entrypoint="",
            user="root",
            remove=True,
            volumes={path: {"bind": "/mnt/target", "mode": "rw"}},
        )
