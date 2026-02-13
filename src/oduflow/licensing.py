import base64
import json
import logging
import os
import shutil
from dataclasses import dataclass

logger = logging.getLogger("oduflow")

LICENSE_PATH = "/etc/oduflow/license.key"

# License types
TYPE_UNLICENSED = "unlicensed"
TYPE_INDIVIDUAL = "individual"
TYPE_BUSINESS = "business"
TYPE_INTEGRATOR = "integrator"

VALID_TYPES = {TYPE_INDIVIDUAL, TYPE_BUSINESS, TYPE_INTEGRATOR}

# Display labels
TYPE_LABELS = {
    TYPE_UNLICENSED: "UNLICENSED — NON-COMMERCIAL USE ONLY",
    TYPE_INDIVIDUAL: "Licensed to individual",
    TYPE_BUSINESS: "Licensed to company",
    TYPE_INTEGRATOR: "Licensed to Odoo integrator",
}

TYPE_SUFFIXES = {
    TYPE_BUSINESS: " (internal use only)",
}

# RSA public key for license verification
_PUBLIC_KEY_PEM = """\
-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAuWvmwwY7/C1c9W4kU1/V
9o79OypjcOKCGvZ9H1IsmoAHtMHQ/Idfz2Rr/py6UVs37GH2mgA5BVSMA81Gecyb
gX8lvFGxzqh9UvgVpsyWGDFOOyjoQAPSVcbMmQxmYn0AzdyARdBUA62ripmS+m8s
4y12cS5S+ANqhjK83HCVuJMwCxwNFra2QumZ0PePNog+QSv1t74Ky6T6diNsnHGe
44GkmOS1WM1eHUKJ5buI+gupCrapvgwqcAhjE0M8Opj0du3mqDgD3yr+Rtr2qMo0
jEO6kSannZrR42puJ1+rVj10SnZQYDrz+EkC07UsFiTUGIadZ3zw/+AFE6DPaoej
pQIDAQAB
-----END PUBLIC KEY-----
"""


@dataclass(frozen=True)
class LicenseInfo:
    type: str
    name: str
    email: str

    @property
    def label(self) -> str:
        base = TYPE_LABELS.get(self.type, TYPE_LABELS[TYPE_UNLICENSED])
        if self.type == TYPE_UNLICENSED:
            return base
        suffix = TYPE_SUFFIXES.get(self.type, "")
        return f"{base}: {self.name}{suffix}"

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "name": self.name,
            "email": self.email,
            "label": self.label,
        }


_UNLICENSED = LicenseInfo(type=TYPE_UNLICENSED, name="", email="")


def _verify_license_text(raw: str) -> LicenseInfo:
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    raw = raw.strip()
    if "." not in raw:
        raise ValueError("Invalid license format")

    sig_b64, payload_b64 = raw.split(".", 1)
    signature = base64.b64decode(sig_b64)
    payload_bytes = base64.b64decode(payload_b64)

    pub_key = load_pem_public_key(_PUBLIC_KEY_PEM.encode())
    pub_key.verify(
        signature,
        payload_bytes,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )

    data = json.loads(payload_bytes)
    license_type = data.get("type", "")
    if license_type not in VALID_TYPES:
        raise ValueError(f"Unknown license type: {license_type}")

    return LicenseInfo(
        type=license_type,
        name=data.get("name", ""),
        email=data.get("email", ""),
    )


def get_license_info() -> LicenseInfo:
    if not os.path.isfile(LICENSE_PATH):
        return _UNLICENSED
    try:
        raw = open(LICENSE_PATH, "r").read()
        return _verify_license_text(raw)
    except Exception as e:
        logger.warning("Invalid license file %s: %s", LICENSE_PATH, e)
        return _UNLICENSED


def install_license(source_path: str) -> LicenseInfo:
    raw = open(source_path, "r").read()
    info = _verify_license_text(raw)
    os.makedirs(os.path.dirname(LICENSE_PATH), exist_ok=True)
    shutil.copy2(source_path, LICENSE_PATH)
    logger.info("License installed: %s", info.label)
    return info


def install_license_from_text(key_text: str) -> LicenseInfo:
    info = _verify_license_text(key_text)
    os.makedirs(os.path.dirname(LICENSE_PATH), exist_ok=True)
    with open(LICENSE_PATH, "w") as f:
        f.write(key_text.strip())
    logger.info("License installed: %s", info.label)
    return info
