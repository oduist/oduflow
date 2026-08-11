"""Unit tests for the execute_kw-equivalent JSON-RPC transport."""

import ast
import json
import tarfile
import threading
import time

import pytest
from unittest.mock import MagicMock, patch

from oduflow.docker_ops import odoo_rpc
from oduflow.errors import ExternalCommandError
from oduflow.settings import Settings, TeamSettings

TEST_TEAM = TeamSettings(
    team_id="1",
    data_dir="/tmp/flow-test",
    port_registry_path="/tmp/flow-test/ports.json",
    port_range_start=50000,
    port_range_end=50100,
)

TEST_SETTINGS = Settings(
    base_data_dir="/tmp/flow-test",
    db_user="odoo",
    db_password="odoo",
    etc_dir="/tmp/flow-test/etc",
    teams={"1": TEST_TEAM},
)


@pytest.fixture(autouse=True)
def _clear_sessions():
    odoo_rpc._SESSIONS.clear()
    odoo_rpc._MINT_LOCKS.clear()
    yield
    odoo_rpc._SESSIONS.clear()
    odoo_rpc._MINT_LOCKS.clear()


def _framed(payload, status=200):
    """Build the helper's stdout for a JSON-RPC *payload*."""
    body = json.dumps(payload).encode("utf-8")
    return b"ODUFLOW-RPC %d %d\n" % (status, len(body)) + body


def _ok(result):
    return _framed({"jsonrpc": "2.0", "id": 1, "result": result})


def _odoo_error(name, message="boom", debug="Traceback...", code=200):
    return _framed(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {
                "code": code,
                "message": "Odoo Server Error",
                "data": {"name": name, "message": message, "debug": debug},
            },
        }
    )


def _container(*stdouts):
    """A MagicMock container whose RPC execs return *stdouts* in order."""
    container = MagicMock()
    queue = list(stdouts)

    def exec_run(cmd, **kwargs):
        if cmd[0] == "python3" and len(cmd) == 2:
            return (0, (queue.pop(0), b""))
        return (0, (b"", b""))

    container.exec_run.side_effect = exec_run
    return container


def _mint(sid="sid-1", login="admin", uid="2"):
    return patch(
        "oduflow.docker_ops.odoo_ops.connect_as_user",
        return_value={
            "sid": sid,
            "login": login,
            "uid": uid,
            "base_url": "https://main.example.com",
            "cookie_domain": "main.example.com",
            "url": "https://main.example.com/web",
            "expires_at": "2030-01-01T00:00:00Z",
        },
    )


def _embedded_payload(script):
    """Pull the JSON payload literal back out of the generated helper script."""
    prefix = "P = json.loads("
    line = next(line for line in script.splitlines() if line.startswith(prefix))
    return ast.literal_eval(line[len(prefix) : -1])


def _client(container):
    return patch(
        "oduflow.docker_ops.odoo_rpc.get_client",
        return_value=MagicMock(**{"containers.get.return_value": container}),
    )


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


class TestParseJsonArg:
    def test_json_object_and_array(self):
        assert odoo_rpc.parse_json_arg('{"a": 1}', "values") == {"a": 1}
        assert odoo_rpc.parse_json_arg("[1, 2]", "args") == [1, 2]

    def test_empty_returns_default(self):
        assert odoo_rpc.parse_json_arg("", "args", []) == []
        assert odoo_rpc.parse_json_arg("   ", "args") is None

    def test_python_literal_is_accepted(self):
        assert odoo_rpc.parse_json_arg("{'a': True, 'b': None}", "values") == {
            "a": True,
            "b": None,
        }

    def test_tuples_become_lists(self):
        assert odoo_rpc.parse_json_arg("[('name', '=', 'x')]", "domain") == [
            ["name", "=", "x"]
        ]

    def test_code_is_not_evaluated(self):
        # literal_eval, never eval: this must not import anything.
        with pytest.raises(ValueError, match="neither valid JSON"):
            odoo_rpc.parse_json_arg("__import__('os').system('x')", "args")

    def test_sets_are_rejected(self):
        with pytest.raises(ValueError, match="cannot be sent"):
            odoo_rpc.parse_json_arg("{1, 2}", "values")

    def test_garbage_names_the_argument(self):
        with pytest.raises(ValueError, match="domain is neither valid JSON"):
            odoo_rpc.parse_json_arg("not json at all", "domain")


class TestParseDomain:
    def test_list_of_leaves_unchanged(self):
        assert odoo_rpc.parse_domain('[["a", "=", 1]]') == [["a", "=", 1]]

    def test_bare_leaf_is_wrapped(self):
        assert odoo_rpc.parse_domain('["a", "=", 1]') == [["a", "=", 1]]

    def test_prefix_operator_not_mistaken_for_a_leaf(self):
        raw = '["&", ["a", "=", 1], ["b", "=", 2]]'
        assert odoo_rpc.parse_domain(raw) == [
            "&",
            ["a", "=", 1],
            ["b", "=", 2],
        ]

    def test_unknown_operator_is_left_alone(self):
        assert odoo_rpc.parse_domain('["a", "zzz", 1]') == ["a", "zzz", 1]

    def test_empty_is_match_all(self):
        assert odoo_rpc.parse_domain("") == []

    def test_object_is_rejected(self):
        with pytest.raises(ValueError, match="domain must be a JSON array"):
            odoo_rpc.parse_domain('{"a": 1}')


class TestParseFields:
    def test_comma_separated(self):
        assert odoo_rpc.parse_fields("name, email") == ["name", "email"]

    def test_json_array(self):
        assert odoo_rpc.parse_fields('["name", "email"]') == ["name", "email"]

    def test_empty_means_all_fields(self):
        assert odoo_rpc.parse_fields("") is None

    def test_non_strings_rejected(self):
        with pytest.raises(ValueError, match="fields must be"):
            odoo_rpc.parse_fields("[1, 2]")


class TestParseIds:
    def test_single(self):
        assert odoo_rpc.parse_ids("5") == [5]

    def test_comma_separated_with_spaces(self):
        assert odoo_rpc.parse_ids("1, 2 ,3") == [1, 2, 3]

    def test_json_array(self):
        assert odoo_rpc.parse_ids("[1,2]") == [1, 2]

    def test_duplicates_dropped_in_order(self):
        assert odoo_rpc.parse_ids("3,1,3,2") == [3, 1, 2]

    def test_empty_rejected(self):
        with pytest.raises(ValueError, match="ids is required"):
            odoo_rpc.parse_ids("")

    def test_non_integer_rejected(self):
        with pytest.raises(ValueError, match="must contain integers"):
            odoo_rpc.parse_ids("a")


class TestParseValues:
    def test_object(self):
        assert odoo_rpc.parse_values('{"name": "x"}') == {"name": "x"}

    def test_list_when_allowed(self):
        assert odoo_rpc.parse_values('[{"a": 1}, {"a": 2}]', allow_list=True) == [
            {"a": 1},
            {"a": 2},
        ]

    def test_list_rejected_when_not_allowed(self):
        with pytest.raises(ValueError, match="values must be a JSON object"):
            odoo_rpc.parse_values('[{"a": 1}]')

    def test_scalar_rejected(self):
        with pytest.raises(ValueError, match="values must be"):
            odoo_rpc.parse_values("42", allow_list=True)


class TestValidateModelAndMethod:
    def test_model_accepted(self):
        assert odoo_rpc.validate_model("res.partner") == "res.partner"

    @pytest.mark.parametrize("bad", ["../../etc", "Res.Partner", "res partner", ""])
    def test_model_rejected(self, bad):
        with pytest.raises(ValueError, match="Invalid model name"):
            odoo_rpc.validate_model(bad)

    def test_private_method_points_at_run_odoo_shell(self):
        with pytest.raises(ValueError, match="run_odoo_shell"):
            odoo_rpc.validate_method("_compute_amount")

    def test_method_rejected(self):
        with pytest.raises(ValueError, match="Invalid method name"):
            odoo_rpc.validate_method("search read")


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class TestHelperFraming:
    def test_round_trip(self):
        status, body = odoo_rpc.parse_helper_stdout(_ok({"a": 1}))
        assert status == 200
        assert json.loads(body) == {"jsonrpc": "2.0", "id": 1, "result": {"a": 1}}

    def test_body_containing_the_end_sentinel_survives(self):
        # Length framing, not sentinels — ORM data may contain any string.
        stdout = _ok({"note": "__END__ and __ODUFLOW_SID__"})
        _status, body = odoo_rpc.parse_helper_stdout(stdout)
        assert json.loads(body)["result"]["note"] == "__END__ and __ODUFLOW_SID__"

    def test_missing_frame_is_an_error(self):
        with pytest.raises(ExternalCommandError, match="no framed response"):
            odoo_rpc.parse_helper_stdout(b"Traceback (most recent call last):")

    def test_truncated_body_is_an_error(self):
        with pytest.raises(ExternalCommandError, match="Truncated response"):
            odoo_rpc.parse_helper_stdout(b"ODUFLOW-RPC 200 99\n{}")


class TestExecRpc:
    def _capture(self, container):
        members = []

        def capture_archive(_path, stream):
            with tarfile.open(fileobj=stream, mode="r") as archive:
                name = archive.getnames()[0]
                members.append((name, archive.extractfile(name).read().decode()))

        container.put_archive.side_effect = capture_archive
        return members

    def test_envelope_and_url(self):
        container = _container(_ok([{"id": 1}]))
        members = self._capture(container)

        odoo_rpc._exec_rpc(
            container,
            "sid-1",
            "res.partner",
            "search_read",
            [[], ["name"]],
            {"limit": 5},
        )

        _name, script = members[0]
        payload = json.loads(_embedded_payload(script))
        assert payload["url"] == (
            "http://127.0.0.1:8069/web/dataset/call_kw/res.partner/search_read"
        )
        assert payload["body"]["jsonrpc"] == "2.0"
        assert payload["body"]["method"] == "call"
        assert payload["body"]["params"] == {
            "model": "res.partner",
            "method": "search_read",
            "args": [[], ["name"]],
            "kwargs": {"limit": 5},
        }
        assert "Cookie" in script and 'P["sid"]' in script

    def test_session_id_never_reaches_argv_or_environ(self):
        container = _container(_ok(True))
        self._capture(container)

        odoo_rpc._exec_rpc(container, "secret-sid", "res.partner", "read", [[1]], {})

        for call in container.exec_run.call_args_list:
            assert "secret-sid" not in " ".join(call.args[0])
            assert "environment" not in call.kwargs

    def test_payload_file_is_root_owned_and_unreadable_by_odoo(self):
        container = _container(_ok(True))
        modes = []

        def capture_archive(_path, stream):
            with tarfile.open(fileobj=stream, mode="r") as archive:
                info = archive.getmembers()[0]
                modes.append((info.mode, info.uid, info.gid))

        container.put_archive.side_effect = capture_archive

        odoo_rpc._exec_rpc(container, "sid", "res.partner", "read", [[1]], {})

        assert modes == [(0o600, 0, 0)]

    def test_each_call_uses_a_distinct_path_and_cleans_up(self):
        container = _container(_ok(True), _ok(True))
        members = self._capture(container)

        odoo_rpc._exec_rpc(container, "sid", "res.partner", "read", [[1]], {})
        odoo_rpc._exec_rpc(container, "sid", "res.partner", "read", [[1]], {})

        names = [name for name, _script in members]
        assert len(set(names)) == 2
        removed = [
            call.args[0][2]
            for call in container.exec_run.call_args_list
            if call.args[0][:2] == ["rm", "-f"]
        ]
        assert sorted(removed) == sorted(f"/tmp/{name}" for name in names)

    def test_connection_refused_maps_to_a_retryable_error(self):
        container = MagicMock()
        container.exec_run.return_value = (4, (b"", b"cannot reach Odoo"))

        with pytest.raises(ConnectionRefusedError):
            odoo_rpc._exec_rpc(container, "sid", "res.partner", "read", [[1]], {})

    def test_oversized_response_names_the_fix(self):
        container = MagicMock()
        container.exec_run.return_value = (5, (b"", b"response is 9999999 bytes"))

        with pytest.raises(ValueError, match="Narrow the query"):
            odoo_rpc._exec_rpc(container, "sid", "res.partner", "read", [[1]], {})

    def test_helper_bounds_success_and_error_response_reads(self):
        script = odoo_rpc.build_helper_script({"sid": "x"})

        assert script.count('read(P["max_bytes"] + 1)') == 2

    def test_non_json_response_is_surfaced(self):
        container = MagicMock()
        container.exec_run.return_value = (0, (b"ODUFLOW-RPC 500 5\n<html", b""))

        with pytest.raises(ExternalCommandError, match="non-JSON response"):
            odoo_rpc._exec_rpc(container, "sid", "res.partner", "read", [[1]], {})


# ---------------------------------------------------------------------------
# Session cache
# ---------------------------------------------------------------------------


class TestSessionCache:
    def test_session_is_minted_once_and_reused(self):
        container = _container(_ok(1), _ok(2))
        with _client(container), _mint() as mint:
            odoo_rpc.call_kw(
                TEST_SETTINGS, TEST_TEAM, "main", "res.partner", "search_count", [[]]
            )
            second = odoo_rpc.call_kw(
                TEST_SETTINGS, TEST_TEAM, "main", "res.partner", "search_count", [[]]
            )

        assert mint.call_count == 1
        assert second.minted is False
        assert second.value == 2

    def test_expired_session_is_reminted(self):
        container = _container(_ok(1), _ok(2))
        with _client(container), _mint() as mint:
            odoo_rpc.call_kw(
                TEST_SETTINGS, TEST_TEAM, "main", "res.partner", "search_count", [[]]
            )
            for session in odoo_rpc._SESSIONS.values():
                session.expires_at = time.time() - 1
            odoo_rpc.call_kw(
                TEST_SETTINGS, TEST_TEAM, "main", "res.partner", "search_count", [[]]
            )

        assert mint.call_count == 2

    def test_resolved_superuser_is_rejected_before_rpc(self):
        container = _container(_ok(1))
        with _client(container), _mint(uid="1"):
            with pytest.raises(ValueError, match="resolved to the superuser"):
                odoo_rpc.call_kw(
                    TEST_SETTINGS,
                    TEST_TEAM,
                    "main",
                    "res.partner",
                    "search_count",
                    [[]],
                    as_user="01",
                )

        rpc_execs = [
            call
            for call in container.exec_run.call_args_list
            if call.args[0][0] == "python3"
        ]
        assert rpc_execs == []

    @pytest.mark.parametrize(
        "invalid",
        [
            _framed({"error": {"code": 100, "message": "Odoo Session Expired"}}),
            _framed(
                {
                    "error": {
                        "code": 200,
                        "data": {"name": "odoo.http.SessionExpiredException"},
                    }
                }
            ),
            _framed({"error": {"code": 200}}, status=401),
        ],
    )
    def test_invalid_session_triggers_exactly_one_remint(self, invalid):
        container = _container(invalid, _ok(7))
        with _client(container), _mint() as mint:
            result = odoo_rpc.call_kw(
                TEST_SETTINGS, TEST_TEAM, "main", "res.partner", "search_count", [[]]
            )

        assert mint.call_count == 2
        assert result.ok is True
        assert result.value == 7

    def test_invalid_twice_gives_up_with_a_pointer_to_run_odoo_shell(self):
        invalid = _framed({"error": {"code": 100, "message": "Odoo Session Expired"}})
        container = _container(invalid, invalid)
        with _client(container), _mint():
            with pytest.raises(ExternalCommandError, match="run_odoo_shell"):
                odoo_rpc.call_kw(
                    TEST_SETTINGS,
                    TEST_TEAM,
                    "main",
                    "res.partner",
                    "search_count",
                    [[]],
                )

    def test_concurrent_calls_mint_once(self):
        container = _container(*[_ok(1)] * 8)
        started = threading.Event()

        def slow_mint(*_args, **_kwargs):
            started.set()
            time.sleep(0.05)
            return {
                "sid": "sid-1",
                "login": "admin",
                "uid": "2",
                "base_url": "",
                "cookie_domain": "",
                "url": "",
                "expires_at": "",
            }

        with (
            _client(container),
            patch(
                "oduflow.docker_ops.odoo_ops.connect_as_user", side_effect=slow_mint
            ) as mint,
        ):
            threads = [
                threading.Thread(
                    target=odoo_rpc.call_kw,
                    args=(
                        TEST_SETTINGS,
                        TEST_TEAM,
                        "main",
                        "res.partner",
                        "search_count",
                        [[]],
                    ),
                )
                for _ in range(8)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        assert mint.call_count == 1

    def test_invalidate_sessions_is_team_and_env_scoped(self):
        session = odoo_rpc._CachedSession("s", "admin", 2, time.time() + 100)
        odoo_rpc._SESSIONS[("1", "main", "")] = session
        odoo_rpc._SESSIONS[("1", "other", "")] = session
        odoo_rpc._SESSIONS[("2", "main", "")] = session

        assert odoo_rpc.invalidate_sessions("1", "main") == 1
        assert set(odoo_rpc._SESSIONS) == {("1", "other", ""), ("2", "main", "")}
        assert odoo_rpc.invalidate_sessions("1") == 1
        assert set(odoo_rpc._SESSIONS) == {("2", "main", "")}

    def test_prune_caps_the_cache(self):
        for index in range(odoo_rpc.MAX_SESSIONS + 5):
            odoo_rpc._SESSIONS[("1", f"env{index}", "")] = odoo_rpc._CachedSession(
                "s", "admin", 2, time.time() + 100 + index
            )
        odoo_rpc._prune_locked()

        assert len(odoo_rpc._SESSIONS) < odoo_rpc.MAX_SESSIONS


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


class TestErrorMapping:
    @pytest.mark.parametrize(
        "name",
        [
            "odoo.exceptions.AccessError",
            "odoo.exceptions.UserError",
            "odoo.exceptions.ValidationError",
            "odoo.exceptions.MissingError",
            "psycopg2.errors.UniqueViolation",
        ],
    )
    def test_odoo_errors_are_results_not_exceptions(self, name):
        container = _container(_odoo_error(name, "nope", "Traceback: line 1"))
        with _client(container), _mint():
            result = odoo_rpc.call_kw(
                TEST_SETTINGS, TEST_TEAM, "main", "res.partner", "write", [[1], {}]
            )

        assert result.ok is False
        assert result.error_name == name
        assert result.error_message == "nope"
        assert "Traceback: line 1" in result.error_debug
        assert result.error_text().startswith(name.rsplit(".", 1)[-1] + ": nope")

    def test_traceback_is_kept_for_the_agent(self):
        container = _container(
            _odoo_error("odoo.exceptions.ValidationError", "bad", "File x, line 9")
        )
        with _client(container), _mint():
            result = odoo_rpc.call_kw(
                TEST_SETTINGS, TEST_TEAM, "main", "res.partner", "create", [{}]
            )

        assert "File x, line 9" in result.error_text()


class TestGeneratedHelper:
    def test_helper_is_valid_python_with_hostile_payload(self):
        # The helper's source is never executed by the unit suite, so a
        # producer-side escaping bug would otherwise pass everything.
        payload = {
            "url": f"{odoo_rpc.ODOO_ORIGIN}/web/dataset/call_kw/res.partner/write",
            "origin": odoo_rpc.ODOO_ORIGIN,
            "cookie": odoo_rpc.SESSION_COOKIE,
            "sid": "quote ' double \" backslash \\ newline \n",
            "timeout": 120,
            "max_bytes": 10,
            "body": {"params": {"args": ["'\"\\\n\t", "__END__"]}},
        }
        script = odoo_rpc.build_helper_script(payload)

        compile(script, "<helper>", "exec")
        assert json.loads(_embedded_payload(script)) == payload

    def test_helper_deletes_itself_before_carrying_the_session_id(self):
        script = odoo_rpc.build_helper_script({"sid": "x"})

        assert script.index("os.unlink(__file__)") < script.index("P = json.loads(")
