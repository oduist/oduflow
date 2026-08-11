from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from oduflow.docker_ops.cancellable_exec import exec_run


def _container():
    api = MagicMock()
    container = MagicMock()
    container.id = "container-1"
    container.client.api = api

    def exec_direct(command, **_kwargs):
        if command[0] == "cat":
            return 0, b"complete output"
        return 0, b""

    container.exec_run.side_effect = exec_direct
    return container, api


def test_outside_operation_uses_normal_docker_exec():
    container, api = _container()
    container.exec_run.side_effect = None
    container.exec_run.return_value = SimpleNamespace(exit_code=2, output=b"error")

    code, output = exec_run(container, ["false"], user="odoo")

    assert (code, output) == (2, b"error")
    container.exec_run.assert_called_once_with(["false"], user="odoo", environment=None)
    api.exec_create.assert_not_called()


def test_operation_exec_persists_pid_and_output_for_recovery():
    container, api = _container()
    api.exec_create.return_value = {"Id": "exec-1"}
    api.exec_inspect.return_value = {"Running": False, "ExitCode": 0}
    reported = MagicMock()

    with (
        patch(
            "oduflow.docker_ops.cancellable_exec.current_cancel_event",
            return_value=threading.Event(),
        ),
        patch(
            "oduflow.docker_ops.cancellable_exec.current_operation_id",
            return_value="operation-1",
        ),
        patch(
            "oduflow.docker_ops.cancellable_exec.get_current_runtime_ref",
            return_value={},
        ),
        patch(
            "oduflow.docker_ops.cancellable_exec.report_current_runtime_ref",
            reported,
        ),
    ):
        code, output = exec_run(container, ["odoo", "--stop-after-init"])

    assert (code, output) == (0, b"complete output")
    wrapper = api.exec_create.call_args.args[1]
    assert wrapper[:4] == ["setsid", "-w", "sh", "-c"]
    reported.assert_called_once()
    runtime = reported.call_args.args[0]
    assert runtime["exec_id"] == "exec-1"
    assert runtime["pidfile"].endswith("operation-1.pid")
    assert runtime["outputfile"].endswith("operation-1.log")


def test_running_exec_recovery_polls_and_reads_persisted_output():
    container, api = _container()
    api.exec_inspect.side_effect = [
        {"Running": True, "ExitCode": None},
        {"Running": False, "ExitCode": 3},
    ]
    runtime = {
        "type": "docker_exec",
        "container_id": "container-1",
        "exec_id": "exec-existing",
        "pidfile": "/tmp/existing.pid",
        "outputfile": "/tmp/existing.log",
    }

    with (
        patch(
            "oduflow.docker_ops.cancellable_exec.current_cancel_event",
            return_value=threading.Event(),
        ),
        patch(
            "oduflow.docker_ops.cancellable_exec.current_operation_id",
            return_value="operation-1",
        ),
        patch(
            "oduflow.docker_ops.cancellable_exec.get_current_runtime_ref",
            return_value=runtime,
        ),
    ):
        code, output = exec_run(container, ["ignored"])

    assert (code, output) == (3, b"complete output")
    api.exec_create.assert_not_called()
    api.exec_start.assert_not_called()


def test_image_without_sets_id_falls_back_to_plain_exec():
    container, api = _container()

    def exec_direct(command, **_kwargs):
        if command == [
            "sh",
            "-c",
            "command -v setsid >/dev/null 2>&1 && setsid -w true",
        ]:
            return 127, b""
        return 0, b"plain output"

    container.exec_run.side_effect = exec_direct

    with (
        patch(
            "oduflow.docker_ops.cancellable_exec.current_cancel_event",
            return_value=threading.Event(),
        ),
        patch(
            "oduflow.docker_ops.cancellable_exec.current_operation_id",
            return_value="operation-1",
        ),
        patch(
            "oduflow.docker_ops.cancellable_exec.get_current_runtime_ref",
            return_value={},
        ),
    ):
        code, output = exec_run(container, ["/service", "check"])

    assert (code, output) == (0, b"plain output")
    api.exec_create.assert_not_called()
