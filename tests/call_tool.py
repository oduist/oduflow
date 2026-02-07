#!/usr/bin/env python3
"""
CLI runner for Flow MCP tools — no MCP client required.

Usage:
    # Positional args — mapped to tool parameters in order
    python tests/call_tool.py delete_environment test
    python tests/call_tool.py create_environment main https://github.com/odoo/odoo.git
    python tests/call_tool.py create_environment main https://github.com/odoo/odoo.git 16.0

    # Named args via JSON
    python tests/call_tool.py create_environment -a '{"branch_name":"main","repo_url":"..."}'

    # Run a scenario from fixtures.yaml
    python tests/call_tool.py -s create_env_main

    # List tools / scenarios / run all
    python tests/call_tool.py --list
    python tests/call_tool.py --list-scenarios
    python tests/call_tool.py --all
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
import textwrap
from pathlib import Path

import yaml

FIXTURES_PATH = Path(__file__).parent / "fixtures.yaml"


def _load_fixtures():
    with open(FIXTURES_PATH) as f:
        return yaml.safe_load(f)["scenarios"]


def _build_kwargs_from_positional(tool_fn, positional: list[str]) -> dict:
    """Map positional CLI values to the tool function's parameter names."""
    sig = inspect.signature(tool_fn)
    params = list(sig.parameters.values())
    kwargs: dict = {}
    for i, value in enumerate(positional):
        if i >= len(params):
            print(f"Warning: extra argument '{value}' ignored", file=sys.stderr)
            continue
        param = params[i]
        annotation = param.annotation
        if annotation is bool or (annotation is inspect.Parameter.empty and isinstance(param.default, bool)):
            kwargs[param.name] = value.lower() in ("true", "1", "yes")
        elif annotation is int or (annotation is inspect.Parameter.empty and isinstance(param.default, int)):
            kwargs[param.name] = int(value)
        elif annotation is float:
            kwargs[param.name] = float(value)
        else:
            kwargs[param.name] = value
    return kwargs


def _show_tool_usage(tool_name: str, tool_fn):
    """Print parameter info for a tool."""
    sig = inspect.signature(tool_fn)
    parts = []
    for p in sig.parameters.values():
        if p.default is inspect.Parameter.empty:
            parts.append(f"<{p.name}>")
        else:
            parts.append(f"[{p.name}={p.default}]")
    print(f"Usage: call_tool.py {tool_name} {' '.join(parts)}")


def main() -> None:
    from tool_helpers import call_cli, call_tool, list_tools

    from flow.server import mcp

    parser = argparse.ArgumentParser(
        description="Call Flow MCP tools from the command line",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(__doc__ or ""),
    )
    parser.add_argument("tool", nargs="?", help="Tool name to invoke")
    parser.add_argument("tool_args", nargs="*", help="Positional arguments for the tool")
    parser.add_argument("-s", "--scenario", help="Scenario name from fixtures.yaml")
    parser.add_argument("-a", "--args", default=None, help="JSON-encoded keyword arguments")
    parser.add_argument("--list", action="store_true", help="List all registered tools")
    parser.add_argument("--list-scenarios", action="store_true", help="List scenarios from fixtures.yaml")
    parser.add_argument("--all", action="store_true", help="Run all scenarios from fixtures.yaml")

    args = parser.parse_args()

    if args.list:
        print("Registered tools:")
        for t in list_tools():
            tool_fn = mcp._tool_manager._tools[t].fn
            sig = inspect.signature(tool_fn)
            params = []
            for p in sig.parameters.values():
                if p.default is inspect.Parameter.empty:
                    params.append(f"<{p.name}>")
                else:
                    params.append(f"[{p.name}={p.default}]")
            print(f"  {t} {' '.join(params)}")
        return

    if args.list_scenarios:
        scenarios = _load_fixtures()
        print("Scenarios:")
        for name, sc in scenarios.items():
            print(f"  - {name:25s}  tool={sc['tool']}")
        return

    if args.all:
        scenarios = _load_fixtures()
        passed, failed = 0, 0
        for name, sc in scenarios.items():
            label = sc.get("tool") or f"cli:{sc.get('cli')}"
            print(f"\n{'='*60}")
            print(f"  Scenario: {name}  ({label})")
            print(f"{'='*60}")
            try:
                if "cli" in sc:
                    result = call_cli(sc["cli"], **sc.get("args", {}))
                else:
                    result = call_tool(sc["tool"], **sc.get("args", {}))
                print(result)
                for expected in sc.get("expect_contains", []):
                    if expected not in result:
                        print(f"  FAIL: expected '{expected}' in result")
                        failed += 1
                        break
                else:
                    passed += 1
                    print("  OK")
            except Exception as e:
                print(f"  ERROR: {e}")
                failed += 1

        print(f"\n{'='*60}")
        print(f"Results: {passed} passed, {failed} failed, {passed + failed} total")
        sys.exit(1 if failed else 0)

    if args.scenario:
        scenarios = _load_fixtures()
        if args.scenario not in scenarios:
            print(f"Unknown scenario: {args.scenario}")
            print(f"Available: {', '.join(scenarios.keys())}")
            sys.exit(1)
        sc = scenarios[args.scenario]
        if "cli" in sc:
            print(f"Calling CLI: {sc['cli']}({sc.get('args', {})})")
            print("-" * 60)
            try:
                result = call_cli(sc["cli"], **sc.get("args", {}))
                print(result)
            except Exception as e:
                print(f"ERROR: {e}", file=sys.stderr)
                sys.exit(1)
            return
        tool_name = sc["tool"]
        tool_args = sc.get("args", {})
    elif args.tool:
        tool_name = args.tool
        if tool_name not in mcp._tool_manager._tools:
            print(f"Unknown tool: {tool_name}")
            print(f"Available: {', '.join(list_tools())}")
            sys.exit(1)

        tool_fn = mcp._tool_manager._tools[tool_name].fn

        if args.args is not None:
            tool_args = json.loads(args.args)
        elif args.tool_args:
            tool_args = _build_kwargs_from_positional(tool_fn, args.tool_args)
        else:
            sig = inspect.signature(tool_fn)
            required = [p for p in sig.parameters.values() if p.default is inspect.Parameter.empty]
            if required:
                _show_tool_usage(tool_name, tool_fn)
                sys.exit(0)
            tool_args = {}
    else:
        parser.print_help()
        sys.exit(1)

    print(f"Calling: {tool_name}({tool_args})")
    print("-" * 60)
    try:
        result = call_tool(tool_name, **tool_args)
        print(result)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
