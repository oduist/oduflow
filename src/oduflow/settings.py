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

TRACE: bool = False

# Active MCP transport for the running server ("stdio" | "http").
# Set in server.main() before the server starts. Mostly informational;
# local_path is gated by the allow_local_path setting.
TRANSPORT: str = "stdio"


@dataclass(frozen=True)
class TeamSettings:
    """Per-team settings (isolated workspaces, templates, credentials, ports)."""

    team_id: str
    hostname: str = "localhost"
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
        for name in ("dump.pgdump", "dump.sql", "dump.pgdump.gz", "dump.sql.gz"):
            path = os.path.join(tpl_dir, name)
            if os.path.isfile(path):
                return path
        return os.path.join(tpl_dir, "dump.pgdump")

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
    trace: bool = False
    disable_telemetry: bool = False
    allow_local_path: bool = True

    # Routing
    routing_mode: str = "port"
    acme_email: str = ""

    # Database
    db_user: str = "odoo"
    db_password: str = "odoo"
    postgres_image: str = "postgres:15"

    # Storage
    base_data_dir: str = ""
    overlay_threshold_mb: int = 50

    # Lifecycle: automatic stop of idle environments and cleanup of stopped
    # ones (see oduflow.reaper). 0 disables either behavior. Protected
    # environments are always exempt.
    auto_stop_hours: int = 48
    auto_delete_hours: int = 72

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

    # OAuth (self-hosted Authorization Server). Public base URL of this server,
    # used as the OAuth issuer and to advertise authorize/token endpoints.
    # When set, Oduflow exposes /.well-known/oauth-authorization-server,
    # /authorize, and /token, and each team's auth_token doubles as
    # client_id, client_secret, and the issued access token.
    oauth_base_url: str = ""

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

    def get_team_by_hostname(self, hostname: str) -> TeamSettings | None:
        if not hostname:
            return None
        hostname = hostname.split(":")[0]  # Strip port
        for team in self.teams.values():
            if team.hostname == hostname:
                return team
        return None

    @property
    def oauth_enabled(self) -> bool:
        return bool(self.oauth_base_url)

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
            if not self.acme_email:
                raise ValueError("acme_email must be set when routing_mode=traefik")

        # Validate per-team settings
        for team in self.teams.values():
            if self.routing_mode == "traefik" and not team.hostname:
                raise ValueError(
                    f"Team '{team.team_id}': hostname must be set "
                    "when routing_mode=traefik"
                )
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

        # Validate OAuth: when oauth_base_url is set, at least one team must
        # have a non-empty auth_token (it doubles as OAuth client credentials).
        if self.oauth_base_url and not any(t.auth_token for t in self.teams.values()):
            raise ValueError(
                "oauth_base_url is set but no team has an auth_token. "
                "Self-hosted OAuth requires at least one [team.*] section "
                "with a non-empty auth_token."
            )

    @staticmethod
    def from_toml(path: str) -> Settings:
        with open(path, "rb") as f:
            raw = tomllib.load(f)

        server = raw.get("server", {})
        routing = raw.get("routing", {})
        database = raw.get("database", {})
        storage = raw.get("storage", {})
        lifecycle = raw.get("lifecycle", {})
        oauth = raw.get("oauth", server)  # [oauth] section or fall back to [server]

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

            raw_hostname = str(
                team_cfg.get("hostname", routing.get("hostname", "localhost"))
            )
            hostname = re.sub(r"^https?://", "", raw_hostname).strip()

            teams[team_id] = TeamSettings(
                team_id=team_id,
                hostname=hostname,
                auth_token=str(team_cfg.get("auth_token", "")),
                ui_password=str(team_cfg.get("ui_password", "")),
                port_range_start=port_start,
                port_range_end=port_end,
                data_dir=team_data_dir,
                port_registry_path=os.path.join(team_data_dir, "ports.json"),
            )

        trace = bool(server.get("trace", False))

        global TRACE  # noqa: PLW0603
        TRACE = trace

        return Settings(
            host=str(server.get("host", "0.0.0.0")),
            port=int(server.get("port", 8000)),
            trace=trace,
            disable_telemetry=bool(server.get("disable_telemetry", False)),
            allow_local_path=bool(server.get("allow_local_path", True)),
            routing_mode=str(routing.get("mode", "port")).strip().lower(),
            acme_email=str(routing.get("acme_email", "")).strip(),
            db_user=str(database.get("user", "odoo")),
            db_password=str(database.get("password", "odoo")),
            postgres_image=str(database.get("image", "postgres:15")),
            base_data_dir=base_data_dir,
            overlay_threshold_mb=int(storage.get("overlay_threshold_mb", 50)),
            auto_stop_hours=int(lifecycle.get("auto_stop_hours", 48)),
            auto_delete_hours=int(lifecycle.get("auto_delete_hours", 72)),
            oauth_base_url=str(oauth.get("oauth_base_url", "")).strip(),
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
        os.path.join(os.path.expanduser("~"), ".oduflow", "conf", "oduflow.toml"),
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


_cached_etc_dir: str | None = None


def _resolve_etc_dir() -> str:
    """Resolve etc directory: /etc/oduflow or ~/.oduflow/conf (cached)."""
    global _cached_etc_dir
    if _cached_etc_dir is not None:
        return _cached_etc_dir
    default = "/etc/oduflow"
    if os.access(default, os.W_OK) or os.access(os.path.dirname(default), os.W_OK):
        _cached_etc_dir = default
    else:
        _cached_etc_dir = os.path.join(os.path.expanduser("~"), ".oduflow", "conf")
        logger.debug(
            "Default %s is not writable, falling back to %s", default, _cached_etc_dir
        )
    return _cached_etc_dir


_cached_data_dir: str | None = None


def _resolve_data_dir(explicit: str) -> str:
    """Resolve base data directory: explicit > /srv/oduflow > ~/.oduflow/data (cached)."""
    global _cached_data_dir
    if explicit:
        return explicit
    if _cached_data_dir is not None:
        return _cached_data_dir
    default = "/srv/oduflow"
    parent = os.path.dirname(default)
    if os.access(parent, os.W_OK) or (
        os.path.isdir(default) and os.access(default, os.W_OK)
    ):
        _cached_data_dir = default
    else:
        _cached_data_dir = os.path.join(os.path.expanduser("~"), ".oduflow", "data")
        logger.debug(
            "Default %s is not writable, falling back to %s", default, _cached_data_dir
        )
    return _cached_data_dir
