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
    shared_network: str = "oduflow-net"
    shared_db_container: str = "oduflow-db"
    shared_db_volume: str = "oduflow-db-data"
    traefik_container: str = "oduflow-traefik"
    traefik_acme_volume: str = "oduflow-traefik-acme"
    flow_server_port: int = 8000
    template_db_name: str = "odoo_ref"
    prefix: str = "oduflow-"
    branch_label: str = "oduflow.branch"
    managed_label: str = "oduflow.managed"
    system_label: str = "oduflow.system"
    repo_label: str = "oduflow.repo"
    image_label: str = "oduflow.image"
    default_branch: str = "prod"
    port_registry_path: str = ""

    @staticmethod
    def from_env() -> "Settings":
        flow_home = os.getenv("ODUFLOW_HOME", "/srv/oduflow_data")
        return Settings(
            routing_mode=os.getenv("ODUFLOW_ROUTING_MODE", "port").strip().lower(),
            base_domain=re.sub(r"^https?://", "", os.getenv("ODUFLOW_BASE_DOMAIN", "")).strip(),
            acme_email=os.getenv("ODUFLOW_ACME_EMAIL", "").strip(),
            external_host=re.sub(r"^https?://", "", os.getenv("EXTERNAL_HOST", "localhost")),
            port_range_start=int(os.getenv("PORT_RANGE_START", "50000")),
            port_range_end=int(os.getenv("PORT_RANGE_END", "50100")),
            workspaces_dir=os.getenv(
                "ODUFLOW_WORKSPACES_DIR",
                os.path.join(flow_home, "workspaces"),
            ),
            dump_file_path=os.getenv(
                "ODUFLOW_DUMP_PATH",
                os.path.join(flow_home, "odoo_ref.dump"),
            ),
            ref_filestore_path=os.getenv(
                "ODUFLOW_REF_FILESTORE_PATH",
                os.path.join(flow_home, "odoo_ref_filestore"),
            ),
            db_user=os.getenv("ODOO_DB_USER", "odoo"),
            db_password=os.getenv("ODOO_DB_PASSWORD", "odoo"),
            flow_server_port=int(os.getenv("ODUFLOW_PORT", "8000")),
            default_branch=os.getenv("ODUFLOW_DEFAULT_BRANCH", "prod"),
            port_registry_path=os.getenv(
                "ODUFLOW_PORT_REGISTRY",
                os.path.join(flow_home, "ports.json"),
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
