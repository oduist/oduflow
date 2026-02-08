import os
import textwrap
import pytest

from flow.git_analysis import classify_changes, _get_module_name, _is_security_path


class TestGetModuleName:
    def test_module_file(self):
        assert _get_module_name("sale/models/sale.py") == "sale"

    def test_root_file(self):
        assert _get_module_name("setup.py") is None

    def test_nested(self):
        assert _get_module_name("crm/views/crm_lead.xml") == "crm"


class TestIsSecurityPath:
    def test_security_dir(self):
        assert _is_security_path("sale/security/ir.model.access.csv") is True

    def test_not_security(self):
        assert _is_security_path("sale/views/sale_order.xml") is False

    def test_security_xml(self):
        assert _is_security_path("crm/security/crm_security.xml") is True


class TestClassifyChanges:
    def test_empty(self):
        result = classify_changes([], "/tmp")
        assert result["action"] == "none"

    def test_only_xml_not_security(self):
        files = ["sale/views/sale_order.xml", "crm/views/crm_lead.xml"]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "refresh"
        assert result["modules_to_upgrade"] == []

    def test_py_files(self):
        files = ["sale/models/sale.py"]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "restart"

    def test_security_xml(self):
        files = ["sale/security/ir.model.access.csv"]
        result = classify_changes(files, "/tmp")
        # csv is not xml, should be refresh
        assert result["action"] == "refresh"

    def test_security_xml_file(self):
        files = ["sale/security/sale_security.xml"]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "upgrade"
        assert "sale" in result["modules_to_upgrade"]

    def test_js_only(self):
        files = ["web/static/src/js/app.js"]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "refresh"

    def test_py_plus_xml(self):
        files = ["sale/models/sale.py", "sale/views/sale_order.xml"]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "restart"

    def test_upgrade_beats_restart(self):
        files = [
            "sale/models/sale.py",
            "sale/security/sale_security.xml",
        ]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "upgrade"
        assert "sale" in result["modules_to_upgrade"]

    def test_manifest_version_change(self, tmp_path):
        module_dir = tmp_path / "sale"
        module_dir.mkdir()

        old_manifest = "{'name': 'Sale', 'version': '15.0.1.0.0', 'data': []}"
        new_manifest = "{'name': 'Sale', 'version': '15.0.1.1.0', 'data': []}"
        (module_dir / "__manifest__.py").write_text(new_manifest)

        os.makedirs(tmp_path / ".git", exist_ok=True)

        import subprocess
        from unittest.mock import patch

        with patch("subprocess.run") as mock_run:
            mock_rev = type("Result", (), {"stdout": old_manifest})()
            mock_run.return_value = mock_rev

            files = ["sale/__manifest__.py"]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "upgrade"
            assert "sale" in result["modules_to_upgrade"]

    def test_manifest_data_list_change(self, tmp_path):
        module_dir = tmp_path / "crm"
        module_dir.mkdir()

        new_manifest = "{'name': 'CRM', 'version': '15.0.1.0.0', 'data': ['views/crm.xml', 'views/new.xml']}"
        (module_dir / "__manifest__.py").write_text(new_manifest)

        import subprocess
        from unittest.mock import patch

        old_manifest = "{'name': 'CRM', 'version': '15.0.1.0.0', 'data': ['views/crm.xml']}"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"stdout": old_manifest})()

            files = ["crm/__manifest__.py"]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "upgrade"
            assert "crm" in result["modules_to_upgrade"]

    def test_manifest_no_significant_change(self, tmp_path):
        module_dir = tmp_path / "sale"
        module_dir.mkdir()

        new_manifest = "{'name': 'Sale Updated', 'version': '15.0.1.0.0', 'data': []}"
        (module_dir / "__manifest__.py").write_text(new_manifest)

        import subprocess
        from unittest.mock import patch

        old_manifest = "{'name': 'Sale', 'version': '15.0.1.0.0', 'data': []}"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"stdout": old_manifest})()

            files = ["sale/__manifest__.py"]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "refresh"

    def test_multiple_modules_upgrade(self):
        files = [
            "sale/security/sale_security.xml",
            "crm/security/crm_security.xml",
        ]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "upgrade"
        assert sorted(result["modules_to_upgrade"]) == ["crm", "sale"]

    def test_nested_addon_dir(self, tmp_path):
        module_dir = tmp_path / "addons_veles" / "customer_code"
        module_dir.mkdir(parents=True)
        (module_dir / "__manifest__.py").write_text(
            "{'name': 'Customer Code', 'version': '17.0.1.0.0', 'data': []}"
        )

        from unittest.mock import patch

        old_manifest = "{'name': 'Customer Code', 'version': '17.0.0.0.0', 'data': []}"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"stdout": old_manifest})()

            files = ["addons_veles/customer_code/__manifest__.py"]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "upgrade"
            assert result["modules_to_upgrade"] == ["customer_code"]

    def test_nested_addon_dir_py_file(self, tmp_path):
        module_dir = tmp_path / "addons_ee" / "sale_ext"
        module_dir.mkdir(parents=True)
        (module_dir / "__manifest__.py").write_text(
            "{'name': 'Sale Ext', 'version': '17.0.1.0.0'}"
        )

        files = ["addons_ee/sale_ext/security/ir.model.access.xml"]
        result = classify_changes(files, str(tmp_path))
        assert result["action"] == "upgrade"
        assert result["modules_to_upgrade"] == ["sale_ext"]
