"""Guard: the dashboard sign-in path is configurable, so nothing may hardcode it.

``[server].login_path`` (default ``/auth_login``) moves the sign-in page off the
path every commodity scanner probes. That only holds if every producer of the
URL — route registration, redirects, the login form action, the dashboard's
401 handler — goes through the setting. A stray ``"/login"`` literal would
either 404 for users or resurrect the old path as a working alias.

This is a lint-style test over the shipped package, not a behaviour test; the
behavioural coverage lives in tests/test_web_ui_auth.py.
"""

from __future__ import annotations

import pathlib
import re

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "oduflow"

# ``/login`` as a whole path segment inside a quoted string. Odoo's own
# ``/web/login`` (used by connect_as_user and the docs) is a different server
# and is excluded by requiring the quote/start boundary before the slash.
_HARDCODED = re.compile(r"""["'`]/login(?:["'`/?#])""")

# Free-text mentions of the coding agents' interactive ``/login`` slash command
# (Claude Code / Codex CLI), which is unrelated to the dashboard URL.
_AGENT_CLI_HINT = re.compile(r"interactive|slash command|`/login`|run `/login`")


def _offenders() -> list[str]:
    hits: list[str] = []
    for path in sorted(_SRC.rglob("*")):
        if path.suffix not in (".py", ".html", ".js") or not path.is_file():
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # Comments may name the old path when explaining why it moved.
            if line.lstrip().startswith(("#", "//", "*")):
                continue
            if _HARDCODED.search(line) and not _AGENT_CLI_HINT.search(line):
                rel = path.relative_to(_SRC.parent.parent)
                hits.append(f"{rel}:{lineno}: {line.strip()}")
    return hits


def test_no_hardcoded_login_path_in_package():
    offenders = _offenders()
    assert not offenders, (
        "Hardcoded '/login' found; use Settings.web_login_path instead:\n"
        + "\n".join(offenders)
    )
