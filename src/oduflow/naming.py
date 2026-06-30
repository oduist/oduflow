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


def slugify_branch(env_name: str) -> str:
    slug = env_name.replace("/", "-")
    slug = re.sub(r"[^a-zA-Z0-9_-]", "", slug)
    slug = slug.lower()
    return slug[:63]


def get_db_name(env_name: str, team_id: str = "1") -> str:
    return f"oduflow_{team_id}_{slugify_branch(env_name)}"


def get_resource_name(
    env_name: str, resource_type: str, prefix: str = "oduflow-"
) -> str:
    return f"{prefix}{env_name.replace('/', '-')}-{resource_type}"


def get_workspace_path(env_name: str, workspaces_dir: str) -> str:
    return os.path.join(workspaces_dir, env_name.replace("/", "-"))


def get_repo_path(env_name: str, workspaces_dir: str) -> str:
    return os.path.join(get_workspace_path(env_name, workspaces_dir), "repo")


def get_env_hostname(env_name: str, hostname: str) -> str:
    slug = slugify_branch(env_name).replace("_", "-")
    slug = re.sub(r"-+", "-", slug).strip("-")
    return f"{slug}.{hostname}"


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
