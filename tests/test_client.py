from __future__ import annotations

import io
import json
from types import SimpleNamespace
from typing import Any

from oduflow.client import run_client


def _tool(
    name: str,
    properties: dict[str, dict[str, Any]] | None = None,
    required: list[str] | None = None,
    description: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        description=description,
        inputSchema={
            "type": "object",
            "properties": properties or {},
            "required": required or [],
        },
    )


class FakeClient:
    def __init__(self, tools: list[Any], result: Any | None = None) -> None:
        self.tools = tools
        self.result = result or SimpleNamespace(
            content=[SimpleNamespace(text="ok")],
            structured_content=None,
            data=None,
            meta=None,
            is_error=False,
        )
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def list_tools(self) -> list[Any]:
        return self.tools

    async def call_tool(
        self, name: str, arguments: dict[str, Any], **kwargs: Any
    ) -> Any:
        self.calls.append((name, arguments, kwargs))
        return self.result


class Factory:
    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.calls: list[tuple[str, str, float]] = []

    def __call__(self, url: str, token: str, timeout: float) -> FakeClient:
        self.calls.append((url, token, timeout))
        return self.client


def _run(
    argv: list[str],
    client: FakeClient,
    *,
    environ: dict[str, str] | None = None,
    branch: str = "feature/test",
    stdin: str = "",
) -> tuple[int, str, str, Factory]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    factory = Factory(client)
    code = run_client(
        argv,
        environ=environ
        or {
            "ODUFLOW_MCP_URL": "https://oduflow.example/mcp",
            "ODUFLOW_MCP_TOKEN": "secret",
        },
        stdin=io.StringIO(stdin),
        stdout=stdout,
        stderr=stderr,
        client_factory=factory,
        branch_getter=lambda: branch,
    )
    return code, stdout.getvalue(), stderr.getvalue(), factory


def test_list_tools_uses_remote_url_and_token() -> None:
    client = FakeClient(
        [_tool("z_tool"), _tool("a_tool", description="First line\nMore")]
    )

    code, stdout, stderr, factory = _run(["list", "--verbose"], client)

    assert code == 0
    assert stdout == "a_tool\n  First line\nz_tool\n"
    assert stderr == ""
    assert factory.calls == [("https://oduflow.example/mcp", "secret", 600.0)]


def test_required_env_name_defaults_to_current_branch() -> None:
    client = FakeClient(
        [
            _tool(
                "pull_and_apply",
                {
                    "env_name": {"type": "string"},
                    "upgrade": {"type": "string", "default": ""},
                    "restart": {"type": "boolean", "default": False},
                    "strict": {"type": "boolean", "default": False},
                },
                ["env_name"],
            )
        ]
    )

    code, stdout, stderr, _factory = _run(
        ["pull_and_apply", "--upgrade", "sale_custom", "--strict"], client
    )

    assert code == 0
    assert stdout == "ok\n"
    assert stderr == ""
    assert client.calls[0][0:2] == (
        "pull_and_apply",
        {
            "env_name": "feature/test",
            "upgrade": "sale_custom",
            "strict": True,
        },
    )
    assert client.calls[0][2]["raise_on_error"] is False


def test_env_override_wins_and_boolean_can_be_negated() -> None:
    client = FakeClient(
        [
            _tool(
                "pull_and_apply",
                {
                    "env_name": {"type": "string"},
                    "restart": {"type": "boolean"},
                },
                ["env_name"],
            )
        ]
    )

    code, _stdout, _stderr, _factory = _run(
        ["--env", "demo", "pull_and_apply", "--no-restart"], client
    )

    assert code == 0
    assert client.calls[0][1] == {"env_name": "demo", "restart": False}


def test_scoped_schema_does_not_read_current_branch() -> None:
    client = FakeClient(
        [_tool("run_odoo_tests", {"modules": {"type": "string"}}, ["modules"])]
    )

    code = run_client(
        ["run_odoo_tests", "--modules", "sale_custom"],
        environ={"ODUFLOW_MCP_URL": "https://host/mcp/demo"},
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        client_factory=Factory(client),
        branch_getter=lambda: (_ for _ in ()).throw(AssertionError("branch read")),
    )

    assert code == 0
    assert client.calls[0][1] == {"modules": "sale_custom"}


def test_create_environment_injects_branch_and_optional_env_override() -> None:
    client = FakeClient(
        [
            _tool(
                "create_environment",
                {
                    "branch": {"type": "string"},
                    "env_name": {"type": "string", "default": ""},
                    "odoo_image": {"type": "string", "default": ""},
                },
                ["branch"],
            )
        ]
    )

    code, _stdout, _stderr, _factory = _run(
        ["--env", "demo", "create_environment", "--odoo-image", "odoo:19.0"],
        client,
        branch="feature/client",
    )

    assert code == 0
    assert client.calls[0][1] == {
        "branch": "feature/client",
        "env_name": "demo",
        "odoo_image": "odoo:19.0",
    }


def test_json_arguments_are_passed_through() -> None:
    client = FakeClient(
        [
            _tool(
                "odoo_search_read",
                {
                    "env_name": {"type": "string"},
                    "model": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                ["env_name", "model"],
            )
        ]
    )

    code, _stdout, _stderr, _factory = _run(
        ["odoo_search_read", '{"model":"res.partner","limit":20}'], client
    )

    assert code == 0
    assert client.calls[0][1] == {
        "env_name": "feature/test",
        "model": "res.partner",
        "limit": 20,
    }


def test_named_values_follow_tool_schema_types() -> None:
    client = FakeClient(
        [
            _tool(
                "typed",
                {
                    "count": {"type": "integer"},
                    "ratio": {"type": "number"},
                    "enabled": {"type": "boolean"},
                    "items": {"type": "array"},
                },
            )
        ]
    )

    code, _stdout, _stderr, _factory = _run(
        [
            "typed",
            "--count",
            "20",
            "--ratio=1.5",
            "--enabled",
            "false",
            "--items",
            '["a","b"]',
        ],
        client,
    )

    assert code == 0
    assert client.calls[0][1] == {
        "count": 20,
        "ratio": 1.5,
        "enabled": False,
        "items": ["a", "b"],
    }


def test_tool_help_comes_from_live_schema() -> None:
    client = FakeClient(
        [
            _tool(
                "logs",
                {"n_lines": {"type": "integer", "default": 100}},
                description="Read logs.",
            )
        ]
    )

    code, stdout, stderr, _factory = _run(["logs", "--help"], client)

    assert code == 0
    assert "Usage: oduflow client logs" in stdout
    assert "--n-lines <integer>" in stdout
    assert "Read logs." in stdout
    assert stderr == ""
    assert client.calls == []


def test_unavailable_tool_mentions_scoped_endpoint() -> None:
    code, _stdout, stderr, _factory = _run(
        ["list_environments"], FakeClient([_tool("run_odoo_tests")])
    )

    assert code == 2
    assert "not available" in stderr
    assert "scoped" in stderr


def test_tool_error_is_printed_and_returns_nonzero() -> None:
    result = SimpleNamespace(
        content=[SimpleNamespace(text="upgrade failed")],
        structured_content=None,
        data=None,
        meta=None,
        is_error=True,
    )
    client = FakeClient([_tool("upgrade")], result=result)

    code, stdout, stderr, _factory = _run(["upgrade"], client)

    assert code == 1
    assert stdout == "upgrade failed\n"
    assert stderr == ""


def test_json_output_is_machine_readable() -> None:
    result = SimpleNamespace(
        content=[
            SimpleNamespace(model_dump=lambda **_kwargs: {"type": "text", "text": "ok"})
        ],
        structured_content={"answer": 42},
        data="ok",
        meta={"request": "1"},
        is_error=False,
    )
    client = FakeClient([_tool("answer")], result=result)

    code, stdout, _stderr, _factory = _run(["--json", "answer"], client)

    assert code == 0
    assert json.loads(stdout) == {
        "content": [{"type": "text", "text": "ok"}],
        "structured_content": {"answer": 42},
        "data": "ok",
        "meta": {"request": "1"},
        "is_error": False,
    }


def test_missing_url_fails_before_connecting() -> None:
    client = FakeClient([])

    code, _stdout, stderr, factory = _run(
        ["list"], client, environ={"ODUFLOW_MCP_TOKEN": "secret"}
    )

    assert code == 2
    assert "ODUFLOW_MCP_URL" in stderr
    assert factory.calls == []


def test_token_can_be_read_from_stdin() -> None:
    client = FakeClient([])

    code, _stdout, _stderr, factory = _run(
        ["--token-stdin", "list"],
        client,
        environ={"ODUFLOW_MCP_URL": "https://host/mcp"},
        stdin="from-stdin\n",
    )

    assert code == 0
    assert factory.calls == [("https://host/mcp", "from-stdin", 600.0)]


def test_real_fastmcp_client_round_trip() -> None:
    from fastmcp import Client, FastMCP

    server = FastMCP("test")

    @server.tool()
    def identify(env_name: str, count: int = 1) -> str:
        return f"{env_name}:{count}"

    stdout = io.StringIO()
    stderr = io.StringIO()
    code = run_client(
        ["identify", "--count", "2"],
        environ={"ODUFLOW_MCP_URL": "https://unused.example/mcp"},
        stdout=stdout,
        stderr=stderr,
        client_factory=lambda _url, _token, _timeout: Client(server),
        branch_getter=lambda: "feature/round-trip",
    )

    assert code == 0
    assert stdout.getvalue() == "feature/round-trip:2\n"
    assert stderr.getvalue() == ""


def test_optional_boolean_union_supports_bare_and_negated_flags() -> None:
    optional_bool = {"anyOf": [{"type": "boolean"}, {"type": "null"}], "default": None}
    client = FakeClient(
        [
            _tool(
                "update_service",
                {
                    "name": {"type": "string"},
                    "host_mode": optional_bool,
                    "privileged": optional_bool,
                },
                ["name"],
            )
        ]
    )

    code, _stdout, _stderr, _factory = _run(
        ["update_service", "--name", "fs", "--host-mode", "--no-privileged"], client
    )

    assert code == 0
    assert client.calls[0][1] == {
        "name": "fs",
        "host_mode": True,
        "privileged": False,
    }


def test_optional_union_help_shows_concrete_type() -> None:
    client = FakeClient(
        [
            _tool(
                "update_service",
                {
                    "host_mode": {
                        "anyOf": [{"type": "boolean"}, {"type": "null"}],
                        "default": None,
                    }
                },
            )
        ]
    )

    code, stdout, _stderr, _factory = _run(["update_service", "--help"], client)

    assert code == 0
    assert "--host-mode <boolean>" in stdout


def test_required_branch_is_not_inferred_outside_create_environment() -> None:
    client = FakeClient(
        [
            _tool(
                "create_production",
                {
                    "name": {"type": "string"},
                    "branch": {"type": "string"},
                    "domain": {"type": "string"},
                },
                ["name", "branch", "domain"],
            )
        ]
    )

    code, _stdout, stderr, _factory = _run(
        ["create_production", "--name", "acme", "--domain", "acme.example"], client
    )

    assert code == 2
    assert "--branch" in stderr
    assert client.calls == []
