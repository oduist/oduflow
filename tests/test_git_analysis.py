import os
import textwrap

from oduflow.git_analysis import (
    classify_changes,
    _get_module_name,
    _is_security_path,
    _is_data_path,
    _extract_field_lines,
    _extract_view_tag_attrs,
)


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


class TestIsDataPath:
    def test_data_dir(self):
        assert _is_data_path("sale/data/sale_data.xml") is True

    def test_not_data(self):
        assert _is_data_path("sale/views/sale_order.xml") is False

    def test_nested_data(self):
        assert _is_data_path("addons_ee/connect_elevenlabs/data/tools.xml") is True


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

    def test_odoo_conf_restart(self):
        result = classify_changes([".oduflow/odoo.conf"], "/tmp")
        assert result["action"] == "restart"
        assert result["details"]["restart_required"] == [".oduflow/odoo.conf"]

    def test_odoo_conf_plus_hot_reload_files_restart(self):
        files = [
            ".oduflow/odoo.conf",
            "sale/views/sale_order.xml",
            "web/static/src/js/app.js",
        ]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "restart"
        assert result["details"]["restart_required"] == [".oduflow/odoo.conf"]

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

    def test_data_xml_triggers_upgrade(self):
        files = ["connect_elevenlabs/data/tools.xml"]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "upgrade"
        assert "connect_elevenlabs" in result["modules_to_upgrade"]

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

    def test_upgrade_beats_odoo_conf_restart(self):
        files = [
            ".oduflow/odoo.conf",
            "sale/security/sale_security.xml",
        ]
        result = classify_changes(files, "/tmp")
        assert result["action"] == "upgrade"
        assert "sale" in result["modules_to_upgrade"]
        assert result["details"]["restart_required"] == [".oduflow/odoo.conf"]

    def test_manifest_version_change(self, tmp_path):
        module_dir = tmp_path / "sale"
        module_dir.mkdir()

        old_manifest = "{'name': 'Sale', 'version': '15.0.1.0.0', 'data': []}"
        new_manifest = "{'name': 'Sale', 'version': '15.0.1.1.0', 'data': []}"
        (module_dir / "__manifest__.py").write_text(new_manifest)

        os.makedirs(tmp_path / ".git", exist_ok=True)

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

        from unittest.mock import patch

        old_manifest = (
            "{'name': 'CRM', 'version': '15.0.1.0.0', 'data': ['views/crm.xml']}"
        )
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

    def test_new_module_install(self, tmp_path):
        module_dir = tmp_path / "addons_veles" / "customer_code"
        module_dir.mkdir(parents=True)
        (module_dir / "__manifest__.py").write_text(
            "{'name': 'Customer Code', 'version': '17.0.1.0.0', 'data': []}"
        )
        models_dir = module_dir / "models"
        models_dir.mkdir()
        (models_dir / "res_partner.py").write_text(
            "from odoo import fields, models\n\n"
            "class ResPartner(models.Model):\n"
            "    customer_code = fields.Char(string='Customer Code')\n"
        )

        import subprocess
        from unittest.mock import patch

        def mock_subprocess_run(cmd, **kwargs):
            raise subprocess.CalledProcessError(128, cmd)

        with patch("subprocess.run", side_effect=mock_subprocess_run):
            files = [
                "addons_veles/customer_code/__manifest__.py",
                "addons_veles/customer_code/__init__.py",
                "addons_veles/customer_code/models/__init__.py",
                "addons_veles/customer_code/models/res_partner.py",
            ]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "install"
            assert result["modules_to_install"] == ["customer_code"]
            assert result["modules_to_upgrade"] == []

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

    def test_py_field_added_triggers_upgrade(self, tmp_path):
        module_dir = tmp_path / "sale" / "models"
        module_dir.mkdir(parents=True)
        (tmp_path / "sale" / "__manifest__.py").write_text(
            "{'name': 'Sale', 'version': '17.0.1.0.0'}"
        )
        (module_dir / "sale.py").write_text(
            textwrap.dedent("""\
            from odoo import fields, models

            class SaleOrder(models.Model):
                name = fields.Char(string='Name')
                customer_code = fields.Char(string='Customer Code')
        """)
        )

        from unittest.mock import patch

        old_source = textwrap.dedent("""\
            from odoo import fields, models

            class SaleOrder(models.Model):
                name = fields.Char(string='Name')
        """)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"stdout": old_source})()

            files = ["sale/models/sale.py"]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "upgrade"
            assert "sale" in result["modules_to_upgrade"]

    def test_py_field_removed_triggers_upgrade(self, tmp_path):
        module_dir = tmp_path / "crm" / "models"
        module_dir.mkdir(parents=True)
        (tmp_path / "crm" / "__manifest__.py").write_text(
            "{'name': 'CRM', 'version': '17.0.1.0.0'}"
        )
        (module_dir / "lead.py").write_text(
            textwrap.dedent("""\
            from odoo import fields, models

            class CrmLead(models.Model):
                name = fields.Char(string='Name')
        """)
        )

        from unittest.mock import patch

        old_source = textwrap.dedent("""\
            from odoo import fields, models

            class CrmLead(models.Model):
                name = fields.Char(string='Name')
                priority = fields.Selection(string='Priority')
        """)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"stdout": old_source})()

            files = ["crm/models/lead.py"]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "upgrade"
            assert "crm" in result["modules_to_upgrade"]

    def test_py_field_param_changed_triggers_upgrade(self, tmp_path):
        module_dir = tmp_path / "sale" / "models"
        module_dir.mkdir(parents=True)
        (tmp_path / "sale" / "__manifest__.py").write_text(
            "{'name': 'Sale', 'version': '17.0.1.0.0'}"
        )
        (module_dir / "sale.py").write_text(
            textwrap.dedent("""\
            from odoo import fields, models

            class SaleOrder(models.Model):
                name = fields.Char(string='Name', required=True)
        """)
        )

        from unittest.mock import patch

        old_source = textwrap.dedent("""\
            from odoo import fields, models

            class SaleOrder(models.Model):
                name = fields.Char(string='Name')
        """)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"stdout": old_source})()

            files = ["sale/models/sale.py"]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "upgrade"
            assert "sale" in result["modules_to_upgrade"]

    def test_manifest_assets_change_with_base_ref(self, tmp_path):
        """Manifest change is detected when base_ref points to a commit
        several steps back (multi-commit pull scenario)."""
        module_dir = tmp_path / "web_ext"
        module_dir.mkdir()

        old_manifest = "{'name': 'Web Ext', 'version': '17.0.1.0.0', 'assets': {}}"
        new_manifest = (
            "{'name': 'Web Ext', 'version': '17.0.1.0.0', "
            "'assets': {'web.assets_backend': ['web_ext/static/src/js/app.js']}}"
        )
        (module_dir / "__manifest__.py").write_text(new_manifest)

        from unittest.mock import patch

        fake_base_ref = "abc123"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"stdout": old_manifest})()

            files = [
                "web_ext/__manifest__.py",
                "web_ext/static/src/js/app.js",
            ]
            result = classify_changes(files, str(tmp_path), base_ref=fake_base_ref)
            assert result["action"] == "upgrade"
            assert "web_ext" in result["modules_to_upgrade"]

            # Verify git show used base_ref, not HEAD~1
            git_show_calls = [
                c
                for c in mock_run.call_args_list
                if any(f"{fake_base_ref}:" in str(a) for a in c[0][0])
            ]
            assert len(git_show_calls) == 1

    def test_py_no_field_change_stays_restart(self, tmp_path):
        module_dir = tmp_path / "sale" / "models"
        module_dir.mkdir(parents=True)
        (tmp_path / "sale" / "__manifest__.py").write_text(
            "{'name': 'Sale', 'version': '17.0.1.0.0'}"
        )
        (module_dir / "sale.py").write_text(
            textwrap.dedent("""\
            from odoo import fields, models

            class SaleOrder(models.Model):
                name = fields.Char(string='Name')

                def action_confirm(self):
                    return True
        """)
        )

        from unittest.mock import patch

        old_source = textwrap.dedent("""\
            from odoo import fields, models

            class SaleOrder(models.Model):
                name = fields.Char(string='Name')

                def action_confirm(self):
                    pass
        """)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"stdout": old_source})()

            files = ["sale/models/sale.py"]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "restart"
            assert result["modules_to_upgrade"] == []


class TestExtractViewTagAttrs:
    def test_tree_with_attrs(self):
        source = '<tree editable="bottom" string="Items">'
        assert _extract_view_tag_attrs(source) == {
            '<tree editable="bottom" string="Items">'
        }

    def test_form(self):
        source = '<form string="Sale Order">'
        assert _extract_view_tag_attrs(source) == {'<form string="Sale Order">'}

    def test_list_tag(self):
        source = '<list editable="top">'
        assert _extract_view_tag_attrs(source) == {'<list editable="top">'}

    def test_no_view_tags(self):
        source = '<field name="partner_id"/>'
        assert _extract_view_tag_attrs(source) == set()

    def test_self_closing(self):
        source = '<tree string="Items"/>'
        assert _extract_view_tag_attrs(source) == {'<tree string="Items"/>'}

    def test_multiple_tags(self):
        source = '<tree editable="bottom">\n</tree>\n<form string="Order">\n</form>'
        result = _extract_view_tag_attrs(source)
        assert result == {'<tree editable="bottom">', '<form string="Order">'}


class TestXmlViewAttrUpgrade:
    def test_tree_attr_change_triggers_upgrade(self, tmp_path):
        module_dir = tmp_path / "sale" / "views"
        module_dir.mkdir(parents=True)
        (tmp_path / "sale" / "__manifest__.py").write_text(
            "{'name': 'Sale', 'version': '17.0.1.0.0'}"
        )
        (module_dir / "sale_order.xml").write_text(
            '<odoo><record id="view" model="ir.ui.view"><field name="arch" type="xml">'
            '<tree editable="top"><field name="name"/></tree>'
            "</field></record></odoo>"
        )

        from unittest.mock import patch

        old_xml = (
            '<odoo><record id="view" model="ir.ui.view"><field name="arch" type="xml">'
            '<tree editable="bottom"><field name="name"/></tree>'
            "</field></record></odoo>"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"stdout": old_xml})()

            files = ["sale/views/sale_order.xml"]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "upgrade"
            assert "sale" in result["modules_to_upgrade"]

    def test_form_attr_change_triggers_upgrade(self, tmp_path):
        module_dir = tmp_path / "crm" / "views"
        module_dir.mkdir(parents=True)
        (tmp_path / "crm" / "__manifest__.py").write_text(
            "{'name': 'CRM', 'version': '17.0.1.0.0'}"
        )
        (module_dir / "crm_lead.xml").write_text(
            '<form string="Lead" create="false">'
        )

        from unittest.mock import patch

        old_xml = '<form string="Lead">'
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"stdout": old_xml})()

            files = ["crm/views/crm_lead.xml"]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "upgrade"
            assert "crm" in result["modules_to_upgrade"]

    def test_xml_no_view_attr_change_stays_refresh(self, tmp_path):
        module_dir = tmp_path / "sale" / "views"
        module_dir.mkdir(parents=True)
        (tmp_path / "sale" / "__manifest__.py").write_text(
            "{'name': 'Sale', 'version': '17.0.1.0.0'}"
        )
        (module_dir / "sale_order.xml").write_text(
            '<tree editable="bottom"><field name="name"/><field name="date"/></tree>'
        )

        from unittest.mock import patch

        old_xml = '<tree editable="bottom"><field name="name"/></tree>'
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = type("Result", (), {"stdout": old_xml})()

            files = ["sale/views/sale_order.xml"]
            result = classify_changes(files, str(tmp_path))
            assert result["action"] == "refresh"
            assert result["modules_to_upgrade"] == []


class TestExtractFieldLines:
    def test_basic(self):
        source = "    name = fields.Char(string='Name')\n"
        assert _extract_field_lines(source) == {"name = fields.Char(string='Name')"}

    def test_no_spaces(self):
        source = "customer_code=fields.Char(string='Code')\n"
        assert _extract_field_lines(source) == {
            "customer_code=fields.Char(string='Code')"
        }

    def test_many2one(self):
        source = "    partner_id = fields.Many2one('res.partner')\n"
        assert _extract_field_lines(source) == {
            "partner_id = fields.Many2one('res.partner')"
        }

    def test_no_fields(self):
        source = "def create(self, vals):\n    return super().create(vals)\n"
        assert _extract_field_lines(source) == set()


class TestMergeRecommendations:
    def test_empty(self):
        from oduflow.git_analysis import merge_recommendations

        assert merge_recommendations([]) == {
            "action": "none",
            "modules_to_install": [],
            "modules_to_upgrade": [],
        }

    def test_picks_most_disruptive_action_and_unions_modules(self):
        from oduflow.git_analysis import merge_recommendations

        main = {
            "action": "restart",
            "modules_to_install": [],
            "modules_to_upgrade": [],
            "details": {"restart_required": True},
        }
        extra = {
            "action": "install",
            "modules_to_install": ["enterprise_mod"],
            "modules_to_upgrade": ["other"],
            "details": {"restart_required": False},
        }
        merged = merge_recommendations([main, extra])
        # install (from the extra-addon repo) outranks restart (from the main repo)
        assert merged["action"] == "install"
        assert merged["modules_to_install"] == ["enterprise_mod"]
        assert merged["modules_to_upgrade"] == ["other"]
        assert merged["details"]["restart_required"] is True

    def test_upgrade_in_extra_addon_not_lost(self):
        from oduflow.git_analysis import merge_recommendations

        main = {"action": "none", "modules_to_install": [], "modules_to_upgrade": []}
        extra = {
            "action": "upgrade",
            "modules_to_install": [],
            "modules_to_upgrade": ["sale_enterprise"],
            "details": {"restart_required": False},
        }
        merged = merge_recommendations([main, extra])
        assert merged["action"] == "upgrade"
        assert merged["modules_to_upgrade"] == ["sale_enterprise"]
