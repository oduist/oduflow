import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    routing_mode: str = "port"
    base_domain: str = ""
    acme_email: str = ""
    external_host: str = "localhost"
    port_range_start: int = 50000
    port_range_end: int = 50100
    workspaces_dir: str = ""
    dump_file_path: str = ""
    ref_filestore_path: str = ""
    db_user: str = "odoo"
    db_password: str = "odoo"
    odoo_image: str = "odoo"
    postgres_image: str = "postgres:15"
    shared_network: str = "flow-net"
    shared_db_container: str = "flow-db"
    shared_db_volume: str = "flow-db-data"
    traefik_container: str = "flow-traefik"
    traefik_acme_volume: str = "flow-traefik-acme"
    flow_server_port: int = 8000
    template_db_name: str = "odoo_ref"
    prefix: str = "flow-"
    branch_label: str = "flow.branch"
    managed_label: str = "flow.managed"
    system_label: str = "flow.system"
    default_branch: str = "prod"
    port_registry_path: str = ""

    @staticmethod
    def from_env() -> "Settings":
        workspaces_dir = os.getenv(
            "FLOW_WORKSPACES_DIR",
            os.path.expanduser("~/.flow/workspaces"),
        )
        return Settings(
            routing_mode=os.getenv("FLOW_ROUTING_MODE", "port").strip().lower(),
            base_domain=re.sub(r"^https?://", "", os.getenv("FLOW_BASE_DOMAIN", "")).strip(),
            acme_email=os.getenv("FLOW_ACME_EMAIL", "").strip(),
            external_host=re.sub(r"^https?://", "", os.getenv("EXTERNAL_HOST", "localhost")),
            port_range_start=int(os.getenv("PORT_RANGE_START", "50000")),
            port_range_end=int(os.getenv("PORT_RANGE_END", "50100")),
            workspaces_dir=workspaces_dir,
            dump_file_path=os.getenv(
                "FLOW_DUMP_PATH",
                os.path.expanduser("~/.flow/odoo_ref.dump"),
            ),
            ref_filestore_path=os.getenv(
                "FLOW_REF_FILESTORE_PATH",
                os.path.expanduser("~/.flow/odoo_ref_filestore"),
            ),
            db_user=os.getenv("ODOO_DB_USER", "odoo"),
            db_password=os.getenv("ODOO_DB_PASSWORD", "odoo"),
            flow_server_port=int(os.getenv("FLOW_PORT", "8000")),
            default_branch=os.getenv("FLOW_DEFAULT_BRANCH", "prod"),
            port_registry_path=os.getenv(
                "FLOW_PORT_REGISTRY",
                os.path.join(os.path.dirname(workspaces_dir), "ports.json"),
            ),
        )

    def validate(self) -> None:
        if self.port_range_start >= self.port_range_end:
            raise ValueError(
                f"Invalid port range: {self.port_range_start}-{self.port_range_end}"
            )
        if not self.workspaces_dir:
            raise ValueError("workspaces_dir must be set")

        if self.routing_mode not in ("port", "traefik"):
            raise ValueError("routing_mode must be 'port' or 'traefik'")

        if self.routing_mode == "traefik":
            if not self.base_domain:
                raise ValueError("base_domain must be set when routing_mode=traefik")
            if not self.acme_email:
                raise ValueError("acme_email must be set when routing_mode=traefik")
