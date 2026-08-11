from __future__ import annotations

import asyncio
import os
import stat
import time
import uuid
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import docker
from oduflow.operations import (
    OperationManager,
    OperationRecord,
    operation,
    register_operation,
    report_current_output,
    static_resource,
)
from oduflow.settings import Settings, TeamSettings


class FakeKV:
    def __init__(self, records: list[OperationRecord] | None = None):
        self.values = {
            record.operation_id: record.to_bytes() for record in records or []
        }
        self.purged: list[tuple[str, float | None]] = []
        self.purge_deletes_calls: list[int] = []
        self.on_put = None

    async def put(self, key, value):
        self.values[key] = value
        if self.on_put is not None:
            self.on_put(key, value)

    async def get(self, key):
        from nats.js.errors import KeyNotFoundError

        if key not in self.values:
            raise KeyNotFoundError
        return SimpleNamespace(value=self.values[key])

    async def keys(self):
        from nats.js.errors import NoKeysError

        if not self.values:
            raise NoKeysError
        return list(self.values)

    async def purge(self, key, msg_ttl=None):
        self.values.pop(key, None)
        self.purged.append((key, msg_ttl))

    async def purge_deletes(self, olderthan=1800):
        self.purge_deletes_calls.append(olderthan)


class FakeJS:
    def __init__(self):
        self.published: list[tuple[str, bytes, dict[str, str]]] = []

    async def publish(self, subject, data, headers):
        self.published.append((subject, data, headers))


class FakeObjects:
    def __init__(self):
        self.deleted: list[str] = []
        self.values: dict[str, bytes] = {}

    async def delete(self, name):
        self.deleted.append(name)
        self.values.pop(name, None)

    async def put(self, name, value):
        self.values[name] = value

    async def get(self, name):
        return SimpleNamespace(data=self.values[name])

    async def list(self, ignore_deletes=False):
        return [SimpleNamespace(name=name) for name in self.values]


class FakeMsg:
    def __init__(self):
        self.acked = False

    async def ack(self):
        self.acked = True


def _inline_calls(manager: OperationManager) -> None:
    manager._call = lambda coroutine, timeout=15.0: asyncio.run(coroutine)  # type: ignore[method-assign]


def test_operation_record_public_hides_arguments_and_result_by_default():
    record = OperationRecord(
        operation_id="op-1",
        kind="test",
        team_id="1",
        resources=["env:1:x"],
        arguments={"token": "secret"},
        state="succeeded",
        result={"password": "secret"},
        coalesce_key="internal",
    )

    public = record.public()

    assert "arguments" not in public
    assert "team_id" not in public
    assert "coalesce_key" not in public
    assert "result" not in public
    assert record.public(include_result=True)["result"] == {"password": "secret"}


def test_submit_generates_uuid_and_returns_immediate_ticket():
    manager = OperationManager(Settings())
    manager._kv = FakeKV()
    manager._js = FakeJS()
    _inline_calls(manager)

    ticket = manager.submit(
        "test.uuid",
        "1",
        {"value": 1},
        ["env:1:x"],
        wait=False,
    )

    assert uuid.UUID(ticket["operation_id"]).version == 4
    assert ticket["state"] == "queued"
    assert len(manager._js.published) == 1
    manager._executor.shutdown()


def test_wait_true_returns_fast_result_without_a_second_call():
    manager = OperationManager(Settings(operation_wait_timeout_seconds=1))
    kv = FakeKV()
    manager._kv = kv
    manager._js = FakeJS()
    _inline_calls(manager)

    def complete_when_queued(key, raw):
        record = OperationRecord.from_bytes(raw)
        if record.state != "queued":
            return
        record.state = "succeeded"
        record.started_at = time.time()
        record.completed_at = time.time()
        record.result = {"ok": True}
        kv.values[key] = record.to_bytes()
        manager._signal(key)

    kv.on_put = complete_when_queued

    result = manager.submit("test.fast", "1", {}, ["env:1:x"], wait=True)

    assert result == {"ok": True}
    manager._executor.shutdown()


def test_wait_true_returns_ticket_after_safe_timeout_without_cancelling():
    manager = OperationManager(Settings(operation_wait_timeout_seconds=0.01))
    manager._kv = FakeKV()
    manager._js = FakeJS()
    _inline_calls(manager)

    result = manager.submit("test.slow", "1", {}, ["env:1:x"], wait=True)

    assert result["state"] == "queued"
    assert uuid.UUID(result["operation_id"]).version == 4
    assert len(manager._js.published) == 1
    manager._executor.shutdown()


def test_scheduler_is_fifo_per_resource_but_keeps_independent_parallelism():
    manager = OperationManager(Settings(operation_max_workers=3))
    manager._kv = FakeKV()
    manager._objects = FakeObjects()
    manager._pending = OrderedDict(
        (
            record.operation_id,
            (FakeMsg(), record),
        )
        for record in (
            OperationRecord("a", "test.a", "1", ["env:1:x"], {}, state="queued"),
            OperationRecord("b", "test.b", "1", ["env:1:x"], {}, state="queued"),
            OperationRecord("c", "test.c", "1", ["env:1:y"], {}, state="queued"),
        )
    )
    for kind in ("test.a", "test.b", "test.c"):
        register_operation(kind, lambda: None, lambda _args, _team: ())

    asyncio.run(manager._schedule())

    assert set(manager._running) == {"a", "c"}
    assert list(manager._pending) == ["b"]
    manager._executor.shutdown()


def test_cancel_queued_operation_is_terminal_immediately():
    record = OperationRecord(
        "op-cancel",
        "test.cancel",
        "1",
        ["env:1:x"],
        {},
        state="queued",
    )
    manager = OperationManager(Settings())
    manager._kv = FakeKV([record])
    manager._pending[record.operation_id] = (FakeMsg(), record)

    result = asyncio.run(manager._cancel_async(record.operation_id, "1"))

    assert result["state"] == "cancelled"
    assert result["completed_at"] is not None
    assert (
        OperationRecord.from_bytes(manager._kv.values[record.operation_id]).state
        == "cancelled"
    )
    manager._executor.shutdown()


def test_cleanup_uses_terminal_completion_time_and_removes_output():
    now = time.time()
    expired = OperationRecord(
        "expired",
        "test",
        "1",
        [],
        {},
        state="succeeded",
        completed_at=now - 4000,
        result_object="expired.json",
    )
    active = OperationRecord(
        "active",
        "test",
        "1",
        [],
        {},
        state="queued",
        created_at=now - 10000,
    )
    manager = OperationManager(Settings(operation_retention_seconds=3600))
    manager._kv = FakeKV([expired, active])
    manager._objects = FakeObjects()

    asyncio.run(manager._cleanup_expired())

    assert manager._kv.purged == [("expired", 60)]
    assert manager._kv.purge_deletes_calls == [60]
    assert manager._objects.deleted == ["expired.json"]
    assert "active" in manager._kv.values
    manager._executor.shutdown()


def test_cleanup_removes_orphaned_output_when_operation_bucket_is_empty():
    manager = OperationManager(Settings())
    manager._kv = FakeKV()
    manager._objects = FakeObjects()
    manager._objects.values["orphan.output.json"] = b'"output"'

    asyncio.run(manager._cleanup_expired())

    assert manager._objects.deleted == ["orphan.output.json"]
    manager._executor.shutdown()


def test_startup_reconcile_resumes_tracked_exec_and_interrupts_other_work():
    submitting = OperationRecord("submit", "test", "1", [], {})
    interrupted = OperationRecord("plain-running", "test", "1", [], {}, state="running")
    attachable = OperationRecord(
        "exec-running",
        "test",
        "1",
        [],
        {},
        state="running",
        runtime_ref={"type": "docker_exec", "exec_id": "exec-1"},
    )
    manager = OperationManager(Settings())
    manager._kv = FakeKV([submitting, interrupted, attachable])
    manager._js = FakeJS()
    manager._docker_exec_is_attachable = (  # type: ignore[method-assign]
        lambda runtime: runtime.get("exec_id") == "exec-1"
    )

    asyncio.run(manager._reconcile())

    records = {
        key: OperationRecord.from_bytes(value)
        for key, value in manager._kv.values.items()
    }
    assert records["submit"].state == "queued"
    assert records["plain-running"].state == "interrupted"
    assert records["plain-running"].completed_at is not None
    assert records["exec-running"].state == "queued"
    assert len(manager._js.published) == 1
    manager._executor.shutdown()


def test_full_output_is_stored_separately_from_summary():
    record = OperationRecord(
        "op-output",
        "test.output",
        "1",
        ["env:1:x"],
        {},
        state="queued",
    )
    message = FakeMsg()
    manager = OperationManager(Settings())
    manager._kv = FakeKV([record])
    manager._objects = FakeObjects()
    manager._pending[record.operation_id] = (message, record)

    def handler():
        report_current_output("the complete log")
        return "short summary"

    register_operation("test.output", handler, lambda _args, _team: ())
    asyncio.run(manager._schedule())
    manager._running[record.operation_id][2].result(timeout=2)
    asyncio.run(manager._collect_completions())
    _inline_calls(manager)

    stored = OperationRecord.from_bytes(manager._kv.values[record.operation_id])
    assert stored.result == "short summary"
    assert stored.output_object == "op-output.output.json"
    assert manager.read_output(record.operation_id, team_id="1") == "the complete log"
    assert message.acked is True
    manager._executor.shutdown()


def test_running_tracked_exec_exposes_live_output():
    record = OperationRecord(
        "op-live",
        "test",
        "1",
        [],
        {},
        state="running",
        runtime_ref={
            "type": "docker_exec",
            "container_id": "container-1",
            "outputfile": "/tmp/live.log",
        },
    )
    manager = OperationManager(Settings())
    manager._kv = FakeKV([record])
    _inline_calls(manager)
    container = Mock()
    container.exec_run.return_value = (0, b"partial output")
    client = Mock()
    client.containers.get.return_value = container

    with patch("oduflow.docker_ops.client.get_client", return_value=client):
        output = manager.read_output(record.operation_id, team_id="1")

    assert output == "partial output"
    container.exec_run.assert_called_once_with(["cat", "/tmp/live.log"])
    manager._executor.shutdown()


def test_operation_decorator_exposes_wait_and_submits_named_resource():
    fake_manager = Mock(started=True)
    fake_manager.submit.return_value = {
        "operation_id": "server-generated",
        "state": "queued",
    }
    team = TeamSettings(team_id="7")

    @operation(static_resource("env", "env_name"), kind="test.decorator")
    def mutate(env_name: str, value: int = 1, ctx=None):
        return {"env_name": env_name, "value": value}

    with (
        patch("oduflow.server._get_settings", return_value=Settings()),
        patch("oduflow.server._resolve_team", return_value=team),
        patch("oduflow.operations.get_operation_manager", return_value=fake_manager),
    ):
        result = mutate("feature", value=2, wait=False)

    assert result["operation_id"] == "server-generated"
    fake_manager.submit.assert_called_once_with(
        "test.decorator",
        "7",
        {"env_name": "feature", "value": 2},
        ["env:7:feature"],
        wait=False,
    )


def test_managed_secret_is_stable_and_private(tmp_path):
    from oduflow.nats_runtime import _load_or_create_secrets

    settings = Settings(etc_dir=str(tmp_path))
    first, first_key = _load_or_create_secrets(settings)
    second, second_key = _load_or_create_secrets(settings)
    path = tmp_path / "nats-secrets.json"

    assert first == second
    assert first_key == second_key
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_managed_nats_mounts_relative_config_as_absolute_host_path(
    tmp_path, monkeypatch
):
    from oduflow.nats_runtime import ensure_nats

    monkeypatch.chdir(tmp_path)
    settings = Settings(
        etc_dir="relative-etc",
        shared_network="test-net",
        nats_container="test-nats",
        nats_volume="test-nats-data",
    )
    client = MagicMock()
    client.volumes.get.side_effect = docker.errors.NotFound("missing")
    client.containers.get.side_effect = docker.errors.NotFound("missing")
    container = MagicMock()
    container.status = "running"
    container.logs.return_value = b"[INF] Server is ready"
    client.containers.run.return_value = container

    ensure_nats(client, settings, {"oduflow.managed": "true"})

    volumes = client.containers.run.call_args.kwargs["volumes"]
    config_path = str((tmp_path / "relative-etc" / "nats.conf").resolve())
    assert volumes[config_path] == {
        "bind": "/etc/nats/nats.conf",
        "mode": "ro",
    }
