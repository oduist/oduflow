"""Docker exec helper that can terminate a queued operation's process group."""

from __future__ import annotations

import shlex
import threading
import time
from collections.abc import Sequence
from typing import Any

from oduflow.operations import (
    OperationCancelled,
    current_cancel_event,
    current_operation_id,
    get_current_runtime_ref,
    report_current_runtime_ref,
)


def _output_bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    if value is None:
        return b""
    return str(value).encode("utf-8", errors="replace")


def exec_run(
    container: Any,
    command: str | Sequence[str],
    *,
    user: str | None = None,
    environment: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    """Run a Docker exec and TERM→KILL its process group on cancellation.

    Outside an operation worker this intentionally delegates to the Docker SDK
    unchanged, keeping read-only and bootstrap calls lightweight.
    """
    cancel_event = current_cancel_event()
    operation_id = current_operation_id()
    if cancel_event is None or operation_id is None:
        result = container.exec_run(
            command,
            user=user,
            environment=environment,
        )
        output = result.output if hasattr(result, "output") else result[1]
        code = result.exit_code if hasattr(result, "exit_code") else result[0]
        return int(code), _output_bytes(output)

    api = container.client.api
    existing = get_current_runtime_ref()
    has_existing_exec = (
        existing.get("type") == "docker_exec"
        and existing.get("container_id") == str(container.id)
        and bool(existing.get("exec_id"))
    )
    if not has_existing_exec:
        try:
            probe = container.exec_run(
                [
                    "sh",
                    "-c",
                    "command -v setsid >/dev/null 2>&1 && setsid -w true",
                ]
            )
            probe_code = probe.exit_code if hasattr(probe, "exit_code") else probe[0]
        except Exception:
            probe_code = 127
        if int(probe_code) != 0:
            # Arbitrary auxiliary images may not ship a shell or setsid.
            # Preserve command compatibility; cancellation then remains
            # best-effort at the operation's next safe checkpoint.
            result = container.exec_run(
                command,
                user=user,
                environment=environment,
            )
            output = result.output if hasattr(result, "output") else result[1]
            code = result.exit_code if hasattr(result, "exit_code") else result[0]
            if cancel_event.is_set():
                raise OperationCancelled("Operation cancellation requested.")
            return int(code), _output_bytes(output)

    argv = shlex.split(command) if isinstance(command, str) else list(command)
    pidfile = f"/tmp/oduflow-operation-{operation_id}.pid"
    outputfile = f"/tmp/oduflow-operation-{operation_id}.log"
    wrapper = [
        "setsid",
        "-w",
        "sh",
        "-c",
        (
            'pidfile="$1"; outputfile="$2"; shift 2; '
            'echo "$$" > "$pidfile"; exec "$@" >"$outputfile" 2>&1'
        ),
        "oduflow-operation",
        pidfile,
        outputfile,
        *argv,
    ]
    existing_exec = False
    if has_existing_exec:
        existing_exec = True
        exec_id = existing["exec_id"]
        pidfile = existing.get("pidfile", pidfile)
        outputfile = existing.get("outputfile", outputfile)
        initial_info = api.exec_inspect(exec_id)
    else:
        created = api.exec_create(
            container.id,
            wrapper,
            user=user,
            environment=environment,
            stdout=True,
            stderr=True,
        )
        exec_id = created["Id"]
        report_current_runtime_ref(
            {
                "type": "docker_exec",
                "container_id": str(container.id),
                "exec_id": exec_id,
                "pidfile": pidfile,
                "outputfile": outputfile,
            }
        )
        initial_info = {}
    cancelled = threading.Event()
    monitor_done = threading.Event()

    def monitor() -> None:
        try:
            while not monitor_done.wait(0.2):
                if not cancel_event.is_set():
                    continue
                cancelled.set()
                pid_result = container.exec_run(["cat", pidfile])
                pid_raw = (
                    pid_result.output
                    if hasattr(pid_result, "output")
                    else pid_result[1]
                )
                pid = _output_bytes(pid_raw).decode("ascii", errors="ignore").strip()
                if not pid.isdigit():
                    return
                container.exec_run(["kill", "-TERM", "--", f"-{pid}"])
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if not api.exec_inspect(exec_id).get("Running", False):
                        return
                    if monitor_done.wait(0.2):
                        return
                container.exec_run(["kill", "-KILL", "--", f"-{pid}"])
                return
        except Exception:
            # The main exec result remains authoritative; cancellation is
            # best-effort when the target image lacks setsid/kill.
            return

    monitor_thread = threading.Thread(
        target=monitor,
        name=f"oduflow-cancel-{operation_id[:8]}",
        daemon=True,
    )
    monitor_thread.start()

    def wait_for_exec(info: dict[str, Any]) -> dict[str, Any]:
        # With no attached stdout (the wrapper redirects to a durable file),
        # Docker may return from exec_start just before Running flips true.
        # Wait through that short false/None startup state, then through exit.
        startup_deadline = time.monotonic() + 5
        while (
            not info.get("Running", False)
            and info.get("ExitCode") is None
            and time.monotonic() < startup_deadline
        ):
            time.sleep(0.05)
            info = api.exec_inspect(exec_id)
        if not info.get("Running", False) and info.get("ExitCode") is None:
            raise RuntimeError(f"Docker exec {exec_id} did not start")
        while info.get("Running", False):
            time.sleep(0.2)
            info = api.exec_inspect(exec_id)
        return info

    try:
        if existing_exec and initial_info.get("Running", False):
            # Docker cannot open a second attach stream for an already-started
            # exec. The wrapper persists output in the container, so recovery
            # only needs to follow exec state and read that file at the end.
            info = wait_for_exec(initial_info)
        elif existing_exec and initial_info.get("ExitCode") is not None:
            info = initial_info
        else:
            api.exec_start(exec_id, stream=False, demux=False)
            info = wait_for_exec(api.exec_inspect(exec_id))

        output_result = container.exec_run(["cat", outputfile])
        output = (
            output_result.output
            if hasattr(output_result, "output")
            else output_result[1]
        )
    finally:
        monitor_done.set()
        monitor_thread.join(timeout=1)
        try:
            container.exec_run(["rm", "-f", pidfile, outputfile])
        except Exception:
            pass
    if cancelled.is_set():
        raise OperationCancelled("Docker process cancelled.")
    return int(info.get("ExitCode") or 0), _output_bytes(output)
