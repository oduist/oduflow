from oduflow import licensing
from oduflow.licensing import TYPE_BUSINESS, TYPE_INDIVIDUAL, LicenseInfo


def _info() -> LicenseInfo:
    return LicenseInfo(type=TYPE_INDIVIDUAL, name="Ada", email="ada@example.com")


def test_install_license_from_text_writes_to_etc_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(licensing, "_verify_license_text", lambda raw: _info())

    info = licensing.install_license_from_text(" fake-key \n", etc_dir=str(tmp_path))

    assert info == _info()
    assert (tmp_path / "license.key").read_text(encoding="utf-8") == "fake-key"


def test_get_license_info_reads_from_etc_dir(tmp_path, monkeypatch):
    license_path = tmp_path / "license.key"
    license_path.write_text("fake-key", encoding="utf-8")
    seen = {}

    def verify(raw: str) -> LicenseInfo:
        seen["raw"] = raw
        return _info()

    monkeypatch.setattr(licensing, "_verify_license_text", verify)

    assert licensing.get_license_info(etc_dir=str(tmp_path)) == _info()
    assert seen["raw"] == "fake-key"


def test_license_path_falls_back_to_resolved_etc_dir(tmp_path, monkeypatch):
    fallback = tmp_path / "conf"

    def resolve_etc_dir() -> str:
        return str(fallback)

    monkeypatch.setattr("oduflow.settings._resolve_etc_dir", resolve_etc_dir)

    assert licensing.get_license_path() == str(fallback / "license.key")


def test_install_license_copies_to_etc_dir(tmp_path, monkeypatch):
    source = tmp_path / "source.key"
    dest_dir = tmp_path / "conf"
    source.write_text("fake-file-key", encoding="utf-8")
    expected = LicenseInfo(type=TYPE_BUSINESS, name="Oduist", email="ops@example.com")
    monkeypatch.setattr(licensing, "_verify_license_text", lambda raw: expected)

    assert licensing.install_license(str(source), etc_dir=str(dest_dir)) == expected
    assert (dest_dir / "license.key").read_text(encoding="utf-8") == "fake-file-key"
