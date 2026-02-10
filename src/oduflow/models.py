from dataclasses import dataclass


@dataclass(frozen=True)
class EnvironmentRef:
    branch: str
    slug: str
    db_name: str
    odoo_container: str
    labels: dict[str, str]
