import re

from oduflow import prod_tune


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
    m = re.fullmatch(r"(\d+)(MB|GB|kB)", value)
    assert m, f"unexpected memory value: {value!r}"
    n, unit = int(m.group(1)), m.group(2)
    return {"kB": n // 1024, "MB": n, "GB": n * 1024}[unit]


class TestProdPostgresConf:
    def test_first_line_is_keep_marker(self):
        conf = prod_tune.generate_prod_postgresql_conf(8192, 4)
        assert conf.splitlines()[0] == "# KEEP"

    def test_archiving_enabled_with_noop_command(self):
        # archive_mode needs a PG restart to toggle, so it must ship enabled;
        # the actual command is flipped later via ALTER SYSTEM by walg.py.
        settings = _parse(prod_tune.generate_prod_postgresql_conf(8192, 4))
        assert settings["archive_mode"] == "on"
        assert settings["archive_command"] == "'/bin/true'"
        assert settings["wal_level"] == "replica"

    def test_shared_buffers_share_of_ram(self):
        settings = _parse(prod_tune.generate_prod_postgresql_conf(16384, 8))
        assert _mb(settings["shared_buffers"]) == round(16384 * 0.20)

    def test_shared_buffers_floor_and_cap(self):
        small = _parse(prod_tune.generate_prod_postgresql_conf(1024, 2))
        assert _mb(small["shared_buffers"]) == 512
        huge = _parse(prod_tune.generate_prod_postgresql_conf(128 * 1024, 32))
        assert _mb(huge["shared_buffers"]) == 8192

    def test_max_connections_default(self):
        settings = _parse(prod_tune.generate_prod_postgresql_conf(8192, 4))
        assert settings["max_connections"] == "200"


class TestOdooWorkerSettings:
    def test_standard_formula(self):
        # 4 CPU, plenty of RAM: workers = 2*4+1 = 9 but capped at 8 by default.
        opts = prod_tune.compute_odoo_worker_settings(4, 32768)
        assert opts["workers"] == "8"
        assert opts["max_cron_threads"] == "1"
        assert opts["proxy_mode"] == "True"
        assert opts["list_db"] == "False"

    def test_ram_bounded(self):
        # 4 GB host: 45% RAM budget = ~1.8GB -> 2 workers minimum floor.
        opts = prod_tune.compute_odoo_worker_settings(8, 4096)
        assert opts["workers"] == "2"

    def test_cap_override(self):
        opts = prod_tune.compute_odoo_worker_settings(16, 128 * 1024, workers_cap=20)
        assert opts["workers"] == "20"

    def test_db_maxconn_scales_with_workers(self):
        opts = prod_tune.compute_odoo_worker_settings(4, 32768)
        assert int(opts["db_maxconn"]) == max(2 * int(opts["workers"]) + 3, 16)

    def test_minimum_two_workers(self):
        opts = prod_tune.compute_odoo_worker_settings(1, 2048)
        assert int(opts["workers"]) >= 2
