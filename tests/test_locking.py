"""A rejected caller must be able to tell "still running" from "stuck".

An agent that reads a bare "another operation is in progress" as a stale lock
reaches for restarts and recreations; the message therefore names the operation
that holds the lock and how long it has held it.
"""

from __future__ import annotations

import threading

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


class TestEnvLocks:
    def test_a_second_acquire_of_the_same_env_is_busy(self):
        locks = LockManager()
        locks.acquire_env("main")

        with pytest.raises(BusyError):
            locks.acquire_env("main")

    def test_different_environments_do_not_contend(self):
        locks = LockManager()
        locks.acquire_env("main")

        locks.acquire_env("other")  # must not raise

    def test_release_makes_the_env_available_again(self):
        locks = LockManager()
        locks.acquire_env("main")
        locks.release_env("main")

        locks.acquire_env("main")  # must not raise

    def test_releasing_an_unheld_env_is_a_no_op(self):
        LockManager().release_env("never-locked")  # must not raise

    def test_a_double_release_is_a_no_op(self):
        locks = LockManager()
        locks.acquire_env("main")
        locks.release_env("main")

        locks.release_env("main")  # must not raise

    def test_blocking_acquire_succeeds_when_free(self):
        assert LockManager().acquire_env_blocking("main", 0.1) is True

    def test_blocking_acquire_times_out_when_held(self):
        locks = LockManager()
        locks.acquire_env_blocking("main", 0.1)

        assert locks.acquire_env_blocking("main", 0.05) is False

    def test_blocking_acquire_waits_for_a_release(self):
        locks = LockManager()
        locks.acquire_env_blocking("main", 0.1)
        threading.Timer(0.05, lambda: locks.release_env("main")).start()

        assert locks.acquire_env_blocking("main", 5) is True
        locks.release_env("main")


class TestTeamLocks:
    def test_a_team_operation_blocks_that_team_s_environments(self):
        locks = LockManager()
        locks.acquire_team("1")

        with pytest.raises(BusyError, match="team"):
            locks.acquire_env("main", team_id="1")

    def test_another_team_is_unaffected(self):
        locks = LockManager()
        locks.acquire_team("1")

        locks.acquire_env("main", team_id="2")  # must not raise

    def test_an_env_operation_blocks_its_team_lock(self):
        locks = LockManager()
        locks.acquire_env("main", team_id="1")

        with pytest.raises(BusyError):
            locks.acquire_team("1")

    def test_a_second_team_acquire_is_busy(self):
        locks = LockManager()
        locks.acquire_team("1")

        with pytest.raises(BusyError):
            locks.acquire_team("1")

    def test_releasing_the_env_frees_the_team_lock(self):
        # The team is remembered at acquire time, so release needs no team_id.
        locks = LockManager()
        locks.acquire_env("main", team_id="1")
        locks.release_env("main")

        locks.acquire_team("1")  # must not raise

    def test_the_team_stays_blocked_while_any_of_its_envs_is_busy(self):
        locks = LockManager()
        locks.acquire_env("main", team_id="1")
        locks.acquire_env("other", team_id="1")
        locks.release_env("main")

        with pytest.raises(BusyError):
            locks.acquire_team("1")

        locks.release_env("other")
        locks.acquire_team("1")  # now free

    def test_an_untagged_env_lock_does_not_block_a_team(self):
        # Only team-scoped acquires participate in the team accounting.
        locks = LockManager()
        locks.acquire_env("main")

        locks.acquire_team("1")  # must not raise


class TestSystemLock:
    def test_a_second_system_acquire_is_busy(self):
        locks = LockManager()
        locks.acquire_system()

        with pytest.raises(BusyError, match="system-level"):
            locks.acquire_system()

    def test_release_makes_the_system_lock_available_again(self):
        locks = LockManager()
        locks.acquire_system()
        locks.release_system()

        locks.acquire_system()  # must not raise

    def test_releasing_an_unheld_system_lock_is_a_no_op(self):
        LockManager().release_system()  # must not raise

    def test_a_double_release_is_a_no_op(self):
        locks = LockManager()
        locks.acquire_system()
        locks.release_system()

        locks.release_system()  # must not raise

    def test_two_managers_do_not_share_state(self):
        LockManager().acquire_system()

        LockManager().acquire_system()  # must not raise
