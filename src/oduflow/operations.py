"""Durable, resource-aware operation queue backed by local NATS JetStream."""

from __future__ import annotations

import asyncio
import builtins
import contextvars
import functools
import inspect
import json
import logging
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any, TypeVar, cast

import nats
from nats.aio.msg import Msg
from nats.js import api
from nats.js.errors import (
    BucketNotFoundError,
    KeyNotFoundError,
    NoKeysError,
    ObjectNotFoundError,
)
from nats.js.errors import (
    NotFoundError as JetStreamNotFoundError,
)

from oduflow.errors import (
    FlowError,
    NotFoundError,
    PrerequisiteNotMetError,
)
from oduflow.nats_runtime import connection_urls, load_credentials
from oduflow.settings import Settings

logger = logging.getLogger("oduflow")

_STREAM = "ODUFLOW_JOB_COMMANDS"
_SUBJECT = "oduflow.jobs.execute"
_CONSUMER = "ODUFLOW_WORKER"
_OPERATIONS_BUCKET = "ODUFLOW_OPERATIONS"
_OUTPUT_BUCKET = "ODUFLOW_OPERATION_OUTPUTS"
_LARGE_RESULT_BYTES = 64 * 1024
_TERMINAL_STATES = {"succeeded", "failed", "cancelled", "interrupted"}

_current_team_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "oduflow_operation_team_id", default=None
)
_current_operation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "oduflow_operation_id", default=None
)
_current_cancel_event: contextvars.ContextVar[threading.Event | None] = (
    contextvars.ContextVar("oduflow_operation_cancel_event", default=None)
)
_current_full_output: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "oduflow_operation_full_output", default=None
)

T = TypeVar("T")
OperationHandler = Callable[..., Any]
ResourceResolver = Callable[[Mapping[str, Any], str], Iterable[str]]


def current_team_id() -> str | None:
    return _current_team_id.get()


def current_operation_id() -> str | None:
    return _current_operation_id.get()


def cancellation_requested() -> bool:
    event = _current_cancel_event.get()
    return bool(event and event.is_set())


def current_cancel_event() -> threading.Event | None:
    return _current_cancel_event.get()


def report_current_output(output: str) -> None:
    """Attach full text output to the current durable operation."""
    if current_operation_id() is not None:
        _current_full_output.set(output)


def cancellation_checkpoint() -> None:
    if cancellation_requested():
        raise OperationCancelled("Operation cancellation requested.")


class OperationCancelled(Exception):
    pass


@dataclass
class OperationRecord:
    operation_id: str
    kind: str
    team_id: str
    resources: list[str]
    arguments: dict[str, Any]
    state: str = "submitting"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    result: Any = None
    result_object: str = ""
    output_object: str = ""
    error: str = ""
    cancel_requested: bool = False
    coalesce_key: str = ""
    runtime_ref: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "OperationRecord":
        return cls(**json.loads(raw.decode("utf-8")))

    def to_bytes(self) -> bytes:
        return json.dumps(
            asdict(self), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

    def public(self, *, include_result: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "operation_id": self.operation_id,
            "kind": self.kind,
            "state": self.state,
            "resources": list(self.resources),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "cancel_requested": self.cancel_requested,
        }
        if self.started_at is not None and self.completed_at is None:
            data["elapsed_seconds"] = max(0, round(time.time() - self.started_at, 1))
        elif self.started_at is not None and self.completed_at is not None:
            data["elapsed_seconds"] = max(
                0, round(self.completed_at - self.started_at, 1)
            )
        if self.error:
            data["error"] = self.error
        if include_result and not self.result_object:
            data["result"] = self.result
        elif self.result_object:
            data["output_available"] = True
        if self.output_object:
            data["output_available"] = True
        return data


@dataclass(frozen=True)
class RegisteredOperation:
    handler: OperationHandler
    resources: ResourceResolver


_registry: dict[str, RegisteredOperation] = {}


def register_operation(
    kind: str, handler: OperationHandler, resources: ResourceResolver
) -> None:
    existing = _registry.get(kind)
    if existing and existing.handler is not handler:
        raise RuntimeError(f"Operation kind already registered: {kind}")
    _registry[kind] = RegisteredOperation(handler, resources)


def static_resource(resource_type: str, argument: str) -> ResourceResolver:
    def resolve(arguments: Mapping[str, Any], team_id: str) -> Iterable[str]:
        value = str(arguments.get(argument, "")).strip()
        if not value:
            raise ValueError(f"{argument} is required")
        return [f"{resource_type}:{team_id}:{value}"]

    return resolve


def resource_set(*resolvers: ResourceResolver) -> ResourceResolver:
    def resolve(arguments: Mapping[str, Any], team_id: str) -> Iterable[str]:
        resources: set[str] = set()
        for resolver in resolvers:
            resources.update(resolver(arguments, team_id))
        return sorted(resources)

    return resolve


class OperationManager:
    """One embedded worker with a durable JetStream command queue."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._accepting = True
        self._draining = False
        self._force_stop = False
        self._stop_requested = threading.Event()
        self._events: dict[str, threading.Event] = {}
        self._events_lock = threading.Lock()
        self._submit_lock = threading.Lock()
        self._cancel_events: dict[str, threading.Event] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=settings.operation_max_workers,
            thread_name_prefix="oduflow-job",
        )

        self._nc: Any = None
        self._js: Any = None
        self._kv: Any = None
        self._objects: Any = None
        self._sub: Any = None
        self._pending: OrderedDict[str, tuple[Msg, OperationRecord]] = OrderedDict()
        self._running: dict[
            str,
            tuple[
                Msg,
                OperationRecord,
                Future[tuple[str, Any, str, str | None]],
            ],
        ] = {}
        self._owned_resources: dict[str, str] = {}
        self._last_heartbeat = 0.0
        self._last_cleanup = 0.0

    @property
    def accepting(self) -> bool:
        return self._accepting

    @property
    def draining(self) -> bool:
        return self._draining

    @property
    def started(self) -> bool:
        return bool(
            self._thread
            and self._thread.is_alive()
            and self._ready.is_set()
            and self._startup_error is None
        )

    def start(self, timeout: float = 20.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ready.clear()
        self._startup_error = None
        self._accepting = True
        self._draining = False
        self._force_stop = False
        self._stop_requested.clear()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="oduflow-operation-manager",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            raise PrerequisiteNotMetError(
                "Timed out connecting to the managed NATS operation queue."
            )
        if self._startup_error is not None:
            raise PrerequisiteNotMetError(
                f"Cannot start the NATS operation queue: {self._startup_error}"
            ) from self._startup_error

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run())
        except BaseException as exc:  # startup/result is forwarded to main thread
            self._startup_error = exc
            logger.exception("Operation manager stopped unexpectedly")
            self._ready.set()

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        credentials = load_credentials(self.settings)
        self._nc = await nats.connect(
            servers=connection_urls(self.settings),
            user=credentials.username,
            password=credentials.password,
            connect_timeout=2,
            max_reconnect_attempts=2,
            reconnect_time_wait=1,
        )
        # Bound initial startup failure, but keep an established worker alive
        # across arbitrary managed-NATS restarts.
        self._nc.options["max_reconnect_attempts"] = -1
        self._js = self._nc.jetstream(timeout=10)
        await self._ensure_storage()
        await self._reconcile()
        self._sub = await self._js.pull_subscribe(
            _SUBJECT,
            durable=_CONSUMER,
            stream=_STREAM,
            config=api.ConsumerConfig(
                durable_name=_CONSUMER,
                ack_policy=api.AckPolicy.EXPLICIT,
                ack_wait=60,
                max_ack_pending=max(64, self.settings.operation_max_workers * 16),
            ),
        )
        self._ready.set()
        logger.info(
            "Operation manager ready (workers=%d)", self.settings.operation_max_workers
        )

        try:
            while not self._stop_requested.is_set():
                await self._collect_completions()
                if not self._draining:
                    await self._fetch_commands()
                    await self._schedule()
                await self._heartbeat()
                await self._cleanup_expired()
                if self._draining and not self._running:
                    break
                await asyncio.sleep(0.05)
        finally:
            await self._shutdown_async()

    async def _ensure_storage(self) -> None:
        try:
            info = await self._js.stream_info(_STREAM)
            config = info.config.evolve(
                subjects=[_SUBJECT],
                retention=api.RetentionPolicy.WORK_QUEUE,
                storage=api.StorageType.FILE,
                num_replicas=1,
                allow_msg_ttl=False,
            )
            await self._js.update_stream(config)
        except JetStreamNotFoundError:
            await self._js.add_stream(
                name=_STREAM,
                subjects=[_SUBJECT],
                retention=api.RetentionPolicy.WORK_QUEUE,
                storage=api.StorageType.FILE,
                num_replicas=1,
                allow_msg_ttl=False,
            )

        try:
            self._kv = await self._js.key_value(_OPERATIONS_BUCKET)
        except BucketNotFoundError:
            self._kv = await self._js.create_key_value(
                config=api.KeyValueConfig(
                    bucket=_OPERATIONS_BUCKET,
                    history=1,
                    storage=api.StorageType.FILE,
                    replicas=1,
                )
            )
        try:
            self._objects = await self._js.object_store(_OUTPUT_BUCKET)
        except BucketNotFoundError:
            self._objects = await self._js.create_object_store(
                bucket=_OUTPUT_BUCKET,
                config=api.ObjectStoreConfig(
                    bucket=_OUTPUT_BUCKET,
                    storage=api.StorageType.FILE,
                    replicas=1,
                ),
            )

    async def _reconcile(self) -> None:
        try:
            keys = await self._kv.keys()
        except NoKeysError:
            keys = []
        for key in keys:
            try:
                record = await self._get_async(key)
            except NotFoundError:
                continue
            if record.state == "submitting":
                await self._publish(record)
                record.state = "queued"
                await self._put_async(record)
            elif record.state in {"running", "cancel_requested"}:
                if self._docker_exec_is_attachable(record.runtime_ref):
                    # The unacked command is redelivered and the handler's
                    # cancellable Docker helper attaches to this exec instead
                    # of starting it again.
                    record.state = "queued"
                    record.cancel_requested = False
                    record.error = ""
                else:
                    record.state = "interrupted"
                    record.completed_at = time.time()
                    record.error = "Oduflow restarted while the operation was running."
                await self._put_async(record)

    @staticmethod
    def _docker_exec_is_attachable(runtime_ref: Mapping[str, str]) -> bool:
        if runtime_ref.get("type") != "docker_exec" or not runtime_ref.get("exec_id"):
            return False
        try:
            from oduflow.docker_ops.client import get_client

            get_client().api.exec_inspect(runtime_ref["exec_id"])
            return True
        except Exception:
            return False

    async def _fetch_commands(self) -> None:
        if len(self._pending) >= max(64, self.settings.operation_max_workers * 16):
            return
        try:
            messages = await self._sub.fetch(batch=16, timeout=0.1)
        except (nats.errors.TimeoutError, asyncio.TimeoutError):
            return
        for msg in messages:
            try:
                payload = json.loads(msg.data.decode("utf-8"))
                operation_id = str(payload["operation_id"])
                record = await self._get_async(operation_id)
            except Exception:
                logger.exception("Discarding invalid operation command")
                await msg.term()
                continue
            if record.state in _TERMINAL_STATES:
                await msg.ack()
                self._signal(operation_id)
                continue
            if operation_id in self._pending or operation_id in self._running:
                await msg.ack()
                continue
            self._pending[operation_id] = (msg, record)

    async def _schedule(self) -> None:
        available = self.settings.operation_max_workers - len(self._running)
        if available <= 0:
            return
        reserved_by_older: set[str] = set()
        for operation_id, (msg, record) in list(self._pending.items()):
            if available <= 0:
                break
            if record.state in _TERMINAL_STATES:
                await msg.ack()
                self._pending.pop(operation_id, None)
                self._signal(operation_id)
                continue
            conflicts = any(
                resource in self._owned_resources or resource in reserved_by_older
                for resource in record.resources
            )
            if conflicts:
                reserved_by_older.update(record.resources)
                continue
            for resource in record.resources:
                self._owned_resources[resource] = operation_id
            self._pending.pop(operation_id, None)
            record.state = "running"
            record.started_at = time.time()
            await self._put_async(record)
            cancel_event = threading.Event()
            self._cancel_events[operation_id] = cancel_event
            future = self._executor.submit(self._execute, record, cancel_event)
            self._running[operation_id] = (msg, record, future)
            available -= 1

    def _execute(
        self, record: OperationRecord, cancel_event: threading.Event
    ) -> tuple[str, Any, str, str | None]:
        registered = _registry.get(record.kind)
        if registered is None:
            return "failed", None, f"Unknown operation kind: {record.kind}", None
        team_token = _current_team_id.set(record.team_id)
        operation_token = _current_operation_id.set(record.operation_id)
        cancel_token = _current_cancel_event.set(cancel_event)
        output_token = _current_full_output.set(None)
        try:
            cancellation_checkpoint()
            result = registered.handler(**record.arguments)
            cancellation_checkpoint()
            return "succeeded", result, "", _current_full_output.get()
        except OperationCancelled:
            return (
                "cancelled",
                None,
                "Operation cancelled.",
                _current_full_output.get(),
            )
        except (FlowError, ValueError) as exc:
            return "failed", None, str(exc), _current_full_output.get()
        except Exception:
            logger.error(
                "Unhandled operation failure %s:\n%s",
                record.operation_id,
                traceback.format_exc(),
            )
            return (
                "failed",
                None,
                "Internal operation error. Check Oduflow logs.",
                _current_full_output.get(),
            )
        finally:
            _current_full_output.reset(output_token)
            _current_cancel_event.reset(cancel_token)
            _current_operation_id.reset(operation_token)
            _current_team_id.reset(team_token)

    async def _collect_completions(self) -> None:
        for operation_id, (msg, record, future) in list(self._running.items()):
            if not future.done():
                continue
            state, result, error, full_output = future.result()
            record.state = state
            record.completed_at = time.time()
            record.error = error
            if result is not None:
                try:
                    encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
                except (TypeError, ValueError):
                    logger.error(
                        "Operation %s returned a non-JSON-serializable result",
                        operation_id,
                    )
                    record.state = "failed"
                    record.error = (
                        "Operation returned an unsupported result. Check Oduflow logs."
                    )
                    encoded = b""
                if len(encoded) > _LARGE_RESULT_BYTES:
                    object_name = f"{operation_id}.json"
                    await self._objects.put(object_name, encoded)
                    record.result_object = object_name
                    record.result = None
                elif encoded:
                    record.result = result
            if full_output is not None:
                object_name = f"{operation_id}.output.json"
                await self._objects.put(
                    object_name,
                    json.dumps(full_output, ensure_ascii=False).encode("utf-8"),
                )
                record.output_object = object_name
            await self._put_async(record)
            await msg.ack()
            for resource in record.resources:
                if self._owned_resources.get(resource) == operation_id:
                    self._owned_resources.pop(resource, None)
            self._cancel_events.pop(operation_id, None)
            self._running.pop(operation_id, None)
            self._signal(operation_id)

    async def _heartbeat(self) -> None:
        now = time.monotonic()
        if now - self._last_heartbeat < 20:
            return
        self._last_heartbeat = now
        for msg, _record in list(self._pending.values()):
            try:
                await msg.in_progress()
            except Exception:
                logger.debug("Pending job heartbeat failed", exc_info=True)
        for msg, _record, _future in list(self._running.values()):
            try:
                await msg.in_progress()
            except Exception:
                logger.debug("Running job heartbeat failed", exc_info=True)

    async def _cleanup_expired(self) -> None:
        now_mono = time.monotonic()
        if now_mono - self._last_cleanup < 60:
            return
        self._last_cleanup = now_mono
        cutoff = time.time() - self.settings.operation_retention_seconds
        try:
            keys = await self._kv.keys()
        except NoKeysError:
            keys = []
        for key in keys:
            try:
                record = await self._get_async(key)
            except NotFoundError:
                continue
            if (
                record.state not in _TERMINAL_STATES
                or record.completed_at is None
                or record.completed_at > cutoff
            ):
                continue
            for object_name in (record.result_object, record.output_object):
                if object_name:
                    try:
                        await self._objects.delete(object_name)
                    except (ObjectNotFoundError, JetStreamNotFoundError):
                        pass
            # KV purge uses a short-lived marker so an older revision cannot
            # reappear; remove those implementation markers on the next pass.
            # This is not a user-visible operation tombstone or history record.
            await self._kv.purge(record.operation_id, msg_ttl=60)
            with self._events_lock:
                self._events.pop(record.operation_id, None)
        await self._kv.purge_deletes(olderthan=60)
        # A crash between Object Store put and the terminal KV update can leave
        # an orphan. Object names begin with the operation UUID, so remove any
        # object whose authoritative operation record no longer exists.
        try:
            objects = await self._objects.list(ignore_deletes=True)
        except (ObjectNotFoundError, JetStreamNotFoundError):
            objects = []
        for info in objects:
            operation_id = str(info.name).split(".", 1)[0]
            try:
                await self._get_async(operation_id)
            except NotFoundError:
                try:
                    await self._objects.delete(info.name)
                except (ObjectNotFoundError, JetStreamNotFoundError):
                    pass

    async def _shutdown_async(self) -> None:
        for msg, _record in list(self._pending.values()):
            try:
                await msg.nak()
            except Exception:
                pass
        self._pending.clear()
        if self._nc is not None:
            try:
                await self._nc.drain()
            except Exception:
                await self._nc.close()
        self._executor.shutdown(wait=not self._force_stop, cancel_futures=True)
        logger.info("Operation manager stopped")

    async def _publish(self, record: OperationRecord) -> None:
        await self._js.publish(
            _SUBJECT,
            json.dumps({"operation_id": record.operation_id}).encode("utf-8"),
            headers={"Nats-Msg-Id": record.operation_id},
        )

    async def _put_async(self, record: OperationRecord) -> None:
        await self._kv.put(record.operation_id, record.to_bytes())

    async def _get_async(self, operation_id: str) -> OperationRecord:
        try:
            entry = await self._kv.get(operation_id)
        except KeyNotFoundError as exc:
            raise NotFoundError(f"Operation '{operation_id}' was not found.") from exc
        if entry.value is None:
            raise NotFoundError(f"Operation '{operation_id}' was not found.")
        return OperationRecord.from_bytes(entry.value)

    def _call(self, coroutine: Any, timeout: float = 15.0) -> Any:
        try:
            self.start()
        except Exception:
            close = getattr(coroutine, "close", None)
            if close is not None:
                close()
            raise
        if self._loop is None:
            raise PrerequisiteNotMetError("Operation manager is not running.")
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout=timeout)

    def submit(
        self,
        kind: str,
        team_id: str,
        arguments: Mapping[str, Any],
        resources: Iterable[str],
        *,
        wait: bool,
        coalesce_key: str = "",
    ) -> Any:
        with self._submit_lock:
            if not self._accepting:
                raise PrerequisiteNotMetError(
                    "Oduflow is draining; new mutating operations are not accepted."
                )
            if coalesce_key:
                existing = cast(
                    OperationRecord | None,
                    self._call(self._find_coalesced_async(team_id, kind, coalesce_key)),
                )
                if existing is not None:
                    ticket = existing.public()
                    ticket["coalesced"] = True
                    return ticket
            operation_id = str(uuid.uuid4())
            record = OperationRecord(
                operation_id=operation_id,
                kind=kind,
                team_id=team_id,
                resources=sorted(set(resources)),
                arguments=dict(arguments),
                coalesce_key=coalesce_key,
            )
            event = threading.Event()
            with self._events_lock:
                self._events[operation_id] = event
            self._call(self._submit_async(record))
        if not wait:
            return record.public()
        if event.wait(self.settings.operation_wait_timeout_seconds):
            completed = self.get(operation_id, include_result=True)
            if completed["state"] == "succeeded":
                return completed.get("result")
            return completed
        return self.get(operation_id)

    async def _find_coalesced_async(
        self, team_id: str, kind: str, coalesce_key: str
    ) -> OperationRecord | None:
        try:
            keys = await self._kv.keys()
        except NoKeysError:
            return None
        for key in keys:
            try:
                record = await self._get_async(key)
            except NotFoundError:
                continue
            if (
                record.team_id != team_id
                or record.kind != kind
                or record.coalesce_key != coalesce_key
                or record.state in _TERMINAL_STATES
            ):
                continue
            if record.state in {"submitting", "queued"}:
                return record
        # A running job deliberately does not coalesce the first subsequent
        # request: that one becomes the single "latest desired state" job.
        return None

    async def _submit_async(self, record: OperationRecord) -> None:
        await self._put_async(record)
        await self._publish(record)
        record.state = "queued"
        await self._put_async(record)

    def get(
        self,
        operation_id: str,
        *,
        include_result: bool = False,
        team_id: str | None = None,
    ) -> dict[str, Any]:
        record = cast(OperationRecord, self._call(self._get_async(operation_id)))
        if team_id is not None and record.team_id != team_id:
            raise NotFoundError(f"Operation '{operation_id}' was not found.")
        data = record.public(include_result=include_result)
        if include_result and record.result_object:
            data["result"] = self._call(self._read_object(record.result_object))
        return data

    def read_output(self, operation_id: str, *, team_id: str | None = None) -> Any:
        record = cast(OperationRecord, self._call(self._get_async(operation_id)))
        if team_id is not None and record.team_id != team_id:
            raise NotFoundError(f"Operation '{operation_id}' was not found.")
        if record.output_object:
            return self._call(self._read_object(record.output_object))
        if record.result_object:
            return self._call(self._read_object(record.result_object))
        if (
            record.runtime_ref.get("type") == "docker_exec"
            and record.runtime_ref.get("container_id")
            and record.runtime_ref.get("outputfile")
        ):
            try:
                from oduflow.docker_ops.client import get_client

                container = get_client().containers.get(
                    record.runtime_ref["container_id"]
                )
                result = container.exec_run(["cat", record.runtime_ref["outputfile"]])
                code = result.exit_code if hasattr(result, "exit_code") else result[0]
                output = result.output if hasattr(result, "output") else result[1]
                if int(code) == 0:
                    return (
                        output.decode("utf-8", errors="replace")
                        if isinstance(output, bytes)
                        else str(output)
                    )
            except Exception:
                logger.debug(
                    "Could not read live Docker operation output",
                    exc_info=True,
                )
        return record.result

    async def _read_object(self, name: str) -> Any:
        try:
            result = await self._objects.get(name)
        except ObjectNotFoundError as exc:
            raise NotFoundError("Operation output has expired.") from exc
        return json.loads(result.data.decode("utf-8"))

    def list(self, team_id: str, limit: int = 50) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self._call(self._list_async(team_id, limit))
        return result

    async def _list_async(
        self, team_id: str, limit: int
    ) -> builtins.list[dict[str, Any]]:
        try:
            keys = await self._kv.keys()
        except NoKeysError:
            return []
        records: builtins.list[OperationRecord] = []
        for key in keys:
            try:
                record = await self._get_async(key)
            except NotFoundError:
                continue
            if record.team_id == team_id:
                records.append(record)
        records.sort(key=lambda item: item.created_at, reverse=True)
        return [record.public() for record in records[: max(1, min(limit, 200))]]

    def wait(
        self,
        operation_id: str,
        timeout_seconds: int = 90,
        *,
        team_id: str | None = None,
    ) -> dict[str, Any]:
        record = cast(OperationRecord, self._call(self._get_async(operation_id)))
        if team_id is not None and record.team_id != team_id:
            raise NotFoundError(f"Operation '{operation_id}' was not found.")
        if record.state in _TERMINAL_STATES:
            return self.get(operation_id, include_result=True, team_id=team_id)
        with self._events_lock:
            event = self._events.setdefault(operation_id, threading.Event())
        event.wait(
            max(
                0,
                min(
                    timeout_seconds,
                    self.settings.operation_wait_timeout_seconds,
                ),
            )
        )
        return self.get(operation_id, include_result=True, team_id=team_id)

    def cancel(self, operation_id: str, team_id: str) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._call(self._cancel_async(operation_id, team_id)),
        )

    async def _cancel_async(self, operation_id: str, team_id: str) -> dict[str, Any]:
        record = await self._get_async(operation_id)
        if record.team_id != team_id:
            raise NotFoundError(f"Operation '{operation_id}' was not found.")
        if record.state in _TERMINAL_STATES:
            return record.public()
        record.cancel_requested = True
        if record.state in {"submitting", "queued"}:
            record.state = "cancelled"
            record.completed_at = time.time()
            record.error = "Operation cancelled before it started."
            await self._put_async(record)
            pending = self._pending.get(operation_id)
            if pending is not None:
                self._pending[operation_id] = (pending[0], record)
            self._signal(operation_id)
            return record.public()
        record.state = "cancel_requested"
        await self._put_async(record)
        event = self._cancel_events.get(operation_id)
        if event:
            event.set()
        return record.public()

    def _signal(self, operation_id: str) -> None:
        with self._events_lock:
            event = self._events.get(operation_id)
        if event:
            event.set()

    def begin_drain(self, *, force: bool = False) -> None:
        self._accepting = False
        self._draining = True
        self._force_stop = force
        if force:
            self._stop_requested.set()

    def drain(self, *, force: bool = False, timeout: float | None = None) -> bool:
        self.begin_drain(force=force)
        thread = self._thread
        if thread is None:
            return True
        if force:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def report_runtime_ref(
        self, operation_id: str, runtime_ref: Mapping[str, str]
    ) -> None:
        self._call(self._report_runtime_ref_async(operation_id, runtime_ref))

    async def _report_runtime_ref_async(
        self, operation_id: str, runtime_ref: Mapping[str, str]
    ) -> None:
        record = await self._get_async(operation_id)
        if record.state not in _TERMINAL_STATES:
            record.runtime_ref = dict(runtime_ref)
            await self._put_async(record)


_manager: OperationManager | None = None
_manager_lock = threading.Lock()


def get_operation_manager(settings: Settings) -> OperationManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = OperationManager(settings)
        return _manager


def reset_operation_manager_for_tests() -> None:
    global _manager
    with _manager_lock:
        _manager = None


def report_current_runtime_ref(runtime_ref: Mapping[str, str]) -> None:
    operation_id = current_operation_id()
    if operation_id is None:
        return
    with _manager_lock:
        manager = _manager
    if manager is not None:
        manager.report_runtime_ref(operation_id, runtime_ref)


def get_current_runtime_ref() -> dict[str, str]:
    operation_id = current_operation_id()
    if operation_id is None:
        return {}
    with _manager_lock:
        manager = _manager
    if manager is None:
        return {}
    try:
        record = cast(OperationRecord, manager._call(manager._get_async(operation_id)))
    except Exception:
        return {}
    return dict(record.runtime_ref)


def operation(
    resources: ResourceResolver,
    *,
    kind: str | None = None,
    when: Callable[[Mapping[str, Any]], bool] | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Register a synchronous handler and expose it as a job-submitting wrapper.

    The wrapper adds a keyword-only ``wait`` argument to the MCP schema without
    forcing every handler to carry queue plumbing in its business signature.
    """

    def decorate(fn: Callable[..., T]) -> Callable[..., T]:
        operation_kind = kind or fn.__name__
        register_operation(operation_kind, fn, resources)
        signature = inspect.signature(fn)
        if "wait" in signature.parameters:
            raise RuntimeError(
                f"{fn.__name__} already has a 'wait' parameter; rename it before "
                "applying @operation"
            )

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            wait = bool(kwargs.pop("wait", True))
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            context = bound.arguments.pop("ctx", None)

            # Import lazily to keep the queue independent of FastMCP/server
            # initialization and to preserve the existing request auth routing.
            from oduflow.server import _get_settings, _resolve_team

            settings = _get_settings()
            team = _resolve_team(context)
            payload = {
                name: value for name, value in bound.arguments.items() if name != "ctx"
            }
            if when is not None and not when(payload):
                return fn(**payload, ctx=context)
            resolved = sorted(set(resources(payload, team.team_id)))
            manager = get_operation_manager(settings)
            # The server owns the operation manager lifecycle and starts it
            # before accepting MCP/HTTP traffic. Keeping direct function calls
            # synchronous is useful for maintenance commands and unit tests,
            # and avoids silently creating a second queue consumer outside the
            # server process.
            if not manager.started:
                return fn(**payload, ctx=context)
            return manager.submit(
                operation_kind,
                team.team_id,
                payload,
                resolved,
                wait=wait,
            )

        parameters = list(signature.parameters.values())
        insert_at = next(
            (
                index
                for index, parameter in enumerate(parameters)
                if parameter.name == "ctx"
            ),
            len(parameters),
        )
        parameters.insert(
            insert_at,
            inspect.Parameter(
                "wait",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=True,
                annotation=bool,
            ),
        )
        wrapper.__signature__ = signature.replace(  # type: ignore[attr-defined]
            parameters=parameters,
            return_annotation=Any,
        )
        wrapper.__annotations__ = {
            **getattr(fn, "__annotations__", {}),
            "wait": bool,
            "return": Any,
        }
        wrapper.__doc__ = (
            (fn.__doc__ or "").rstrip()
            + "\n\n"
            + "Set wait=false to return an operation ticket immediately. With "
            "wait=true, Oduflow returns the result when it finishes within the "
            "configured wait timeout, otherwise it returns the same ticket."
        )
        return cast(Callable[..., T], wrapper)

    return decorate
