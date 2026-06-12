"""Concurrency regression tests for the port registry.

Regression: bulk delete in the dashboard fires parallel per-environment
requests; each release rewrote ports.json through the same fixed
``ports.json.tmp`` path, so concurrent writers renamed each other's temp file
away and releases failed with
``[Errno 2] No such file or directory: 'ports.json.tmp' -> 'ports.json'``.
The registry now serializes read-modify-write cycles and writes through a
unique temp file per write.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

from oduflow.port_registry import allocate_port, get_port, release_port

_RANGE = (50_000, 50_100)


def _path(tmp_path) -> str:
    return str(tmp_path / "ports.json")


def test_parallel_release_bulk_delete(tmp_path):
    """Many environments released at once (the bulk-delete shape) must all
    succeed and leave an empty registry."""
    path = _path(tmp_path)
    envs = [f"env-{i}" for i in range(16)]
    for env in envs:
        allocate_port(path, env, *_RANGE)

    with ThreadPoolExecutor(max_workers=16) as pool:
        # ThreadPoolExecutor.map re-raises any worker exception here.
        list(pool.map(lambda env: release_port(path, env), envs))

    for env in envs:
        assert get_port(path, env) is None


def test_parallel_allocate_unique_ports(tmp_path):
    """Concurrent allocations must never hand out the same port twice."""
    path = _path(tmp_path)
    envs = [f"env-{i}" for i in range(16)]

    with ThreadPoolExecutor(max_workers=16) as pool:
        ports = list(pool.map(lambda env: allocate_port(path, env, *_RANGE), envs))

    assert len(set(ports)) == len(envs)
    for env, port in zip(envs, ports):
        assert get_port(path, env) == port


def test_parallel_mixed_allocate_and_release(tmp_path):
    """Releases racing allocations must neither crash nor resurrect entries."""
    path = _path(tmp_path)
    old = [f"old-{i}" for i in range(8)]
    new = [f"new-{i}" for i in range(8)]
    for env in old:
        allocate_port(path, env, *_RANGE)

    def work(task):
        kind, env = task
        if kind == "release":
            release_port(path, env)
            return None
        return allocate_port(path, env, *_RANGE)

    tasks = [("release", e) for e in old] + [("allocate", e) for e in new]
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(work, tasks))

    for env in old:
        assert get_port(path, env) is None
    new_ports = [get_port(path, env) for env in new]
    assert None not in new_ports
    assert len(set(new_ports)) == len(new)


def test_no_stray_temp_files_after_parallel_writes(tmp_path):
    path = _path(tmp_path)
    envs = [f"env-{i}" for i in range(12)]
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda env: allocate_port(path, env, *_RANGE), envs))
        list(pool.map(lambda env: release_port(path, env), envs))

    leftovers = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert leftovers == []
