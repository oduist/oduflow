"""Fail-fast watchdog for the server's startup phase.

Startup runs migrations, ``init_system`` and quota application *before* the HTTP
server binds, so anything that blocks in there leaves a process that is alive
but serves nothing: no ``/healthz``, no ``/mcp``, no dashboard. systemd cannot
help — a ``Type=simple`` unit counts as started the moment it is exec'd, so
``TimeoutStartSec`` never applies and ``Restart=`` never fires for a process
that does not exit.

That failure mode is not hypothetical. An ``unattended-upgrades`` run once
restarted Oduflow in the same batch as ``containerd``; a Docker call during
``init_system`` never returned and the deployment was down for four and a half
hours, until a manual restart brought it up in four seconds. Docker calls are
the standing hazard here because docker-py's exec/attach paths explicitly
disable the socket timeout (``APIClient._disable_socket_timeout``), so a wedged
daemon blocks the caller forever rather than raising.

The watchdog treats *log silence* as the stall signal: every record emitted on
the ``oduflow`` logger is a heartbeat, so a slow-but-progressing start (a large
``docker pull``, a template restore) keeps it satisfied, while a wedged call
does not. On a stall it dumps every thread's stack to the journal — so the next
occurrence is diagnosable instead of invisible — and exits non-zero so systemd
restarts the unit.
"""

from __future__ import annotations

import contextlib
import faulthandler
import logging
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator

logger = logging.getLogger("oduflow")

# How long startup may go without emitting a single log record before it is
# considered wedged. Deliberately generous: a first start on a fresh host pulls
# the PostgreSQL, Traefik and coder images, and those pulls are silent.
DEFAULT_STALL_SECONDS = 900.0

# How often the watchdog thread re-checks. Small relative to the stall window,
# so the reported silence is accurate without busy-waiting.
DEFAULT_POLL_SECONDS = 15.0

# Emergency valve, not a tuning knob: if a future startup step is ever silent
# for longer than the window, the watchdog would kill every restart the same way
# and no restart could ever finish. This lets an operator widen the window (or
# set 0 to switch the watchdog off) to get the host back up without waiting for
# a release.
STALL_ENV_VAR = "ODUFLOW_STARTUP_STALL_SECONDS"


class _HeartbeatHandler(logging.Handler):
    """Turns every log record into a heartbeat for its watchdog."""

    def __init__(self, watchdog: StartupWatchdog) -> None:
        super().__init__(level=logging.NOTSET)
        self._watchdog = watchdog

    def emit(self, record: logging.LogRecord) -> None:
        self._watchdog.beat()


class StartupWatchdog:
    """Aborts the process when the startup phase stops making visible progress.

    ``on_stall`` is injected so tests can observe the decision without the
    default's process-level side effects.
    """

    def __init__(
        self,
        *,
        stall_seconds: float = DEFAULT_STALL_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        on_stall: Callable[[float], None] | None = None,
    ) -> None:
        self.stall_seconds = stall_seconds
        # Never coarser than a quarter of the window, so a narrowed window (the
        # env valve, or a test) is still noticed promptly.
        self.poll_seconds = min(poll_seconds, max(1.0, stall_seconds / 4))
        self._on_stall = on_stall or _abort
        self._last_beat = time.monotonic()
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._handler: _HeartbeatHandler | None = None

    def beat(self) -> None:
        with self._lock:
            self._last_beat = time.monotonic()

    def silence(self) -> float:
        """Seconds since the last heartbeat."""
        with self._lock:
            return time.monotonic() - self._last_beat

    def check(self) -> bool:
        """Fire ``on_stall`` when the silence exceeds the window. True if fired."""
        if self.stall_seconds <= 0:
            return False
        silence = self.silence()
        if silence < self.stall_seconds:
            return False
        self._on_stall(silence)
        return True

    def start(self) -> None:
        if self.stall_seconds <= 0:
            logger.warning(
                "Startup watchdog disabled via %s — a wedged Docker call will "
                "hang the start silently",
                STALL_ENV_VAR,
            )
            return
        self.beat()
        self._handler = _HeartbeatHandler(self)
        logger.addHandler(self._handler)
        self._thread = threading.Thread(
            target=self._run, name="oduflow-startup-watchdog", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._done.set()
        if self._handler is not None:
            logger.removeHandler(self._handler)
            self._handler = None

    def _run(self) -> None:
        while not self._done.wait(self.poll_seconds):
            if self.check():
                return


def _abort(silence: float) -> None:
    """Log why, dump every thread's stack, and exit for systemd to restart us."""
    logger.error(
        "Startup made no progress for %.0fs — assuming a wedged Docker call and "
        "exiting so the service is restarted. Thread stacks follow.",
        silence,
    )
    # Written straight to stderr (journal): the logging path itself may be
    # waiting on the same lock as whatever is stuck.
    with contextlib.suppress(Exception):
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        sys.stderr.flush()
    # _exit, not sys.exit: a normal exit runs interpreter shutdown, which joins
    # non-daemon threads — and one of those is exactly what is hung.
    os._exit(1)


def _configured_stall_seconds(default: float) -> float:
    raw = os.environ.get(STALL_ENV_VAR)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Ignoring non-numeric %s=%r", STALL_ENV_VAR, raw)
        return default


@contextlib.contextmanager
def guard_startup(
    *,
    stall_seconds: float = DEFAULT_STALL_SECONDS,
    on_stall: Callable[[float], None] | None = None,
) -> Iterator[StartupWatchdog]:
    """Watch the wrapped startup work; stop watching once it completes."""
    watchdog = StartupWatchdog(
        stall_seconds=_configured_stall_seconds(stall_seconds), on_stall=on_stall
    )
    watchdog.start()
    try:
        yield watchdog
    finally:
        watchdog.stop()
