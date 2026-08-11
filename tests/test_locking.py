"""A rejected caller must be able to tell "still running" from "stuck".

An agent that reads a bare "another operation is in progress" as a stale lock
reaches for restarts and recreations; the message therefore names the operation
that holds the lock and how long it has held it.
"""

import pytest

from oduflow.errors import BusyError
from oduflow.locking import LockManager, _format_age


class TestFormatAge:
    def test_seconds(self):
        assert _format_age(42) == "42s"

    def test_minutes(self):
        assert _format_age(252) == "4m12s"

    def test_hours(self):
        assert _format_age(3900) == "1h05m"


class TestEnvLockDiagnostics:
    def test_busy_error_names_the_holder(self):
        locks = LockManager()
        locks.acquire_env("main", "1", operation="pull_and_apply")
        with pytest.raises(BusyError) as exc:
            locks.acquire_env("main", "1", operation="run_odoo_tests")
        message = str(exc.value)
        assert "pull_and_apply" in message
        assert "running for" in message
        # The lock is held by a live operation, not leaked — say so, because the
        # tempting "fix" (restart the environment) would interrupt real work.
        assert "restarting" in message

    def test_holder_is_forgotten_after_release(self):
        locks = LockManager()
        locks.acquire_env("main", "1", operation="pull_and_apply")
        locks.release_env("main")
        assert locks.describe_env_holder("main") == ""
        # Reacquiring is possible and reports the new holder.
        locks.acquire_env("main", "1", operation="run_odoo_tests")
        assert "run_odoo_tests" in locks.describe_env_holder("main")

    def test_unknown_operation_still_reports_age(self):
        locks = LockManager()
        locks.acquire_env("main")
        with pytest.raises(BusyError) as exc:
            locks.acquire_env("main")
        assert "running for" in str(exc.value)

    def test_blocking_acquire_records_holder(self):
        locks = LockManager()
        assert locks.acquire_env_blocking("prod:1:erp", 0.1, operation="webhook deploy")
        assert "webhook deploy" in locks.describe_env_holder("prod:1:erp")


class TestTeamLockDiagnostics:
    def test_team_operation_blocks_env_and_is_named(self):
        locks = LockManager()
        locks.acquire_team("1", operation="save_as_template")
        with pytest.raises(BusyError) as exc:
            locks.acquire_env("feature-x", "1", operation="pull_and_apply")
        assert "save_as_template" in str(exc.value)

    def test_env_operation_blocks_team_and_is_named(self):
        locks = LockManager()
        locks.acquire_env("feature-x", "1", operation="run_odoo_tests")
        with pytest.raises(BusyError) as exc:
            locks.acquire_team("1", operation="save_as_template")
        message = str(exc.value)
        assert "feature-x" in message
        assert "run_odoo_tests" in message

    def test_team_lock_is_reusable_after_a_rejected_acquire(self):
        # The rejected team acquire must not leave the underlying lock taken.
        locks = LockManager()
        locks.acquire_env("feature-x", "1")
        with pytest.raises(BusyError):
            locks.acquire_team("1")
        locks.release_env("feature-x")
        locks.acquire_team("1")  # would raise if the lock had leaked
        locks.release_team("1")


class TestSystemLockDiagnostics:
    def test_system_busy_names_the_holder(self):
        locks = LockManager()
        locks.acquire_system(operation="init_system")
        with pytest.raises(BusyError) as exc:
            locks.acquire_system(operation="destroy")
        assert "init_system" in str(exc.value)
        locks.release_system()
        locks.acquire_system()  # released cleanly
