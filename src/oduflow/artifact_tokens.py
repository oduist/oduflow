"""One-time, short-lived download tokens for files generated inside an environment.

Oduflow has always had a way to push code *into* an environment (git push +
``pull_and_apply``, or a live-mount) but no way to get a generated file back
*out* except through a tool response — i.e. through the agent's context window.
That is fine for a 20-line config and wrong for a 32 KB ``.pot``: paying a
sizeable slice of the context window to move a file the agent only wants to save
to disk is pure waste.

So a tool that produces an artifact stashes the bytes here and returns a URL
instead. The agent fetches it with ``curl -o`` and the file never enters the
conversation. Authentication is the token itself — like ``connect_tokens``, the
entry is consumed exactly once and expires quickly, so a link left in a
transcript is useless afterwards.

The store is in-process and bounded: the server is single-process, and artifacts
are large enough that an unbounded dict would be a memory leak with a URL
attached.
"""

from __future__ import annotations

import threading
import time

from oduflow.env_tokens import generate_token

# Long enough for an agent to notice the URL and run curl, short enough that a
# link in a transcript or shell history is dead by the time anyone reads it.
_TTL_SECONDS = 600.0
# Generated translation files are tens of KB; the cap is here so a future caller
# cannot turn this into an unbounded buffer.
_MAX_BYTES = 20 * 1024 * 1024
_MAX_ENTRIES = 20

_lock = threading.Lock()
# token -> (filename, content, expiry_monotonic)
_store: dict[str, tuple[str, bytes, float]] = {}


def _now(now: float | None) -> float:
    return time.monotonic() if now is None else now


def _prune(now: float) -> None:
    for token in [t for t, (_, _, exp) in _store.items() if exp <= now]:
        _store.pop(token, None)


def issue(filename: str, content: bytes, *, now: float | None = None) -> str:
    """Stash ``content`` for one download and return the token addressing it.

    Raises ``ValueError`` if the artifact exceeds the size cap.
    """
    if len(content) > _MAX_BYTES:
        raise ValueError(
            f"Artifact is too large to offer for download "
            f"({len(content)} bytes, limit {_MAX_BYTES})."
        )
    ts = _now(now)
    token = generate_token()
    with _lock:
        _prune(ts)
        # Drop the oldest pending artifacts rather than grow without bound; an
        # evicted link simply 404s, which is the same outcome as expiry.
        while len(_store) >= _MAX_ENTRIES:
            oldest = min(_store, key=lambda t: _store[t][2])
            _store.pop(oldest, None)
        _store[token] = (filename, content, ts + _TTL_SECONDS)
    return token


def consume(token: str, *, now: float | None = None) -> tuple[str, bytes] | None:
    """Return ``(filename, content)`` for a valid token, once.

    Returns ``None`` if the token is unknown, already used, or expired.
    """
    ts = _now(now)
    with _lock:
        _prune(ts)
        entry = _store.pop(token, None)
    if entry is None:
        return None
    filename, content, expiry = entry
    if expiry <= ts:
        return None
    return filename, content
