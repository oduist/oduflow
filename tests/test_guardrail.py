"""Unit tests for the apply guardrail: shallow_classify, recommend, and
guardrail_warnings (pure functions — no Docker, no git)."""

from oduflow.git_analysis import guardrail_warnings, recommend, shallow_classify


class TestShallowClassify:
    def test_empty(self):
        assert shallow_classify([])["action"] == "none"

    def test_python_only_restart(self):
        r = shallow_classify(["sale/models/sale.py"])
        assert r["action"] == "restart"

    def test_security_xml_upgrade(self):
        r = shallow_classify(["sale/security/groups.xml"])
        assert r["action"] == "upgrade"
        assert "sale" in r["modules_to_upgrade"]

    def test_data_xml_upgrade(self):
        r = shallow_classify(["sale/data/cron.xml"])
        assert r["action"] == "upgrade"
        assert "sale" in r["modules_to_upgrade"]

    def test_view_xml_refresh(self):
        r = shallow_classify(["sale/views/sale_views.xml"])
        assert r["action"] == "refresh"

    def test_new_manifest_upgrade(self):
        r = shallow_classify(["sale/__manifest__.py"])
        assert r["action"] == "upgrade"
        assert "sale" in r["modules_to_upgrade"]

    def test_js_refresh(self):
        r = shallow_classify(["sale/static/src/js/widget.js"])
        assert r["action"] == "refresh"

    def test_upgrade_beats_restart(self):
        # A module with both a .py and a security XML change → upgrade wins.
        r = shallow_classify(["sale/models/sale.py", "sale/security/groups.xml"])
        assert r["action"] == "upgrade"


class TestRecommend:
    def test_no_base_ref_uses_shallow(self):
        # Without git there is no old content; a .py change can only be a
        # restart (field changes are undetectable) — shallow path.
        r = recommend(["sale/models/sale.py"], "/tmp/does-not-exist", None)
        assert r["action"] == "restart"

    def test_no_base_ref_security_upgrade(self):
        r = recommend(["sale/security/groups.xml"], "/tmp/does-not-exist", None)
        assert r["action"] == "upgrade"
        assert "sale" in r["modules_to_upgrade"]


class TestGuardrailWarnings:
    def test_no_warnings_when_upgrade_covered(self):
        rec = {
            "action": "upgrade",
            "modules_to_install": [],
            "modules_to_upgrade": ["sale"],
        }
        assert guardrail_warnings(rec, [], ["sale"], False) == []

    def test_missing_upgrade_warns(self):
        rec = {
            "action": "upgrade",
            "modules_to_install": [],
            "modules_to_upgrade": ["sale"],
        }
        # Agent only asked for a restart — the DB-data change is unapplied.
        warns = guardrail_warnings(rec, [], [], True)
        assert any("sale" in w and "-u" in w for w in warns)

    def test_missing_install_warns(self):
        rec = {
            "action": "install",
            "modules_to_install": ["newmod"],
            "modules_to_upgrade": [],
        }
        warns = guardrail_warnings(rec, [], [], False)
        assert any("newmod" in w and "-i" in w for w in warns)

    def test_python_restart_missing_warns(self):
        rec = {
            "action": "restart",
            "modules_to_install": [],
            "modules_to_upgrade": [],
        }
        warns = guardrail_warnings(rec, [], [], False)
        assert any("restart" in w.lower() for w in warns)

    def test_install_request_covers_recommended_upgrade(self):
        # Requesting -i for a module also satisfies a recommended -u for it.
        rec = {
            "action": "upgrade",
            "modules_to_install": [],
            "modules_to_upgrade": ["sale"],
        }
        assert guardrail_warnings(rec, ["sale"], [], False) == []

    def test_over_request_does_not_warn(self):
        # Asking for more than recommended is allowed (no warning).
        rec = {
            "action": "refresh",
            "modules_to_install": [],
            "modules_to_upgrade": [],
        }
        assert guardrail_warnings(rec, [], ["sale"], False) == []
