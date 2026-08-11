"""Tests for system_ops._wait_pg_ready resilience.

When the shared PostgreSQL container is momentarily not running (still starting,
or restarting after a crash — e.g. the disk filled up), Docker's exec_create
returns 409. _wait_pg_ready must treat that as "not ready yet" and retry, rather
than letting the raw APIError crash the whole server startup (which systemd then
turns into a restart loop).
"""

from unittest.mock import MagicMock, patch

import pytest

import docker
from oduflow.docker_ops import system_ops
from oduflow.errors import PrerequisiteNotMetError
from oduflow.settings import Settings


def _client(exec_side_effect):
    client = MagicMock()
    container = MagicMock()
    container.exec_run.side_effect = exec_side_effect
    client.containers.get.return_value = container
    return client


def test_tolerates_transient_409_then_ready():
    # First pg_isready hits a restarting container (409); next round it's up.
    client = _client(
        [
            docker.errors.APIError("409 Client Error: Conflict"),  # pg_isready
            (0, b""),  # pg_isready OK
            (0, b"1"),  # psql SELECT 1 OK
        ]
    )
    with patch("oduflow.docker_ops.system_ops.time.sleep"):
        system_ops._wait_pg_ready(client, Settings(), timeout=5)  # returns, no raise


def test_raises_clean_error_when_never_ready():
    client = _client(docker.errors.APIError("409 Client Error: Conflict"))  # always
    with patch("oduflow.docker_ops.system_ops.time.sleep"):
        with pytest.raises(PrerequisiteNotMetError, match="did not become ready"):
            system_ops._wait_pg_ready(client, Settings(), timeout=3)


def test_retries_until_pg_accepts_connections():
    # pg_isready returns non-zero (starting) a couple of times, then ready.
    client = _client(
        [
            (1, b""),  # pg_isready: not ready
            (1, b""),  # pg_isready: not ready
            (0, b""),  # pg_isready: ready
            (0, b"1"),  # psql: ready
        ]
    )
    with patch("oduflow.docker_ops.system_ops.time.sleep"):
        system_ops._wait_pg_ready(client, Settings(), timeout=5)
