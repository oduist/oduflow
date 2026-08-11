"""Allocation semantics of the port registry.

``allocate_port`` gives an environment a *stable* port: the same one across
restarts, unless that port is out of the configured range or already taken by
another environment's container. Getting the reuse condition wrong hands out a
port that is already bound, so both halves of it — range membership and the
``used_ports`` conflict check — are pinned here, boundaries included.
"""

from __future__ import annotations

import json

import pytest

from oduflow.errors import FlowError
from oduflow.port_registry import allocate_port, get_port, release_port


def _path(tmp_path) -> str:
    return str(tmp_path / "ports.json")


def _seed(tmp_path, registry: dict[str, int]) -> str:
    path = _path(tmp_path)
    with open(path, "w") as f:
        json.dump(registry, f)
    return path


class TestFreshAllocation:
    def test_first_environment_gets_the_range_start(self, tmp_path):
        assert allocate_port(_path(tmp_path), "main", 50_000, 50_100) == 50_000

    def test_next_environment_gets_the_next_free_port(self, tmp_path):
        path = _path(tmp_path)
        assert allocate_port(path, "a", 50_000, 50_100) == 50_000
        assert allocate_port(path, "b", 50_000, 50_100) == 50_001

    def test_allocation_is_persisted(self, tmp_path):
        path = _path(tmp_path)
        allocate_port(path, "main", 50_000, 50_100)
        assert get_port(path, "main") == 50_000

    def test_docker_used_ports_are_skipped(self, tmp_path):
        port = allocate_port(
            _path(tmp_path), "main", 50_000, 50_100, used_ports={50_000, 50_001}
        )
        assert port == 50_002

    def test_released_port_becomes_available_again(self, tmp_path):
        path = _path(tmp_path)
        allocate_port(path, "a", 50_000, 50_100)
        release_port(path, "a")
        assert get_port(path, "a") is None
        assert allocate_port(path, "b", 50_000, 50_100) == 50_000


class TestReuse:
    def test_existing_assignment_is_reused(self, tmp_path):
        path = _seed(tmp_path, {"main": 50_042})
        assert allocate_port(path, "main", 50_000, 50_100) == 50_042

    def test_port_taken_by_another_container_is_not_reused(self, tmp_path):
        # The core conflict check: the registry still says 50_042, but Docker
        # reports it bound elsewhere, so a *different* port must be handed out.
        path = _seed(tmp_path, {"main": 50_042})

        port = allocate_port(path, "main", 50_000, 50_100, used_ports={50_042})

        assert port != 50_042
        assert get_port(path, "main") == port

    def test_reuse_requires_both_conditions(self, tmp_path):
        # In range but used -> reallocate; free but out of range -> reallocate.
        in_range_but_used = _seed(tmp_path, {"main": 50_042})
        assert (
            allocate_port(
                in_range_but_used, "main", 50_000, 50_100, used_ports={50_042}
            )
            != 50_042
        )

        out_of_range_but_free = _seed(tmp_path, {"other": 60_000})
        assert allocate_port(out_of_range_but_free, "other", 50_000, 50_100) != 60_000

    def test_assignment_below_the_range_is_reallocated(self, tmp_path):
        path = _seed(tmp_path, {"main": 49_999})
        assert allocate_port(path, "main", 50_000, 50_100) == 50_000

    def test_range_start_is_inclusive(self, tmp_path):
        path = _seed(tmp_path, {"main": 50_000})
        assert allocate_port(path, "main", 50_000, 50_100) == 50_000

    def test_range_end_is_exclusive(self, tmp_path):
        # 50_100 is past the end of [50_000, 50_100), so it must not be reused.
        path = _seed(tmp_path, {"main": 50_100})
        assert allocate_port(path, "main", 50_000, 50_100) == 50_000

    def test_last_port_in_the_range_is_reusable(self, tmp_path):
        path = _seed(tmp_path, {"main": 50_099})
        assert allocate_port(path, "main", 50_000, 50_100) == 50_099


class TestExhaustion:
    def test_full_range_raises(self, tmp_path):
        path = _seed(tmp_path, {f"env-{p}": p for p in range(50_000, 50_003)})

        with pytest.raises(FlowError, match="No free ports"):
            allocate_port(path, "new", 50_000, 50_003)

    def test_docker_can_exhaust_the_range_on_its_own(self, tmp_path):
        with pytest.raises(FlowError, match="No free ports"):
            allocate_port(
                _path(tmp_path),
                "new",
                50_000,
                50_003,
                used_ports={50_000, 50_001, 50_002},
            )


class TestCorruptRegistry:
    def test_unreadable_registry_is_treated_as_empty(self, tmp_path):
        path = _path(tmp_path)
        with open(path, "w") as f:
            f.write("{not json")

        assert allocate_port(path, "main", 50_000, 50_100) == 50_000

    @pytest.mark.parametrize("doc", ["null", "[1, 2]", '"a string"'])
    def test_a_json_document_of_the_wrong_shape_is_treated_as_empty(
        self, tmp_path, doc
    ):
        # Valid JSON of the wrong shape used to raise AttributeError past the
        # error handling and abort environment creation.
        path = _path(tmp_path)
        with open(path, "w") as f:
            f.write(doc)

        assert allocate_port(path, "main", 50_000, 50_100) == 50_000

    def test_registry_is_written_as_readable_json(self, tmp_path):
        path = _path(tmp_path)
        allocate_port(path, "main", 50_000, 50_100)

        with open(path) as f:
            assert json.load(f) == {"main": 50_000}
