import pytest
from cryptography.exceptions import InvalidSignature

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


class TestVerifyLicenseText:
    """Signature verification for the license key.

    Every other test in this module patches ``_verify_license_text`` out, so
    the actual crypto had no coverage: a mutant that skipped the signature
    check, or accepted an unknown license type, passed unnoticed. The tests
    below swap in a throwaway RSA key pair so real signatures can be produced
    and the genuine failure paths exercised.
    """

    @staticmethod
    def _keypair():
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            PublicFormat,
        )

        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = private.public_key().public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
        )
        return private, pem.decode()

    @staticmethod
    def _sign(private, payload: dict) -> str:
        import base64
        import json

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        payload_bytes = json.dumps(payload).encode()
        signature = private.sign(
            payload_bytes,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return (
            base64.b64encode(signature).decode()
            + "."
            + base64.b64encode(payload_bytes).decode()
        )

    @pytest.fixture
    def signer(self, monkeypatch):
        private, pem = self._keypair()
        monkeypatch.setattr(licensing, "_PUBLIC_KEY_PEM", pem)
        return lambda payload: self._sign(private, payload)

    def test_valid_license_is_accepted(self, signer):
        key = signer({"type": "business", "name": "Acme", "email": "ops@acme.example"})

        info = licensing._verify_license_text(key)

        assert info == LicenseInfo(
            type="business", name="Acme", email="ops@acme.example"
        )

    def test_surrounding_whitespace_is_tolerated(self, signer):
        key = signer({"type": "individual", "name": "Ada", "email": "a@b.co"})

        assert licensing._verify_license_text(f"\n  {key}  \n").name == "Ada"

    def test_missing_fields_default_to_empty_strings(self, signer):
        info = licensing._verify_license_text(signer({"type": "individual"}))

        assert info.name == ""
        assert info.email == ""

    def test_a_tampered_payload_is_rejected(self, signer):
        # The whole point of the signature: edit the payload, keep the
        # signature, and verification must fail.
        import base64
        import json

        key = signer({"type": "individual", "name": "Ada", "email": "a@b.co"})
        sig_b64, _ = key.split(".", 1)
        forged = json.dumps(
            {"type": "integrator", "name": "Mallory", "email": "m@evil.example"}
        ).encode()
        tampered = sig_b64 + "." + base64.b64encode(forged).decode()

        with pytest.raises(InvalidSignature):
            licensing._verify_license_text(tampered)

    def test_a_license_signed_by_another_key_is_rejected(self, monkeypatch):
        other_private, _ = self._keypair()
        _, our_pem = self._keypair()
        monkeypatch.setattr(licensing, "_PUBLIC_KEY_PEM", our_pem)
        key = self._sign(other_private, {"type": "business", "name": "Acme"})

        with pytest.raises(InvalidSignature):
            licensing._verify_license_text(key)

    def test_unknown_license_type_is_rejected(self, signer):
        key = signer({"type": "enterprise-plus", "name": "Acme"})

        with pytest.raises(ValueError, match="Unknown license type"):
            licensing._verify_license_text(key)

    def test_missing_type_is_rejected(self, signer):
        with pytest.raises(ValueError, match="Unknown license type"):
            licensing._verify_license_text(signer({"name": "Acme"}))

    @pytest.mark.parametrize("valid_type", ["individual", "business", "integrator"])
    def test_all_three_license_types_are_accepted(self, signer, valid_type):
        info = licensing._verify_license_text(signer({"type": valid_type}))

        assert info.type == valid_type

    def test_text_without_a_separator_is_rejected(self):
        with pytest.raises(ValueError, match="Invalid license format"):
            licensing._verify_license_text("not-a-license")

    def test_empty_text_is_rejected(self):
        with pytest.raises(ValueError, match="Invalid license format"):
            licensing._verify_license_text("   \n ")

    def test_only_the_first_separator_splits_signature_from_payload(self, signer):
        # Base64 payloads never contain '.', but splitting on the last one
        # would still be wrong; pin the behaviour.
        key = signer({"type": "individual", "name": "Ada"})
        assert key.count(".") == 1
        assert licensing._verify_license_text(key).name == "Ada"

    def test_get_license_info_downgrades_an_invalid_file(self, tmp_path, caplog):
        # An unreadable or forged license must not crash the server; it falls
        # back to unlicensed.
        (tmp_path / "license.key").write_text("garbage", encoding="utf-8")

        info = licensing.get_license_info(etc_dir=str(tmp_path))

        assert info.type == licensing.TYPE_UNLICENSED

    def test_install_license_from_text_rejects_a_bad_key(self, tmp_path):
        with pytest.raises(ValueError):
            licensing.install_license_from_text("garbage", etc_dir=str(tmp_path))

        assert not (tmp_path / "license.key").exists()


class TestLicenseLabel:
    def test_unlicensed_label_has_no_name(self):
        assert licensing.LicenseInfo("unlicensed", "Ada", "a@b.co").label == (
            "UNLICENSED — NON-COMMERCIAL USE ONLY"
        )

    def test_business_label_carries_the_internal_use_suffix(self):
        assert licensing.LicenseInfo("business", "Acme", "a@b.co").label == (
            "Licensed to company: Acme (internal use only)"
        )

    def test_individual_and_integrator_labels_have_no_suffix(self):
        assert licensing.LicenseInfo("individual", "Ada", "a@b.co").label == (
            "Licensed to individual: Ada"
        )
        assert licensing.LicenseInfo("integrator", "Acme", "a@b.co").label == (
            "Licensed to Odoo integrator: Acme"
        )

    def test_unknown_type_falls_back_to_the_unlicensed_label(self):
        assert licensing.LicenseInfo("bogus", "Ada", "a@b.co").label.startswith(
            "UNLICENSED"
        )

    def test_to_dict_includes_the_rendered_label(self):
        info = licensing.LicenseInfo("individual", "Ada", "a@b.co")
        assert info.to_dict() == {
            "type": "individual",
            "name": "Ada",
            "email": "a@b.co",
            "label": "Licensed to individual: Ada",
        }
