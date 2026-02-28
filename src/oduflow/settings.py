"""Configuration loading from TOML with per-team isolation."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

logger = logging.getLogger("oduflow")


@dataclass(frozen=True)
class TeamSettings:
    """Per-team settings (isolated workspaces, templates, credentials, ports)."""

    team_id: str
    auth_token: str = ""
    ui_password: str = ""
    port_range_start: int = 50000
    port_range_end: int = 50100
    data_dir: str = ""
    port_registry_path: str = ""

    @property
    def workspaces_dir(self) -> str:
        return os.path.join(self.data_dir, "workspaces")

    @property
    def shared_repos_dir(self) -> str:
        return os.path.join(self.data_dir, "shared_repos")

    def get_template_dir(self, template_name: str) -> str:
        return os.path.join(self.data_dir, "templates", template_name)

    def get_template_sql_path(self, template_name: str) -> str:
        tpl_dir = self.get_template_dir(template_name)
        pgdump = os.path.join(tpl_dir, "dump.pgdump")
        if os.path.isfile(pgdump):
            return pgdump
        sql = os.path.join(tpl_dir, "dump.sql")
        if os.path.isfile(sql):
            return sql
        return pgdump

    def get_template_filestore_path(self, template_name: str) -> str:
        return os.path.join(self.get_template_dir(template_name), "filestore")

    def get_template_metadata_path(self, template_name: str) -> str:
        return os.path.join(self.get_template_dir(template_name), "metadata.json")

    def list_templates(self) -> list[str]:
        templates_dir = os.path.join(self.data_dir, "templates")
        if not os.path.isdir(templates_dir):
            return []
        return sorted(
            entry
            for entry in os.listdir(templates_dir)
            if os.path.isdir(os.path.join(templates_dir, entry))
        )

    def git_credentials_file(self) -> str:
        return os.path.join(self.data_dir, ".git-credentials")


@dataclass(frozen=True)
class Settings:
    """Global settings + per-team isolation."""

    # Server
    host: str = "0.0.0.0"
    port: int = 8000

    # Routing
    routing_mode: str = "port"
    base_domain: str = ""
    acme_email: str = ""
    external_host: str = "localhost"

    # Database
    db_user: str = "odoo"
    db_password: str = "odoo"
    postgres_image: str = "postgres:15"

    # Storage
    base_data_dir: str = ""
    overlay_threshold_mb: int = 50

    # Shared Docker resource names
    shared_network: str = "oduflow-net"
    shared_db_container: str = "oduflow-db"
    shared_db_volume: str = "oduflow-db-data"
    traefik_container: str = "oduflow-traefik"
    traefik_acme_volume: str = "oduflow-traefik-acme"

    # Docker labels
    prefix: str = "oduflow-"
    branch_label: str = "oduflow.branch"
    team_label: str = "oduflow.team"
    managed_label: str = "oduflow.managed"
    system_label: str = "oduflow.system"
    repo_label: str = "oduflow.repo"
    image_label: str = "oduflow.image"

    # Config location
    etc_dir: str = ""
    toml_path: str = ""

    # Teams
    teams: dict[str, TeamSettings] = field(default_factory=dict)

    def get_team(self, team_id: str) -> TeamSettings:
        if team_id not in self.teams:
            raise ValueError(f"Team '{team_id}' not found in configuration.")
        return self.teams[team_id]

    def get_team_by_token(self, token: str) -> TeamSettings | None:
        if not token:
            return None
        for team in self.teams.values():
            if team.auth_token and team.auth_token == token:
                return team
        return None

    def get_team_by_ui_password(self, password: str) -> TeamSettings | None:
        if not password:
            return None
        for team in self.teams.values():
            if team.ui_password and team.ui_password == password:
                return team
        return None

    def validate(self) -> None:
        if not self.teams:
            raise ValueError(
                "No [team.*] sections found in configuration. "
                "At least one team is required."
            )

        if self.routing_mode not in ("port", "traefik"):
            raise ValueError("routing_mode must be 'port' or 'traefik'")

        if self.routing_mode == "traefik":
            if not self.base_domain:
                raise ValueError("base_domain must be set when routing_mode=traefik")
            if not self.acme_email:
                raise ValueError("acme_email must be set when routing_mode=traefik")

        # Validate per-team settings
        for team in self.teams.values():
            if team.port_range_start >= team.port_range_end:
                raise ValueError(
                    f"Team '{team.team_id}': invalid port range "
                    f"{team.port_range_start}-{team.port_range_end}"
                )

        # Validate uniqueness of auth tokens
        tokens = [t.auth_token for t in self.teams.values() if t.auth_token]
        if len(tokens) != len(set(tokens)):
            raise ValueError("Duplicate auth_token values across teams.")

        # Validate uniqueness of UI passwords
        passwords = [t.ui_password for t in self.teams.values() if t.ui_password]
        if len(passwords) != len(set(passwords)):
            raise ValueError("Duplicate ui_password values across teams.")

    @staticmethod
    def from_toml(path: str) -> Settings:
        with open(path, "rb") as f:
            raw = tomllib.load(f)

        server = raw.get("server", {})
        routing = raw.get("routing", {})
        database = raw.get("database", {})
        storage = raw.get("storage", {})

        etc_dir = _resolve_etc_dir()
        base_data_dir = _resolve_data_dir(storage.get("data_dir", ""))

        # Parse teams
        teams_raw = raw.get("team", {})
        if not teams_raw:
            raise ValueError(
                "No [team.*] sections found in TOML config. "
                "At least one team is required (e.g. [team.1])."
            )

        teams: dict[str, TeamSettings] = {}
        for team_id_raw, team_cfg in teams_raw.items():
            team_id = str(team_id_raw)
            team_data_dir = os.path.join(base_data_dir, f"team_{team_id}")

            port_range = team_cfg.get("port_range", [50000, 50100])
            if isinstance(port_range, list) and len(port_range) == 2:
                port_start, port_end = int(port_range[0]), int(port_range[1])
            else:
                port_start, port_end = 50000, 50100

            teams[team_id] = TeamSettings(
                team_id=team_id,
                auth_token=str(team_cfg.get("auth_token", "")),
                ui_password=str(team_cfg.get("ui_password", "")),
                port_range_start=port_start,
                port_range_end=port_end,
                data_dir=team_data_dir,
                port_registry_path=os.path.join(team_data_dir, "ports.json"),
            )

        base_domain = routing.get("base_domain", "")
        if isinstance(base_domain, str):
            base_domain = re.sub(r"^https?://", "", base_domain).strip()
        external_host = routing.get("external_host", "localhost")
        if isinstance(external_host, str):
            external_host = re.sub(r"^https?://", "", external_host)

        return Settings(
            host=str(server.get("host", "0.0.0.0")),
            port=int(server.get("port", 8000)),
            routing_mode=str(routing.get("mode", "port")).strip().lower(),
            base_domain=base_domain,
            acme_email=str(routing.get("acme_email", "")).strip(),
            external_host=external_host,
            db_user=str(database.get("user", "odoo")),
            db_password=str(database.get("password", "odoo")),
            postgres_image=str(database.get("image", "postgres:15")),
            base_data_dir=base_data_dir,
            overlay_threshold_mb=int(storage.get("overlay_threshold_mb", 50)),
            etc_dir=etc_dir,
            toml_path=path,
            teams=teams,
        )


def find_toml() -> str:
    """Locate oduflow.toml: ODUFLOW_TOML env > /etc/oduflow > ~/.oduflow."""
    explicit = os.getenv("ODUFLOW_TOML", "").strip()
    if explicit:
        if os.path.isfile(explicit):
            return explicit
        raise FileNotFoundError(f"ODUFLOW_TOML={explicit} does not exist.")

    candidates = [
        "/etc/oduflow/oduflow.toml",
        os.path.join(os.path.expanduser("~"), ".oduflow", "oduflow.toml"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path

    raise FileNotFoundError(
        "oduflow.toml not found. Searched:\n"
        + "\n".join(f"  - {c}" for c in candidates)
        + "\nCreate one or set ODUFLOW_TOML environment variable."
    )


def _resolve_etc_dir() -> str:
    """Resolve etc directory: /etc/oduflow or ~/.oduflow/conf."""
    default = "/etc/oduflow"
    if os.access(default, os.W_OK) or os.access(os.path.dirname(default), os.W_OK):
        return default
    fallback = os.path.join(os.path.expanduser("~"), ".oduflow", "conf")
    logger.info("Default %s is not writable, falling back to %s", default, fallback)
    return fallback


def _resolve_data_dir(explicit: str) -> str:
    """Resolve base data directory: explicit > /srv/oduflow > ~/.oduflow/data."""
    if explicit:
        return explicit
    default = "/srv/oduflow"
    parent = os.path.dirname(default)
    if os.access(parent, os.W_OK) or (
        os.path.isdir(default) and os.access(default, os.W_OK)
    ):
        return default
    fallback = os.path.join(os.path.expanduser("~"), ".oduflow", "data")
    logger.info("Default %s is not writable, falling back to %s", default, fallback)
    return fallback
