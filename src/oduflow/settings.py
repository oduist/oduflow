"""Configuration loading from TOML with per-team isolation."""

from __future__ import annotations

import hmac
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
    # Quotas; 0 disables. db_quota_gb caps the combined size of the team's
    # PostgreSQL databases (environments + templates) and is checked before
    # operations that create a new database. disk_quota_gb caps the team's
    # data dir plus its PG tablespace via XFS project quotas (see quotas.py);
    # on filesystems without project-quota support it stays informational.
    db_quota_gb: int = 50
    disk_quota_gb: int = 0
    # Coding agent (dashboard "Agent Chat" / "Agent CLI"). Opt-in per team:
    # it is a hosting feature (clients driving their environments from the
    # browser); local developers use their own agents, so it defaults to off.
    # agent_env holds the variables injected into the team's agent container
    # (provider credentials such as CLAUDE_CODE_OAUTH_TOKEN / OPENAI_API_KEY),
    # from the [team.X.agent_env] TOML table. Config is the source of truth:
    # the container is recreated automatically when these change (see
    # env_ops._ensure_agent_container). See specs/0029-agent-console-and-chat.md.
    agent_enabled: bool = False
    agent_default: str = "claude"  # which agent consoles/chats open by default
    agent_env: dict[str, str] = field(default_factory=dict)

    @property
    def workspaces_dir(self) -> str:
        return os.path.join(self.data_dir, "workspaces")

    @property
    def shared_repos_dir(self) -> str:
        return os.path.join(self.data_dir, "shared_repos")

    def get_template_dir(self, template_name: str) -> str:
        # Reject names that could escape the templates directory (path
        # traversal) before they reach rmtree / file writes.
        from oduflow.naming import validate_template_name

        validate_template_name(template_name)
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

    def get_import_staging_dir(self, template_name: str) -> str:
        """Where a push-based Odoo.sh import stages its upload before finalize.

        Kept outside ``templates/`` so a partial upload never masquerades as (or
        clobbers) a live template; finalize swaps it into place atomically.
        """
        from oduflow.naming import validate_template_name

        validate_template_name(template_name)
        return os.path.join(self.data_dir, "import_staging", template_name)

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
    # Allow starting the HTTP transport with no MCP authentication. Off by
    # default so /mcp is never served unauthenticated by accident (#37); set
    # true only when fronting Oduflow with your own auth proxy.
    allow_insecure_http: bool = False

    # Routing
    routing_mode: str = "port"
    acme_email: str = ""
    # Whether Traefik terminates TLS itself. True (default): Traefik listens on
    # :443, redirects HTTP->HTTPS and obtains Let's Encrypt certificates. False:
    # Traefik listens on plain HTTP :80 only, no redirect and no ACME — for
    # running behind a TLS-terminating upstream (e.g. a Cloudflare tunnel) that
    # already serves HTTPS. Public URLs stay https:// (the upstream provides the
    # certificate). Ignored in port mode.
    routing_tls: bool = True

    # Database
    db_user: str = "odoo"
    db_password: str = "odoo"
    postgres_image: str = "postgres:15"

    # Storage
    base_data_dir: str = ""
    overlay_threshold_mb: int = 50

    # Coding agent — deployment-wide bits only; enabling the agent and its
    # credentials are per team (TeamSettings.agent_*). When enabled for a team,
    # a single agent container (Claude Code + OpenAI Codex) serves all of its
    # environments: one git checkout per environment on a persistent volume,
    # driving environments only through the Oduflow MCP server. Container and
    # volume names are derived per team in naming.py.
    # See specs/0029-agent-console-and-chat.md.
    agent_image: str = "oduist/oduflow-coder:latest"
    agent_claude_model: str = ""  # optional; empty = CLI default
    agent_codex_model: str = ""  # optional; empty = CLI default

    # Lifecycle: automatic stop of idle environments and cleanup of stopped
    # ones (see oduflow.reaper). 0 disables either behavior. Protected
    # environments are always exempt. auto-delete is DESTRUCTIVE (drops the
    # database and workspace), so it is opt-in (defaults to 0); auto-stop is
    # non-destructive and enabled by default.
    auto_stop_hours: int = 48
    auto_delete_hours: int = 0

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
    # In traefik mode this is optional: the issuer is derived per-request from
    # the team's own hostname (already TLS-terminated), so OAuth works without a
    # central host. Set it only to pin a fixed issuer or in port mode.
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
            if team.auth_token and hmac.compare_digest(team.auth_token, token):
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
        # Self-hosted OAuth is served whenever an explicit issuer is configured
        # (oauth_base_url) or we run behind Traefik, where each team's own
        # TLS-terminated hostname is used as a per-request issuer — no central
        # oauth_base_url needed. Port mode still requires an explicit issuer.
        # It also requires a team auth_token (which doubles as the OAuth client
        # credential): without one nothing is actually served, so the flag stays
        # False rather than reporting "OAuth ON" for a tokenless deployment.
        has_token = any(t.auth_token for t in self.teams.values())
        return has_token and (
            bool(self.oauth_base_url) or self.routing_mode == "traefik"
        )

    def get_team_by_ui_password(self, password: str) -> TeamSettings | None:
        if not password:
            return None
        for team in self.teams.values():
            if team.ui_password and hmac.compare_digest(team.ui_password, password):
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

        if self.routing_mode == "traefik" and self.routing_tls:
            if not self.acme_email:
                raise ValueError(
                    "acme_email must be set when routing_mode=traefik and "
                    "routing tls is enabled"
                )

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
            if team.db_quota_gb < 0 or team.disk_quota_gb < 0:
                raise ValueError(
                    f"Team '{team.team_id}': quotas must be >= 0 (0 disables)"
                )

        # Validate that team port ranges do not overlap. The default range is
        # identical for every team, so two teams that never set an explicit
        # port_range would draw host ports from the same pool and collide.
        ranges = sorted(
            (
                (t.port_range_start, t.port_range_end, t.team_id)
                for t in self.teams.values()
            ),
            key=lambda r: r[0],
        )
        for (a_start, a_end, a_id), (b_start, b_end, b_id) in zip(ranges, ranges[1:]):
            # Ranges are half-open [start, end); they overlap when the next
            # range starts before the previous one ends.
            if b_start < a_end:
                raise ValueError(
                    f"Teams '{a_id}' and '{b_id}' have overlapping port ranges "
                    f"({a_start}-{a_end} and {b_start}-{b_end}). "
                    "Set a distinct [team.*] port_range for each team."
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
        agent = raw.get("agent", {})
        oauth = raw.get("oauth", server)  # [oauth] section or fall back to [server]
        routing_mode = str(routing.get("mode", "port")).strip().lower()

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

            port_range = team_cfg.get("port_range")
            if port_range is None:
                port_start, port_end = 50000, 50100
            elif isinstance(port_range, list) and len(port_range) == 2:
                try:
                    port_start, port_end = int(port_range[0]), int(port_range[1])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"Team '{team_id}': port_range must be two integers, "
                        f"got {port_range!r}"
                    ) from exc
            else:
                raise ValueError(
                    f"Team '{team_id}': port_range must be [start, end], "
                    f"got {port_range!r}"
                )

            # [routing].hostname is a fallback only in port mode. In traefik
            # mode the validator requires every team to set its own hostname;
            # silently inheriting one shared default would make two such teams
            # collide in get_team_by_hostname, so leave it empty and let
            # validate() report the misconfiguration.
            default_hostname = (
                routing.get("hostname", "localhost") if routing_mode == "port" else ""
            )
            raw_hostname = str(team_cfg.get("hostname", default_hostname))
            hostname = re.sub(r"^https?://", "", raw_hostname).strip()

            agent_env_raw = team_cfg.get("agent_env", {})
            if not isinstance(agent_env_raw, dict):
                raise ValueError(
                    f"Team '{team_id}': agent_env must be a table "
                    f"([team.{team_id}.agent_env]), got {agent_env_raw!r}"
                )

            teams[team_id] = TeamSettings(
                team_id=team_id,
                hostname=hostname,
                auth_token=str(team_cfg.get("auth_token", "")),
                ui_password=str(team_cfg.get("ui_password", "")),
                port_range_start=port_start,
                port_range_end=port_end,
                data_dir=team_data_dir,
                port_registry_path=os.path.join(team_data_dir, "ports.json"),
                db_quota_gb=int(team_cfg.get("db_quota_gb", 50)),
                disk_quota_gb=int(team_cfg.get("disk_quota_gb", 0)),
                agent_enabled=bool(team_cfg.get("agent_enabled", False)),
                agent_default=str(team_cfg.get("agent_default", "claude"))
                .strip()
                .lower()
                or "claude",
                agent_env={str(k): str(v) for k, v in agent_env_raw.items()},
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
            allow_insecure_http=bool(server.get("allow_insecure_http", False)),
            routing_mode=routing_mode,
            acme_email=str(routing.get("acme_email", "")).strip(),
            routing_tls=bool(routing.get("tls", True)),
            db_user=str(database.get("user", "odoo")),
            db_password=str(database.get("password", "odoo")),
            postgres_image=str(database.get("image", "postgres:15")),
            base_data_dir=base_data_dir,
            overlay_threshold_mb=int(storage.get("overlay_threshold_mb", 50)),
            agent_image=str(agent.get("image", "oduist/oduflow-coder:latest")).strip()
            or "oduist/oduflow-coder:latest",
            agent_claude_model=str(agent.get("claude_model", "")).strip(),
            agent_codex_model=str(agent.get("codex_model", "")).strip(),
            auto_stop_hours=int(lifecycle.get("auto_stop_hours", 48)),
            auto_delete_hours=int(lifecycle.get("auto_delete_hours", 0)),
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
