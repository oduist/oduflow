"""Unit tests for the stateless-transport log filter."""

from __future__ import annotations

import logging
import sys

from anyio import BrokenResourceError, ClosedResourceError, EndOfStream

if sys.version_info < (3, 11):  # ExceptionGroup is a builtin only on 3.11+
    from exceptiongroup import ExceptionGroup  # type: ignore[import-not-found]

from oduflow.mcp_log_filters import (
    StatelessDisconnectFilter,
    _is_client_disconnect,
    install_stateless_disconnect_filter,
)


def _exc_info(exc: BaseException):
    """Return a (type, value, tb) tuple with a real traceback for ``exc``."""
    try:
        raise exc
    except BaseException:  # noqa: BLE001 - re-raise to capture exc_info
        return sys.exc_info()


def _crash_record(exc: BaseException | None) -> logging.LogRecord:
    """Build the record the SDK emits via ``logger.exception(...)``."""
    return logging.LogRecord(
        name="mcp.server.streamable_http_manager",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Stateless session crashed",
        args=(),
        exc_info=_exc_info(exc) if exc is not None else None,
    )


class TestIsClientDisconnect:
    def test_none(self):
        assert _is_client_disconnect(None) is False

    def test_closed_resource(self):
        assert _is_client_disconnect(ClosedResourceError()) is True

    def test_broken_resource(self):
        assert _is_client_disconnect(BrokenResourceError()) is True

    def test_end_of_stream(self):
        assert _is_client_disconnect(EndOfStream()) is True

    def test_unrelated_error(self):
        assert _is_client_disconnect(ValueError("boom")) is False

    def test_exception_group_all_disconnect(self):
        group = ExceptionGroup("tg", [ClosedResourceError()])
        assert _is_client_disconnect(group) is True

    def test_exception_group_nested(self):
        inner = ExceptionGroup("inner", [ClosedResourceError()])
        outer = ExceptionGroup("outer", [inner])
        assert _is_client_disconnect(outer) is True

    def test_exception_group_mixed_is_not_disconnect(self):
        # A real error mixed in must NOT be swallowed.
        group = ExceptionGroup("tg", [ClosedResourceError(), ValueError("real")])
        assert _is_client_disconnect(group) is False


class TestStatelessDisconnectFilter:
    def test_drops_client_disconnect(self):
        f = StatelessDisconnectFilter()
        record = _crash_record(ExceptionGroup("tg", [ClosedResourceError()]))
        assert f.filter(record) is False

    def test_keeps_genuine_crash(self):
        f = StatelessDisconnectFilter()
        record = _crash_record(RuntimeError("something actually broke"))
        assert f.filter(record) is True

    def test_keeps_other_messages(self):
        f = StatelessDisconnectFilter()
        record = logging.LogRecord(
            name="mcp.server.streamable_http_manager",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="Terminating session: %s",
            args=("abc",),
            exc_info=None,
        )
        assert f.filter(record) is True

    def test_keeps_crash_message_without_exc_info(self):
        f = StatelessDisconnectFilter()
        assert f.filter(_crash_record(None)) is True


class TestInstall:
    def test_idempotent_and_attached_to_sdk_logger(self):
        sdk_logger = logging.getLogger("mcp.server.streamable_http_manager")
        before = [
            fltr
            for fltr in sdk_logger.filters
            if isinstance(fltr, StatelessDisconnectFilter)
        ]
        try:
            install_stateless_disconnect_filter()
            install_stateless_disconnect_filter()
            attached = [
                fltr
                for fltr in sdk_logger.filters
                if isinstance(fltr, StatelessDisconnectFilter)
            ]
            assert len(attached) == 1
        finally:
            # Restore the pre-test filter set so this test stays isolated.
            for fltr in list(sdk_logger.filters):
                if isinstance(fltr, StatelessDisconnectFilter) and fltr not in before:
                    sdk_logger.removeFilter(fltr)
