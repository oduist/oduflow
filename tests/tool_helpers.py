"""Shared helper for calling MCP tools directly without an MCP client."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from flow.server import mcp  # noqa: E402


def call_tool(name: str, **kwargs):
    """Look up a registered MCP tool by name and invoke it with *kwargs*."""
    tool = mcp._tool_manager._tools[name]
    return tool.fn(**kwargs)


def list_tools() -> list[str]:
    """Return sorted list of all registered tool names."""
    return sorted(mcp._tool_manager._tools.keys())
