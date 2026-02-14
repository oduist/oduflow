import logging

import docker
from docker import DockerClient

logger = logging.getLogger("oduflow")

_uid_gid_cache: dict[str, str] = {}


def get_client() -> DockerClient:
    try:
        return docker.from_env()
    except docker.errors.DockerException:
        raise SystemExit(
            "\n❌ Cannot connect to Docker.\n"
            "   Please make sure Docker is installed and running.\n"
            "   https://docs.docker.com/get-docker/\n"
        )


def get_odoo_uid_gid(client: DockerClient, image: str) -> str:
    if image in _uid_gid_cache:
        return _uid_gid_cache[image]
    try:
        raw = client.containers.run(
            image, "id", entrypoint="", remove=True,
        ).decode().strip()
        import re
        uid_m = re.search(r"uid=(\d+)", raw)
        gid_m = re.search(r"gid=(\d+)", raw)
        if uid_m and gid_m:
            result = f"{uid_m.group(1)}:{gid_m.group(1)}"
        else:
            raise ValueError(f"unexpected id output: {raw}")
    except Exception as e:
        logger.warning("Could not detect UID:GID from image %s: %s, falling back to 100:101", image, e)
        result = "100:101"
    _uid_gid_cache[image] = result
    logger.debug("Detected UID:GID for %s: %s", image, result)
    return result
