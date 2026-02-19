import os
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    instance_id: str = "1"
    routing_mode: str = "port"
    base_domain: str = ""
    acme_email: str = ""
    external_host: str = "localhost"
    port_range_start: int = 50000
    port_range_end: int = 50100
    workspaces_dir: str = ""
    home: str = ""
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
    prefix: str = "oduflow-"
    branch_label: str = "oduflow.branch"
    instance_label: str = "oduflow.instance"
    managed_label: str = "oduflow.managed"
    system_label: str = "oduflow.system"
    repo_label: str = "oduflow.repo"
    image_label: str = "oduflow.image"
    default_branch: str = "prod"
    port_registry_path: str = ""
    overlay_threshold_mb: int = 50
    stateless_http: bool = True

    def get_template_dir(self, template_name: str) -> str:
        return os.path.join(self.home, "templates", template_name)

    def get_template_sql_path(self, template_name: str) -> str:
        tpl_dir = self.get_template_dir(template_name)
        pgdump = os.path.join(tpl_dir, "dump.pgdump")
        if os.path.isfile(pgdump):
            return pgdump
        sql = os.path.join(tpl_dir, "dump.sql")
        if os.path.isfile(sql):
            return sql
        # Default for new templates
        return pgdump

    def get_template_filestore_path(self, template_name: str) -> str:
        return os.path.join(self.get_template_dir(template_name), "filestore")

    def get_template_metadata_path(self, template_name: str) -> str:
        return os.path.join(self.get_template_dir(template_name), "metadata.json")

    def list_templates(self) -> list[str]:
        templates_dir = os.path.join(self.home, "templates")
        if not os.path.isdir(templates_dir):
            return []
        return sorted(
            entry for entry in os.listdir(templates_dir)
            if os.path.isdir(os.path.join(templates_dir, entry))
        )

    @property
    def shared_repos_dir(self) -> str:
        return os.path.join(self.home, "shared_repos")

    @staticmethod
    def from_env() -> "Settings":
        instance_id = os.getenv("ODUFLOW_INSTANCE_ID", "1").strip()
        flow_home = os.getenv("ODUFLOW_HOME", f"/srv/oduflow_data_{instance_id}")
        return Settings(
            instance_id=instance_id,
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
            home=flow_home,
            db_user=os.getenv("ODOO_DB_USER", "odoo"),
            db_password=os.getenv("ODOO_DB_PASSWORD", "odoo"),
            flow_server_port=int(os.getenv("ODUFLOW_PORT", "8000")),
            default_branch=os.getenv("ODUFLOW_DEFAULT_BRANCH", "prod"),
            overlay_threshold_mb=int(os.getenv("ODUFLOW_OVERLAY_THRESHOLD_MB", "50")),
            stateless_http=os.getenv("ODUFLOW_STATELESS_HTTP", "true").strip().lower() in ("1", "true", "yes"),
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
