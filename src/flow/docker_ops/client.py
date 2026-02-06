import docker
from docker import DockerClient


def get_client() -> DockerClient:
    return docker.from_env()
