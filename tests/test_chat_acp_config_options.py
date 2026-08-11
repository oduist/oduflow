"""Browser-client compatibility with modern ACP session config options."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


_ROOT = Path(__file__).parents[1]
_CHAT_JS = _ROOT / "src" / "oduflow" / "templates" / "static" / "chat.js"
_ACP_JS = _ROOT / "src" / "oduflow" / "templates" / "static" / "acp-client.js"


def _run_node(source: str) -> dict[str, object]:
    result = subprocess.run(
        ["node", "-"],
        input=source,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_chat_normalizes_model_config_option():
    source = _CHAT_JS.read_text(encoding="utf-8")
    body, separator, trailer = source.rpartition("})();")
    assert separator
    instrumented = (
        body
        + "\nwindow.__acpConfigTest = { sessionModels: sessionModels };\n"
        + separator
        + trailer
    )
    harness = (
        "global.window = {};\n"
        "global.document = {};\n"
        + instrumented
        + "\nvar result = window.__acpConfigTest.sessionModels({configOptions:[{\n"
        "  id:'model', category:'model', type:'select', currentValue:'openai/gpt-5',\n"
        "  options:[\n"
        "    {value:'openai/gpt-5', name:'OpenAI/GPT-5'},\n"
        "    {name:'Anthropic', options:[{value:'anthropic/sonnet', name:'Claude Sonnet'}]}\n"
        "  ]\n"
        "}]});\n"
        "process.stdout.write(JSON.stringify(result));\n"
    )

    assert _run_node(harness) == {
        "availableModels": [
            {"modelId": "openai/gpt-5", "name": "OpenAI/GPT-5"},
            {"modelId": "anthropic/sonnet", "name": "Claude Sonnet"},
        ],
        "currentModelId": "openai/gpt-5",
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_acp_client_uses_config_option_setter_only_for_modern_session():
    source = _ACP_JS.read_text(encoding="utf-8")
    harness = (
        "global.window = {};\n" + source + "\n(async function () {\n"
        "  var client = new window.AcpClient('ws://unused');\n"
        "  var calls = [];\n"
        "  client._request = function(method, params) {\n"
        "    calls.push({method:method, params:params});\n"
        "    if (method === 'session/new') return Promise.resolve({\n"
        "      sessionId:'modern', configOptions:[{id:'model', category:'model', options:[]}]\n"
        "    });\n"
        "    return Promise.resolve({});\n"
        "  };\n"
        "  await client.newSession('/workspace');\n"
        "  await client.setModel('modern', 'anthropic/sonnet');\n"
        "  await client.setModel('legacy', 'gpt-test');\n"
        "  process.stdout.write(JSON.stringify({calls:calls}));\n"
        "})().catch(function (error) {\n"
        "  process.stderr.write(String(error && error.stack || error));\n"
        "  process.exit(1);\n"
        "});\n"
    )

    calls = _run_node(harness)["calls"]
    assert calls[1] == {
        "method": "session/set_config_option",
        "params": {
            "sessionId": "modern",
            "configId": "model",
            "value": "anthropic/sonnet",
        },
    }
    assert calls[2] == {
        "method": "session/set_model",
        "params": {"sessionId": "legacy", "modelId": "gpt-test"},
    }
