import re
from unittest.mock import MagicMock

from oduflow import pg_tune


def _parse(conf: str) -> dict[str, str]:
    """Parse a postgresql.conf body into {key: value}, ignoring comments/blanks."""
    out: dict[str, str] = {}
    for line in conf.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition("=")
        out[key.strip()] = value.strip()
    return out


def _mb(value: str) -> int:
    """Parse a '512MB' / '1GB' memory value into MB."""
    m = re.fullmatch(r"(\d+)(MB|GB|kB)", value)
    assert m, f"unexpected memory value: {value!r}"
    n, unit = int(m.group(1)), m.group(2)
    return {"kB": n // 1024, "MB": n, "GB": n * 1024}[unit]


class TestStructure:
    def test_first_line_is_keep_marker(self):
        conf = pg_tune.generate_postgresql_conf(8192, 4)
        assert conf.splitlines()[0] == "# KEEP"
        assert conf.splitlines()[1].startswith("# ODUFLOW-TUNE ")

    def test_every_setting_line_is_key_value(self):
        conf = pg_tune.generate_postgresql_conf(8192, 4)
        for line in conf.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            assert re.match(r"^\w+ = .+$", stripped), f"bad line: {stripped!r}"

    def test_header_reports_detected_resources(self):
        conf = pg_tune.generate_postgresql_conf(
            8192, 4, source="docker", oduflow_version="9.9.9"
        )
        assert "4 vCPU, 8192 MB RAM (source: docker)" in conf
        assert "9.9.9" in conf


class TestMemoryScaling:
    def test_small_host(self):
        cfg = _parse(pg_tune.generate_postgresql_conf(4096, 2))
        # ~10% of 4GB
        assert _mb(cfg["shared_buffers"]) == 410
        assert _mb(cfg["effective_cache_size"]) == 410 * 3

    def test_large_host_caps_shared_buffers_at_1gb(self):
        cfg = _parse(pg_tune.generate_postgresql_conf(32768, 16))
        assert _mb(cfg["shared_buffers"]) == 1024  # capped
        assert cfg["max_worker_processes"] == "16"

    def test_tiny_host_floors_shared_buffers_at_128mb(self):
        cfg = _parse(pg_tune.generate_postgresql_conf(1024, 1))
        assert _mb(cfg["shared_buffers"]) == 128

    def test_shared_buffers_always_within_bounds(self):
        for ram in (256, 512, 2048, 8192, 65536, 262144):
            cfg = _parse(pg_tune.generate_postgresql_conf(ram, 4))
            assert 128 <= _mb(cfg["shared_buffers"]) <= 1024

    def test_work_mem_floor(self):
        cfg = _parse(pg_tune.generate_postgresql_conf(1024, 1))
        assert _mb(cfg["work_mem"]) == 4  # 128MB/100 -> floored to 4MB

    def test_maintenance_work_mem_capped(self):
        cfg = _parse(pg_tune.generate_postgresql_conf(32768, 16))
        # shared_buffers capped at 1024MB -> /2 = 512 -> capped to 256
        assert _mb(cfg["maintenance_work_mem"]) == 256

    def test_production_enabled_uses_combined_host_plan(self):
        cfg = _parse(
            pg_tune.generate_postgresql_conf(
                8192,
                4,
                production_enabled=True,
            )
        )
        assert _mb(cfg["shared_buffers"]) == 410
        assert _mb(cfg["effective_cache_size"]) == 819
        assert cfg["max_worker_processes"] == "2"

    def test_work_mem_cap(self):
        # shared_buffers 1024MB / 50 connections = 20 -> capped at 16.
        cfg = _parse(pg_tune.generate_postgresql_conf(32768, 16, max_connections=50))
        assert _mb(cfg["work_mem"]) == 16

    def test_maintenance_work_mem_is_half_the_shared_buffers(self):
        cfg = _parse(pg_tune.generate_postgresql_conf(4096, 2))
        assert _mb(cfg["shared_buffers"]) == 410
        assert _mb(cfg["maintenance_work_mem"]) == 205

    def test_maintenance_work_mem_floor(self):
        cfg = _parse(pg_tune.generate_postgresql_conf(1024, 1))
        assert _mb(cfg["maintenance_work_mem"]) == 64

    def test_wal_buffers_is_three_percent_of_shared_buffers(self):
        cfg = _parse(pg_tune.generate_postgresql_conf(4096, 2))
        assert _mb(cfg["wal_buffers"]) == 12  # round(410 * 0.03)

    def test_wal_buffers_floor_and_cap(self):
        floored = _parse(pg_tune.generate_postgresql_conf(1024, 1))
        assert _mb(floored["wal_buffers"]) == 4  # round(128 * 0.03) = 4
        capped = _parse(pg_tune.generate_postgresql_conf(32768, 16))
        assert _mb(capped["wal_buffers"]) == 16  # round(1024 * 0.03) = 31 -> 16

    def test_effective_cache_is_three_times_shared_buffers(self):
        cfg = _parse(pg_tune.generate_postgresql_conf(32768, 16))
        assert _mb(cfg["effective_cache_size"]) == 1024 * 3

    def test_degenerate_resources_still_produce_a_valid_conf(self):
        cfg = _parse(pg_tune.generate_postgresql_conf(0, 0))
        assert _mb(cfg["shared_buffers"]) == 128
        assert cfg["max_worker_processes"] == "1"

    def test_max_connections_floor(self):
        cfg = _parse(pg_tune.generate_postgresql_conf(8192, 4, max_connections=0))
        assert cfg["max_connections"] == "1"


class TestParallelism:
    def test_no_parallel_gather_on_single_cpu(self):
        cfg = _parse(pg_tune.generate_postgresql_conf(4096, 1))
        assert cfg["max_parallel_workers_per_gather"] == "0"
        assert cfg["max_parallel_maintenance_workers"] == "1"

    def test_parallel_gather_capped_at_two(self):
        cfg = _parse(pg_tune.generate_postgresql_conf(16384, 32))
        assert cfg["max_parallel_workers_per_gather"] == "2"
        assert cfg["max_parallel_workers"] == "32"

    def test_autovacuum_workers_capped_at_three(self):
        cfg = _parse(pg_tune.generate_postgresql_conf(16384, 32))
        assert cfg["autovacuum_max_workers"] == "3"

    def test_parallel_gather_is_half_the_cpus_up_to_two(self):
        def gather(cpu):
            return _parse(pg_tune.generate_postgresql_conf(16384, cpu))[
                "max_parallel_workers_per_gather"
            ]

        assert gather(1) == "0"
        assert gather(2) == "1"
        assert gather(4) == "2"
        assert gather(8) == "2"  # capped

    def test_parallel_maintenance_workers_track_cpus_with_a_floor(self):
        def maint(cpu):
            return _parse(pg_tune.generate_postgresql_conf(16384, cpu))[
                "max_parallel_maintenance_workers"
            ]

        assert maint(1) == "1"  # floor
        assert maint(2) == "1"
        assert maint(4) == "2"
        assert maint(16) == "2"  # capped

    def test_autovacuum_workers_scale_below_the_cap(self):
        def workers(cpu):
            return _parse(pg_tune.generate_postgresql_conf(16384, cpu))[
                "autovacuum_max_workers"
            ]

        assert workers(1) == "1"
        assert workers(2) == "2"
        assert workers(3) == "3"


class TestSsdAndConnections:
    def test_ssd_planner_settings(self):
        cfg = _parse(pg_tune.generate_postgresql_conf(8192, 4))
        assert cfg["random_page_cost"] == "1.1"
        assert cfg["effective_io_concurrency"] == "200"

    def test_default_max_connections(self):
        cfg = _parse(pg_tune.generate_postgresql_conf(8192, 4))
        assert cfg["max_connections"] == "100"

    def test_max_connections_override(self):
        cfg = _parse(pg_tune.generate_postgresql_conf(8192, 4, max_connections=200))
        assert cfg["max_connections"] == "200"


class TestDetectResources:
    def test_returns_defaults_when_detection_fails(self, monkeypatch):
        # Force both detection paths to fail.
        import oduflow.docker_ops.client as client_mod
        import oduflow.docker_ops.stats as stats_mod

        def boom(*args, **kwargs):
            raise RuntimeError("no docker")

        monkeypatch.setattr(client_mod, "get_client", boom)
        monkeypatch.setattr(stats_mod, "get_system_stats", boom)

        res = pg_tune.detect_resources()
        assert res == {
            "cpu_count": 2,
            "total_ram_mb": 4096.0,
            "source": "defaults",
        }

    def _docker(self, monkeypatch, info):
        import oduflow.docker_ops.client as client_mod

        client = MagicMock()
        client.info.return_value = info
        monkeypatch.setattr(client_mod, "get_client", lambda: client)

    def _host(self, monkeypatch, stats):
        import oduflow.docker_ops.stats as stats_mod

        monkeypatch.setattr(stats_mod, "get_system_stats", lambda: stats)

    def test_docker_info_is_preferred_and_converted_to_mib(self, monkeypatch):
        self._docker(monkeypatch, {"NCPU": 8, "MemTotal": 16 * 1024**3})
        self._host(monkeypatch, {"cpu_count": 99, "mem_total_mb": 99999.0})

        assert pg_tune.detect_resources() == {
            "cpu_count": 8,
            "total_ram_mb": 16384.0,
            "source": "docker",
        }

    def test_falls_back_to_host_stats_when_docker_reports_nothing(self, monkeypatch):
        # A daemon that answers but reports zeroes is as useless as no daemon.
        self._docker(monkeypatch, {"NCPU": 0, "MemTotal": 0})
        self._host(monkeypatch, {"cpu_count": 4, "mem_total_mb": 8192.0})

        assert pg_tune.detect_resources() == {
            "cpu_count": 4,
            "total_ram_mb": 8192.0,
            "source": "host",
        }

    def test_partial_docker_info_is_rejected(self, monkeypatch):
        # Both values are required; CPUs without RAM (or vice versa) would
        # otherwise tune against a zero.
        self._docker(monkeypatch, {"NCPU": 8, "MemTotal": 0})
        self._host(monkeypatch, {"cpu_count": 4, "mem_total_mb": 8192.0})
        assert pg_tune.detect_resources()["source"] == "host"

        self._docker(monkeypatch, {"NCPU": 0, "MemTotal": 16 * 1024**3})
        assert pg_tune.detect_resources()["source"] == "host"

    def test_missing_docker_keys_are_treated_as_zero(self, monkeypatch):
        self._docker(monkeypatch, {})
        self._host(monkeypatch, {"cpu_count": 4, "mem_total_mb": 8192.0})

        assert pg_tune.detect_resources()["source"] == "host"

    def test_partial_host_stats_fall_through_to_defaults(self, monkeypatch):
        import oduflow.docker_ops.client as client_mod

        monkeypatch.setattr(
            client_mod, "get_client", MagicMock(side_effect=RuntimeError("no docker"))
        )
        self._host(monkeypatch, {"cpu_count": 4, "mem_total_mb": 0.0})

        assert pg_tune.detect_resources()["source"] == "defaults"

    def test_single_cpu_and_one_mib_are_accepted(self, monkeypatch):
        # The guard is `> 0`, not `> 1`: a 1-CPU host is a real host.
        self._docker(monkeypatch, {"NCPU": 1, "MemTotal": 1024**2})

        assert pg_tune.detect_resources() == {
            "cpu_count": 1,
            "total_ram_mb": 1.0,
            "source": "docker",
        }

    def test_docker_reporting_one_byte_of_ram_is_still_accepted(self, monkeypatch):
        # `mem_bytes > 0`: any positive value is real data from the daemon.
        self._docker(monkeypatch, {"NCPU": 2, "MemTotal": 1})

        assert pg_tune.detect_resources()["source"] == "docker"

    def test_host_stats_with_zero_cpu_fall_through_to_defaults(self, monkeypatch):
        # Mirror of the RAM-only case: CPUs missing means the reading is
        # unusable, whatever RAM says.
        import oduflow.docker_ops.client as client_mod

        monkeypatch.setattr(
            client_mod, "get_client", MagicMock(side_effect=RuntimeError("no docker"))
        )
        self._host(monkeypatch, {"cpu_count": 0, "mem_total_mb": 8192.0})

        assert pg_tune.detect_resources()["source"] == "defaults"

    def test_missing_host_cpu_key_is_zero_not_one(self, monkeypatch):
        import oduflow.docker_ops.client as client_mod

        monkeypatch.setattr(
            client_mod, "get_client", MagicMock(side_effect=RuntimeError("no docker"))
        )
        self._host(monkeypatch, {"mem_total_mb": 8192.0})

        assert pg_tune.detect_resources()["source"] == "defaults"

    def test_single_cpu_host_stats_are_accepted(self, monkeypatch):
        import oduflow.docker_ops.client as client_mod

        monkeypatch.setattr(
            client_mod, "get_client", MagicMock(side_effect=RuntimeError("no docker"))
        )
        self._host(monkeypatch, {"cpu_count": 1, "mem_total_mb": 8192.0})

        assert pg_tune.detect_resources() == {
            "cpu_count": 1,
            "total_ram_mb": 8192.0,
            "source": "host",
        }

    def test_sub_mib_host_ram_is_accepted(self, monkeypatch):
        # `ram > 0`, not `> 1`: a fractional MB reading is still a reading.
        import oduflow.docker_ops.client as client_mod

        monkeypatch.setattr(
            client_mod, "get_client", MagicMock(side_effect=RuntimeError("no docker"))
        )
        self._host(monkeypatch, {"cpu_count": 4, "mem_total_mb": 0.5})

        assert pg_tune.detect_resources() == {
            "cpu_count": 4,
            "total_ram_mb": 0.5,
            "source": "host",
        }
