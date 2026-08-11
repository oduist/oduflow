"""Behavioral coverage for Agent Chat attachment prompt construction."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_CHAT_JS = (
    Path(__file__).parents[1] / "src" / "oduflow" / "templates" / "static" / "chat.js"
)


def _prompt_scenarios() -> dict[str, list[dict[str, object]]]:
    source = _CHAT_JS.read_text(encoding="utf-8")
    marker = "})();"
    body, separator, trailer = source.rpartition(marker)
    assert separator, "chat.js IIFE terminator not found"
    instrumented = (
        body
        + "\nwindow.__chatPromptTest = { buildPromptBlocks: buildPromptBlocks };\n"
        + separator
        + trailer
    )

    harness = (
        "global.window = {};\n"
        "global.document = {};\n"
        "global.FileReader = function () {};\n"
        "global.FileReader.prototype.readAsDataURL = function (file) {\n"
        "  if (file.fail) { this.onerror(); return; }\n"
        "  this.result = file.dataUrl;\n"
        "  this.onload();\n"
        "};\n" + instrumented + "\n(async function () {\n"
        "  var attachment = {\n"
        "    id: 'abc123',\n"
        "    name: 'pool.png',\n"
        "    path: '/workspace/.oduflow-uploads/main/abc123/pool.png',\n"
        "    uri: 'file:///workspace/.oduflow-uploads/main/abc123/pool.png',\n"
        "    mimeType: 'image/png',\n"
        "    size: 3,\n"
        "    file: { dataUrl: 'data:image/png;base64,aW1n' }\n"
        "  };\n"
        "  var build = window.__chatPromptTest.buildPromptBlocks;\n"
        "  var inline = await build(\n"
        "    { promptCapabilities: { image: true } }, 'Use this image', [attachment]\n"
        "  );\n"
        "  var fallback = await build(\n"
        "    { promptCapabilities: {} }, 'Use this image', [attachment]\n"
        "  );\n"
        "  attachment.file = { fail: true };\n"
        "  var readFailure = await build(\n"
        "    { promptCapabilities: { image: true } }, 'Use this image', [attachment]\n"
        "  );\n"
        "  process.stdout.write(JSON.stringify({ inline, fallback, readFailure }));\n"
        "})().catch(function (error) {\n"
        "  process.stderr.write(String(error && error.stack || error));\n"
        "  process.exit(1);\n"
        "});\n"
    )
    result = subprocess.run(
        ["node", "-"],
        input=harness,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_inline_image_keeps_resource_link_and_visual_block():
    scenarios = _prompt_scenarios()

    inline = scenarios["inline"]
    assert [block["type"] for block in inline] == ["text", "resource_link", "image"]
    assert inline[1]["uri"] == inline[2]["uri"]
    assert (
        inline[1]["_meta"]["oduflow"]["path"] == inline[2]["_meta"]["oduflow"]["path"]
    )
    assert inline[2]["data"] == "aW1n"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_non_inline_and_failed_image_reads_emit_one_resource_link():
    scenarios = _prompt_scenarios()

    assert [block["type"] for block in scenarios["fallback"]] == [
        "text",
        "resource_link",
    ]
    assert [block["type"] for block in scenarios["readFailure"]] == [
        "text",
        "resource_link",
    ]
