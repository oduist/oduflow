import pathlib
import sys

from oduflow.server import _inject_db_password
from oduflow.settings import Settings

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def test_inject_into_database_section():
    text = '[database]\nuser = "odoo"\nimage = "postgres:15"\n'
    rendered = _inject_db_password(text, "s3cret-token")
    data = tomllib.loads(rendered)
    assert data["database"]["password"] == "s3cret-token"
    assert data["database"]["user"] == "odoo"
    assert data["database"]["image"] == "postgres:15"


def test_inject_appends_section_when_missing():
    text = "[server]\nport = 8000\n"
    rendered = _inject_db_password(text, "abc123")
    data = tomllib.loads(rendered)
    assert data["database"]["password"] == "abc123"
    assert data["server"]["port"] == 8000


def test_bundled_template_gets_generated_password():
    bundled = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "oduflow"
        / "templates"
        / "oduflow.toml"
    )
    text = bundled.read_text(encoding="utf-8")
    # Template must ship without a hardcoded password.
    assert tomllib.loads(text).get("database", {}).get("password") is None

    rendered = _inject_db_password(text, "generated-pw-xyz")
    data = tomllib.loads(rendered)
    assert data["database"]["password"] == "generated-pw-xyz"
    assert data["database"]["password"] != "odoo"


def test_rendered_template_loads_via_settings(tmp_path):
    bundled = (
        pathlib.Path(__file__).resolve().parents[1]
        / "src"
        / "oduflow"
        / "templates"
        / "oduflow.toml"
    )
    rendered = _inject_db_password(
        bundled.read_text(encoding="utf-8"), "live-secret-42"
    )
    dest = tmp_path / "oduflow.toml"
    dest.write_text(rendered, encoding="utf-8")

    settings = Settings.from_toml(str(dest))
    assert settings.db_password == "live-secret-42"
    assert settings.db_user == "odoo"
