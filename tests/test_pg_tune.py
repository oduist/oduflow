import re

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
