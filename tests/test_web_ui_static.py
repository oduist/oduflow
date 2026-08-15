"""Regression tests for dashboard static asset cache versioning."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oduflow.locking import LockManager
from oduflow.settings import Settings, TeamSettings
from oduflow.web_ui import mount_web_ui

_DASHBOARD = (
    Path(__file__).parents[1] / "src" / "oduflow" / "templates" / "dashboard.html"
)


def _js_function(source: str, name: str) -> str:
    """Extract one top-level dashboard function for a focused Node harness."""
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function {name}")


def _run_node(source: str) -> dict[str, object]:
    result = subprocess.run(
        ["node", "-"],
        input=source,
        text=True,
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def _client(tmp_path) -> TestClient:
    settings = Settings(
        base_data_dir=str(tmp_path),
        teams={"1": TeamSettings(team_id="1")},
    )
    app = Starlette()
    mount_web_ui(app, lambda: settings, LockManager())
    return TestClient(app)


def test_chat_assets_share_one_positive_integer_cache_version(tmp_path):
    client = _client(tmp_path)
    dashboard = client.get("/")
    assert dashboard.status_code == 200

    # chat.js and acp-client.js are an interdependent pair; the dashboard holds
    # ONE shared cache version (CHAT_V) so a single bump busts both at once.
    match = re.search(r"var CHAT_V = '([1-9][0-9]*)'", dashboard.text)
    assert match is not None, "CHAT_V positive integer cache version not found"
    version = match.group(1)

    for filename in ("chat.js", "acp-client.js"):
        # Both assets must reference the shared CHAT_V, not a hardcoded number,
        # so they can never drift out of sync.
        assert re.search(
            rf"/static/{re.escape(filename)}\?v='\s*\+\s*CHAT_V", dashboard.text
        ), f"{filename} does not use the shared CHAT_V cache version"

        versioned = client.get(f"/static/{filename}?v={version}")
        unversioned = client.get(f"/static/{filename}")

        assert versioned.status_code == 200
        assert versioned.headers["content-type"].startswith("application/javascript")
        assert versioned.headers["cache-control"] == "public, max-age=86400"
        assert versioned.content == unversioned.content


def test_environment_metadata_shows_live_mount_path(tmp_path):
    client = _client(tmp_path)
    dashboard = client.get("/")

    assert dashboard.status_code == 200
    assert "env.local_path ? '<span>Live-mount:" in dashboard.text


def test_environment_name_is_copyable(tmp_path):
    dashboard = _client(tmp_path).get("/")

    assert dashboard.status_code == 200
    assert '<span class="branch-name copy-db" role="button" tabindex="0" ' in dashboard.text
    assert (
        'title="Copy environment name" aria-label="Copy environment name" '
        in dashboard.text
    )
    assert "copyToClipboard(\\'' + escAttr(env.branch)" in dashboard.text


def test_connect_modal_emphasizes_primary_action(tmp_path):
    dashboard = _client(tmp_path).get("/")

    assert dashboard.status_code == 200
    assert '<button class="btn" onclick="closeConnect()">Close</button>' in dashboard.text
    assert (
        '<button class="btn-create" id="connect-submit" '
        'onclick="submitConnect()">Connect</button>' in dashboard.text
    )


def test_dashboard_uses_safe_json_response_reader(tmp_path):
    dashboard = _client(tmp_path).get("/")

    assert dashboard.status_code == 200
    assert re.search(r"await\s+\w+\.json\(\)", dashboard.text) is None
    assert "return r.json()" not in dashboard.text
    assert "[408, 502, 503, 504, 524]" in dashboard.text
    assert "The operation may still be running on the server" in dashboard.text


def test_save_as_template_shows_elapsed_progress(tmp_path):
    dashboard = _client(tmp_path).get("/")

    assert dashboard.status_code == 200
    assert 'id="new-tpl-progress" role="status" aria-live="polite"' in dashboard.text
    assert "Saving database and filestore…" in dashboard.text
    assert 'id="new-tpl-elapsed" aria-hidden="true"' in dashboard.text
    assert "elapsed.textContent = _fmtElapsed" in dashboard.text
    assert "progress.textContent = 'Saving database and filestore" not in dashboard.text
    assert "setBusy(branch, 'Saving template')" in dashboard.text


def test_feedback_modal_is_registered_for_escape_key(tmp_path):
    dashboard = _client(tmp_path).get("/")

    assert dashboard.status_code == 200
    assert re.search(r"'feedback-modal':\s*closeFeedbackModal", dashboard.text)


def test_dashboard_accepts_opencode_default_and_labels_it(tmp_path):
    dashboard = _client(tmp_path).get("/")

    assert dashboard.status_code == 200
    assert "data.default === 'opencode'" in dashboard.text
    assert "(agentType === 'opencode' ? 'OpenCode' : 'Claude')" in dashboard.text
    assert "var CHAT_V = '6'" in dashboard.text


def test_minimized_window_dock_has_group_semantics_and_restores_focus(tmp_path):
    dashboard = _client(tmp_path).get("/").text
    assert 'id="min-dock" role="group"' in dashboard

    # closeMinimized must capture returnFocus BEFORE calling closer() (otherwise
    # closer() would tear down the chip and the captured element would already
    # be detached), and the focus call must happen AFTER closer() (so the
    # trigger element is still in the DOM). Asserting on the literal text does
    # not catch a reorder; pin down the order of the three statements.
    match = re.search(
        r"function closeMinimized\(id\) \{(.*?)\}\s*$",
        dashboard,
        re.DOTALL | re.MULTILINE,
    )
    assert match is not None, "closeMinimized function not found in dashboard"
    body = match.group(1)
    capture_at = body.find("returnFocus = _minimized")
    closer_at = body.find("closer()")
    focus_at = body.find("returnFocus.focus()")
    assert 0 <= capture_at < closer_at < focus_at, (
        f"closeMinimized statement order is wrong: "
        f"capture={capture_at}, closer={closer_at}, focus={focus_at}"
    )


def test_odoo_sh_import_exposes_best_effort_addon_policy(tmp_path):
    dashboard = _client(tmp_path).get("/").text

    assert 'id="import-best-effort" disabled' in dashboard
    assert "Continue if some addons are unavailable" in dashboard
    assert (
        "addon_error_policy: document.getElementById('import-best-effort').checked "
        "? 'best_effort' : 'strict'" in dashboard
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_template_settings_form_round_trips_attribute_and_prototype_key_values():
    dashboard = _DASHBOARD.read_text(encoding="utf-8")
    functions = "\n".join(
        _js_function(dashboard, name)
        for name in ("escHtmlAttr", "_tsetNormalizeExtras", "_tsetCollectForm")
    )
    harness = (
        functions
        + r"""
var fields = {
  'template-settings-error': {style: {}},
  'tset-image': {value: 'odoo:19.0'},
  'tset-repo': {value: 'https://example.test/addons.git'},
  'tset-git-user': {value: "O'Reilly"},
  'tset-auto-install': {value: ''},
  'tset-local-path': {value: ''},
  'tset-overlay': {value: 'auto'}
};
function checkbox(value, branch) {
  return {
    value: value,
    closest: function (selector) {
      if (selector !== '.check-item') throw new Error('Unexpected row selector');
      return {
        querySelector: function (inputSelector) {
          if (inputSelector !== '.tset-extra-branch') {
            throw new Error('Unexpected input selector');
          }
          return {value: branch};
        }
      };
    }
  };
}
global.document = {
  querySelectorAll: function () {
    return [
      checkbox('__proto__', "feature'quote"),
      checkbox('missing"repo\\name', 'release\\candidate')
    ];
  },
  querySelector: function () {
    throw new Error('Repository names must not be interpolated into selectors');
  },
  getElementById: function (id) { return fields[id]; }
};
var _templateSettingsMeta = JSON.parse(
  '{"__proto__":{"keep":true},"custom":{"preserved":true}}'
);
var normalized = _tsetNormalizeExtras(JSON.parse(
  '{"__proto__":"main","constructor":"stable"}'
));
var collected = _tsetCollectForm();
process.stdout.write(JSON.stringify({
  escaped: escHtmlAttr("feature'quote&\"<>"),
  normalized: normalized,
  collected: collected
}));
"""
    )

    assert _run_node(harness) == {
        "escaped": "feature'quote&amp;&quot;&lt;&gt;",
        "normalized": {"__proto__": "main", "constructor": "stable"},
        "collected": {
            "__proto__": {"keep": True},
            "custom": {"preserved": True},
            "odoo_image": "odoo:19.0",
            "repo_url": "https://example.test/addons.git",
            "git_user": "O'Reilly",
            "auto_install_modules": "",
            "extra_addons": {
                "__proto__": "feature'quote",
                'missing"repo\\name': "release\\candidate",
            },
            "use_overlay": None,
        },
    }
