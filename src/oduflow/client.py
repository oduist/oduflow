"""Remote Oduflow CLI backed by the server's FastMCP tool surface."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import warnings
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TextIO

try:
    from authlib.deprecate import AuthlibDeprecationWarning

    warnings.filterwarnings("ignore", category=AuthlibDeprecationWarning)
except Exception:  # pragma: no cover - authlib internals may change
    pass


class ClientUsageError(ValueError):
    """Invalid remote-client configuration or command arguments."""


@dataclass(frozen=True)
class ClientOptions:
    url: str
    token: str
    env_name: str
    timeout: float
    json_output: bool
    tool_name: str
    tool_args: list[str]


ClientFactory = Callable[[str, str, float], Any]
BranchGetter = Callable[[], str]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="oduflow client",
        description="Call tools on a remote Oduflow MCP server",
    )
    parser.add_argument(
        "--url",
        default="",
        help="MCP endpoint (default: ODUFLOW_MCP_URL)",
    )
    parser.add_argument(
        "--env",
        default="",
        help="environment override (default: ODUFLOW_ENV_NAME or current branch)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="tool timeout in seconds (default: 600)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="emit the complete MCP result as JSON",
    )
    parser.add_argument(
        "--token-stdin",
        action="store_true",
        help="read the Bearer token from stdin instead of ODUFLOW_MCP_TOKEN",
    )
    parser.add_argument(
        "tool_name",
        nargs="?",
        default="list",
        help="remote MCP tool name, or 'list'",
    )
    parser.add_argument("tool_args", nargs=argparse.REMAINDER)
    return parser


def _parse_options(
    argv: Sequence[str],
    environ: Mapping[str, str],
    stdin: TextIO,
) -> ClientOptions:
    args = _parser().parse_args(list(argv))
    url = (args.url or environ.get("ODUFLOW_MCP_URL", "")).strip()
    if not url:
        raise ClientUsageError(
            "MCP URL is required; set ODUFLOW_MCP_URL or pass --url."
        )
    if args.timeout <= 0:
        raise ClientUsageError("--timeout must be greater than zero.")

    if args.token_stdin:
        token = stdin.readline().strip()
    else:
        token = environ.get("ODUFLOW_MCP_TOKEN", "").strip()

    return ClientOptions(
        url=url,
        token=token,
        env_name=(args.env or environ.get("ODUFLOW_ENV_NAME", "")).strip(),
        timeout=args.timeout,
        json_output=args.json_output,
        tool_name=args.tool_name,
        tool_args=list(args.tool_args),
    )


def _git_branch() -> str:
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClientUsageError(f"Cannot determine the current git branch: {exc}")
    branch = result.stdout.strip()
    if result.returncode != 0 or not branch:
        raise ClientUsageError(
            "Cannot determine the current git branch; pass --env or the required "
            "--branch argument explicitly."
        )
    return branch


def _default_client_factory(url: str, token: str, timeout: float) -> Any:
    from fastmcp import Client

    return Client(url, auth=token or None, timeout=timeout)


def _tool_schema(tool: Any) -> dict[str, Any]:
    schema = getattr(tool, "inputSchema", None)
    if schema is None:
        schema = getattr(tool, "input_schema", None)
    return schema if isinstance(schema, dict) else {}


def _tool_description(tool: Any) -> str:
    return str(getattr(tool, "description", "") or "").strip()


def _parameter_type(parameter: Mapping[str, Any]) -> Any:
    """Return the effective JSON Schema type of a parameter.

    Optional tool arguments (``bool | None``, ``list[...] | None``) reach the
    client as an ``anyOf`` union with a ``null`` branch, so the concrete type
    has to be unwrapped before flags can be parsed and coerced.
    """
    value_type = parameter.get("type")
    if value_type is not None:
        return value_type
    variants = parameter.get("anyOf")
    if isinstance(variants, list):
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            variant_type = variant.get("type")
            if variant_type is not None and variant_type != "null":
                return variant_type
    return None


def _coerce_value(raw: str, parameter: Mapping[str, Any]) -> Any:
    value_type = _parameter_type(parameter)
    if value_type == "string" or value_type is None:
        return raw
    if value_type == "boolean":
        normalized = raw.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
        raise ClientUsageError(f"Expected a boolean value, got {raw!r}.")
    if value_type == "integer":
        try:
            return int(raw)
        except ValueError as exc:
            raise ClientUsageError(f"Expected an integer value, got {raw!r}.") from exc
    if value_type == "number":
        try:
            return float(raw)
        except ValueError as exc:
            raise ClientUsageError(f"Expected a numeric value, got {raw!r}.") from exc
    if value_type in {"array", "object"}:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ClientUsageError(
                f"Expected JSON for {value_type} value, got {raw!r}."
            ) from exc
        expected = list if value_type == "array" else dict
        if not isinstance(value, expected):
            raise ClientUsageError(f"Expected a JSON {value_type}, got {raw!r}.")
        return value
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _parse_json_arguments(tokens: Sequence[str]) -> dict[str, Any] | None:
    if not tokens or not tokens[0].lstrip().startswith("{"):
        return None
    if len(tokens) != 1:
        raise ClientUsageError(
            "A JSON argument object cannot be combined with named flags."
        )
    try:
        value = json.loads(tokens[0])
    except json.JSONDecodeError as exc:
        raise ClientUsageError(f"Invalid JSON argument object: {exc}") from exc
    if not isinstance(value, dict):
        raise ClientUsageError("Tool arguments must be a JSON object.")
    return value


def _parse_named_arguments(
    tokens: Sequence[str], schema: Mapping[str, Any]
) -> dict[str, Any]:
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        properties = {}
    result: dict[str, Any] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("--"):
            raise ClientUsageError(
                f"Unexpected positional argument {token!r}; use named flags or one "
                "JSON object."
            )
        raw_name, separator, inline_value = token[2:].partition("=")
        negated = raw_name.startswith("no-")
        if negated:
            raw_name = raw_name[3:]
        name = raw_name.replace("-", "_")
        parameter = properties.get(name)
        if not isinstance(parameter, dict):
            available = ", ".join(sorted(properties)) or "none"
            raise ClientUsageError(
                f"Unknown argument --{raw_name}; available parameters: {available}."
            )
        parameter_type = _parameter_type(parameter)
        if negated and parameter_type != "boolean":
            raise ClientUsageError(f"--no-{raw_name} is valid only for booleans.")

        if separator:
            if negated:
                raise ClientUsageError(f"--no-{raw_name} does not take a value.")
            raw_value = inline_value
        elif parameter_type == "boolean":
            if negated:
                raw_value = "false"
            elif index + 1 < len(tokens) and not tokens[index + 1].startswith("--"):
                index += 1
                raw_value = tokens[index]
            else:
                raw_value = "true"
        else:
            if negated:
                raise ClientUsageError(f"--no-{raw_name} is valid only for booleans.")
            if index + 1 >= len(tokens) or tokens[index + 1].startswith("--"):
                raise ClientUsageError(f"--{raw_name} requires a value.")
            index += 1
            raw_value = tokens[index]

        result[name] = _coerce_value(raw_value, parameter)
        index += 1
    return result


def _resolve_arguments(
    tokens: Sequence[str],
    tool: Any,
    env_override: str,
    branch_getter: BranchGetter,
) -> dict[str, Any]:
    schema = _tool_schema(tool)
    arguments = _parse_json_arguments(tokens)
    if arguments is None:
        arguments = _parse_named_arguments(tokens, schema)

    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, dict):
        properties = {}
    if not isinstance(required, list):
        required = []

    if "env_name" in required and "env_name" not in arguments:
        arguments["env_name"] = env_override or branch_getter()
    elif env_override and "env_name" in properties and "env_name" not in arguments:
        arguments["env_name"] = env_override

    # Only environment creation infers the branch from the working copy. Other
    # tools that require a branch (create_production) target long-lived
    # deployments, where a silent default from the operator's checkout would be
    # the wrong target.
    if (
        str(getattr(tool, "name", "")) == "create_environment"
        and "branch" in required
        and "branch" not in arguments
    ):
        arguments["branch"] = branch_getter()

    missing = [name for name in required if name not in arguments]
    if missing:
        rendered = ", ".join(f"--{name.replace('_', '-')}" for name in missing)
        raise ClientUsageError(f"Missing required arguments: {rendered}.")
    return arguments


def _print_tools(tools: Sequence[Any], *, verbose: bool, stdout: TextIO) -> None:
    for tool in sorted(tools, key=lambda item: str(getattr(item, "name", ""))):
        name = str(getattr(tool, "name", ""))
        if not name:
            continue
        print(name, file=stdout)
        if verbose:
            description = _tool_description(tool).splitlines()[0:1]
            if description:
                print(f"  {description[0]}", file=stdout)


def _print_tool_help(tool: Any, stdout: TextIO) -> None:
    name = str(getattr(tool, "name", ""))
    description = _tool_description(tool)
    schema = _tool_schema(tool)
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, dict):
        properties = {}
    if not isinstance(required, list):
        required = []

    print(f"Usage: oduflow client {name} [JSON_OBJECT | --name VALUE ...]", file=stdout)
    if description:
        print(f"\n{description}", file=stdout)
    if not properties:
        print("\nThis tool takes no arguments.", file=stdout)
        return
    print("\nArguments:", file=stdout)
    for parameter_name, parameter in properties.items():
        if not isinstance(parameter, dict):
            parameter = {}
        flag = parameter_name.replace("_", "-")
        value_type = _parameter_type(parameter) or "value"
        suffix = "required" if parameter_name in required else "optional"
        if "default" in parameter:
            suffix += f", default={parameter['default']!r}"
        print(f"  --{flag} <{value_type}>  {suffix}", file=stdout)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _print_result(result: Any, *, as_json: bool, stdout: TextIO) -> None:
    if as_json:
        payload = {
            "content": _jsonable(getattr(result, "content", [])),
            "structured_content": _jsonable(
                getattr(result, "structured_content", None)
            ),
            "data": _jsonable(getattr(result, "data", None)),
            "meta": _jsonable(getattr(result, "meta", None)),
            "is_error": bool(getattr(result, "is_error", False)),
        }
        print(json.dumps(payload, ensure_ascii=True), file=stdout)
        return

    printed = False
    for block in getattr(result, "content", []):
        text = getattr(block, "text", None)
        if text is not None:
            print(text, file=stdout)
            printed = True
        else:
            print(json.dumps(_jsonable(block), ensure_ascii=True), file=stdout)
            printed = True
    if not printed and getattr(result, "data", None) is not None:
        data = getattr(result, "data")
        if isinstance(data, str):
            print(data, file=stdout)
        else:
            print(json.dumps(_jsonable(data), ensure_ascii=True), file=stdout)


async def _run_client_async(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str],
    stdin: TextIO,
    stdout: TextIO,
    client_factory: ClientFactory,
    branch_getter: BranchGetter,
) -> int:
    options = _parse_options(argv, environ, stdin)
    async with client_factory(options.url, options.token, options.timeout) as client:
        tools = await client.list_tools()
        if options.tool_name == "list":
            verbose = options.tool_args == ["--verbose"]
            if options.tool_args and not verbose:
                raise ClientUsageError("Usage: oduflow client list [--verbose]")
            _print_tools(tools, verbose=verbose, stdout=stdout)
            return 0

        by_name = {str(getattr(tool, "name", "")): tool for tool in tools}
        tool = by_name.get(options.tool_name)
        if tool is None:
            scope_hint = " The endpoint may be scoped to one environment."
            raise ClientUsageError(
                f"Tool {options.tool_name!r} is not available on this Oduflow "
                f"endpoint.{scope_hint}"
            )
        if options.tool_args == ["--help"]:
            _print_tool_help(tool, stdout)
            return 0

        arguments = _resolve_arguments(
            options.tool_args, tool, options.env_name, branch_getter
        )
        result = await client.call_tool(
            options.tool_name,
            arguments,
            timeout=options.timeout,
            raise_on_error=False,
        )
        _print_result(result, as_json=options.json_output, stdout=stdout)
        return 1 if bool(getattr(result, "is_error", False)) else 0


def run_client(
    argv: Sequence[str],
    *,
    environ: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    client_factory: ClientFactory | None = None,
    branch_getter: BranchGetter | None = None,
) -> int:
    """Run ``oduflow client`` and return a process-compatible exit code."""
    environ = os.environ if environ is None else environ
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    client_factory = client_factory or _default_client_factory
    branch_getter = branch_getter or _git_branch
    try:
        return asyncio.run(
            _run_client_async(
                argv,
                environ=environ,
                stdin=stdin,
                stdout=stdout,
                client_factory=client_factory,
                branch_getter=branch_getter,
            )
        )
    except ClientUsageError as exc:
        print(f"ERROR: {exc}", file=stderr)
        return 2
    except KeyboardInterrupt:
        print("ERROR: interrupted", file=stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=stderr)
        return 1
