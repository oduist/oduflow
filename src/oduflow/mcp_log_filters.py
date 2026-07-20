"""Log hygiene for the MCP StreamableHTTP stateless transport.

The MCP SDK runs every stateless request's tool call in its own task
(``mcp.server.streamable_http_manager.run_stateless_server``). When a client
disconnects before a long-running tool (e.g. ``create_environment``) finishes,
the tool still completes server-side, but its final response is written to an
already-closed stream. anyio then raises ``ClosedResourceError`` — usually
wrapped in a task-group ``ExceptionGroup`` — which the SDK logs at ERROR with a
full traceback as ``"Stateless session crashed"``.

That is benign: the operation itself succeeded; only response *delivery* failed
because the client went away. This module installs a logging filter that drops
those specific records (leaving a quiet DEBUG breadcrumb) while letting genuine
stateless crashes through untouched.

Note on why we drop rather than downgrade: ``logging.basicConfig`` installs a
root handler at NOTSET, so a record that survives the originating logger's level
check (ERROR passes INFO) is emitted regardless of its level. Rewriting the
record to DEBUG would therefore still print it. Returning ``False`` is the only
reliable way to suppress it by default.
"""

from __future__ import annotations

import logging

from anyio import BrokenResourceError, ClosedResourceError, EndOfStream

logger = logging.getLogger("oduflow")

_SDK_LOGGER_NAME = "mcp.server.streamable_http_manager"
_CRASH_MESSAGE = "Stateless session crashed"

# Stream-closed errors that mean the peer vanished before we could reply.
_DISCONNECT_ERRORS = (ClosedResourceError, BrokenResourceError, EndOfStream)


def _is_client_disconnect(exc: BaseException | None) -> bool:
    """True if ``exc`` is — or is an ``ExceptionGroup`` composed *solely* of —
    a stream-closed error signalling the client disconnected before the
    response could be sent.

    Requiring *every* member of a group to be a disconnect (``all``) keeps a
    real error mixed into the group from being silently swallowed.
    """
    if exc is None:
        return False
    if isinstance(exc, _DISCONNECT_ERRORS):
        return True
    # anyio surfaces task-group failures as an ExceptionGroup (``.exceptions``);
    # unwrap without importing BaseExceptionGroup so this also works on 3.10.
    nested = getattr(exc, "exceptions", None)
    if isinstance(nested, tuple) and nested:
        return all(_is_client_disconnect(sub) for sub in nested)
    return False


class StatelessDisconnectFilter(logging.Filter):
    """Silence the SDK's ``"Stateless session crashed"`` ERROR when it is only
    a client disconnect: drop the record (return ``False``) and leave a DEBUG
    breadcrumb. Every other record — including genuine stateless crashes —
    passes through unchanged.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.getMessage() != _CRASH_MESSAGE:
            return True
        exc = record.exc_info[1] if record.exc_info else None
        if not _is_client_disconnect(exc):
            return True
        # Gated by the "oduflow" logger's level (INFO by default), so this is
        # silent unless the operator turns on DEBUG.
        logger.debug(
            "MCP client disconnected before the tool finished; response not "
            "delivered (the operation itself completed)."
        )
        return False


def install_stateless_disconnect_filter() -> None:
    """Attach :class:`StatelessDisconnectFilter` to the MCP SDK logger, once."""
    sdk_logger = logging.getLogger(_SDK_LOGGER_NAME)
    if any(isinstance(f, StatelessDisconnectFilter) for f in sdk_logger.filters):
        return
    sdk_logger.addFilter(StatelessDisconnectFilter())
