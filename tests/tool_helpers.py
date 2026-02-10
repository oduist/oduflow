"""Shared helpers for calling MCP tools and CLI commands directly."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from oduflow.server import mcp  # noqa: E402


def call_tool(name: str, **kwargs):
    """Look up a registered MCP tool by name and invoke it with *kwargs*."""
    tool = mcp._tool_manager._tools[name]
    return tool.fn(**kwargs)


def list_tools() -> list[str]:
    """Return sorted list of all registered tool names."""
    return sorted(mcp._tool_manager._tools.keys())


def call_cli(command: str, **kwargs) -> str:
    """Run a CLI command (init / destroy) and return formatted output."""
    from oduflow.docker_ops import system_ops
    from oduflow.settings import Settings

    settings = Settings.from_env()

    if command == "init":
        result = system_ops.init_system(
            settings,
            version=kwargs.get("version", "15.0"),
            force=kwargs.get("force", False),
        )
        msg = f"System {result['status']}.\nTemplate DB: {result['template_db']}"
        if "restore_seconds" in result:
            msg += f"\nDB restore time: {result['restore_seconds']}s"
        return msg

    if command == "destroy":
        result = system_ops.destroy_system(settings)
        return (
            f"System {result['status']}.\n"
            f"Removed: {result['removed']}"
        )

    raise ValueError(f"Unknown CLI command: {command}")
