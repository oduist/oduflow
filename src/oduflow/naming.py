import os
import re
from urllib.parse import urlparse, urlunparse

# A template name becomes both a filesystem path (templates/<name>, where "/"
# denotes a sub-directory) and, with "/" replaced by "-", part of a PostgreSQL
# database identifier. "/" is therefore allowed as a segment separator, but each
# segment must start with an alphanumeric (which rules out "." and ".." — i.e.
# path traversal) and contain only [a-zA-Z0-9_.-] (which rules out quotes,
# semicolons and whitespace — i.e. SQL-identifier break-out).
_TEMPLATE_SEGMENT_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


def validate_template_name(template_name: str) -> str:
    """Validate a template name and return it unchanged.

    Raises ValueError if the name could escape the templates directory or break
    out of a SQL identifier. Enforced at the two derivation chokepoints
    (get_template_db_name and TeamSettings.get_template_dir), so every entry
    point — MCP tools, web UI, CLI — is covered.
    """
    name = template_name or ""
    segments = name.split("/")
    if (
        not name
        or len(name) > 63
        or not all(_TEMPLATE_SEGMENT_RE.match(seg) for seg in segments)
    ):
        raise ValueError(
            f"Invalid template name '{template_name}': each '/'-separated "
            "segment must start with a letter or digit and contain only "
            "[a-zA-Z0-9_.-] (max 63 chars total)."
        )
    return name


def validate_env_name(env_name: str) -> str:
    """Validate an environment/branch name and return it unchanged.

    ``env_name`` becomes a filesystem path segment via
    ``get_workspace_path`` (``os.path.join(dir, env_name.replace('/', '-'))``).
    Slashes are folded to ``-`` there, so the only way the result can escape the
    workspaces dir is a name that IS a traversal component (``.``/``..``) or that
    smuggles a native path separator/NUL. Reject those (and empty/oversized
    names) at the creation chokepoint so every derived path stays contained.
    Legitimate git branch names (``19.0``, ``feature/x``, ``release/v1.2``) pass.
    Returns the name **unchanged** (it is not canonicalized): a padded name like
    ``"foo "`` is rejected rather than silently trimmed, because slugified
    container/DB names strip the space while the workspace path keeps it — so the
    trimmed and raw forms would collide on containers yet diverge on disk.
    """
    name = env_name or ""
    if name != name.strip():
        raise ValueError(
            f"Invalid environment name '{env_name}': leading or trailing "
            "whitespace is not allowed."
        )
    if not name or len(name) > 100:
        raise ValueError(
            f"Invalid environment name '{env_name}': must be 1-100 characters."
        )
    if "\\" in name or "\x00" in name:
        raise ValueError(
            f"Invalid environment name '{env_name}': backslashes and NUL bytes "
            "are not allowed."
        )
    if any(seg in ("", ".", "..") for seg in name.split("/")):
        raise ValueError(
            f"Invalid environment name '{env_name}': '/'-separated segments must "
            "be non-empty and not '.' or '..'."
        )
    return name


def slugify_branch(env_name: str) -> str:
    slug = env_name.replace("/", "-")
    slug = re.sub(r"[^a-zA-Z0-9_-]", "", slug)
    slug = slug.lower()
    return slug[:63]


def get_db_name(env_name: str, team_id: str = "1") -> str:
    return f"oduflow_{team_id}_{slugify_branch(env_name)}"


def get_resource_name(
    env_name: str, resource_type: str, prefix: str, team_id: str
) -> str:
    """Docker container name for an environment resource.

    Team-scoped (naming v2): two teams using the same branch name must never
    collide on — or worse, operate on — each other's containers. Existing
    containers are renamed to this scheme by startup migration
    0001-team-scoped-container-names.
    """
    return f"{prefix}{team_id}-{env_name.replace('/', '-')}-{resource_type}"


def get_service_container_name(name: str, prefix: str, team_id: str) -> str:
    """Docker container name for an auxiliary service (team-scoped, see
    get_resource_name)."""
    return f"{prefix}{team_id}-svc-{name}"


def get_agent_container_name(team_id: str, prefix: str) -> str:
    """The team's coding-agent container (one container serves every
    environment of the team). Cannot collide with env resources
    (``{prefix}{team}-{env}-{type}``, always type-suffixed) or services
    (``{prefix}{team}-svc-{name}``)."""
    return f"{prefix}{team_id}-agent"


def get_agent_home_volume_name(team_id: str, prefix: str) -> str:
    """Named volume mounted as the agent container's HOME (auth + sessions)."""
    return f"{prefix}{team_id}-agent-home"


def get_agent_workspace_volume_name(team_id: str, prefix: str) -> str:
    """Named volume mounted at /workspace (one checkout per environment)."""
    return f"{prefix}{team_id}-agent-workspace"


def get_agent_checkout_dir(env_name: str) -> str:
    """Path of an environment's checkout inside the team's agent container.

    Used both by the create hook (clone-env.sh target) and the agent console /
    ACP chat (exec workdir), so they must agree on the same slug.
    See specs/0029-agent-console-and-chat.md.
    """
    return f"/workspace/{slugify_branch(env_name)}"


def get_workspace_path(env_name: str, workspaces_dir: str) -> str:
    return os.path.join(workspaces_dir, env_name.replace("/", "-"))


def get_repo_path(env_name: str, workspaces_dir: str) -> str:
    return os.path.join(get_workspace_path(env_name, workspaces_dir), "repo")


def get_env_hostname(env_name: str, hostname: str) -> str:
    slug = slugify_branch(env_name).replace("_", "-")
    slug = re.sub(r"-+", "-", slug).strip("-")
    return f"{slug}.{hostname}"


def get_team_network_name(team_id: str, prefix: str = "oduflow-") -> str:
    """The team's isolated Docker network. Environment and service containers
    join only this network; shared infrastructure (PostgreSQL, Traefik) is
    additionally attached to every team network, so tenants can reach the
    infra but never each other."""
    return f"{prefix}{team_id}-net"


def get_tablespace_name(team_id: str) -> str:
    """PostgreSQL tablespace holding all of the team's databases (its files
    live under base_data_dir/pg_tablespaces/team_{id} on the host)."""
    return f"oduflow_team_{team_id}"


def get_template_db_name(template_name: str, team_id: str = "1") -> str:
    # validate_template_name guarantees the (slugified) name cannot break out of
    # the double-quoted SQL identifier this is interpolated into.
    validate_template_name(template_name)
    slug = template_name.replace("/", "-")
    return f"oduflow_template_{team_id}_{slug}"


def get_filestore_paths(env_name: str, workspaces_dir: str) -> dict[str, str]:
    base = get_workspace_path(env_name, workspaces_dir)
    return {
        "upper": os.path.join(base, "filestore_upper"),
        "work": os.path.join(base, "filestore_work"),
        "merged": os.path.join(base, "filestore"),
    }


def sanitize_repo_url(url: str) -> str:
    if not url:
        return url
    try:
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            clean = parsed._replace(netloc=parsed.hostname or "")
            return urlunparse(clean)
    except Exception:
        pass
    return url
