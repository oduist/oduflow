import os
import re
from urllib.parse import urlparse, urlunparse


def slugify_branch(branch_name: str) -> str:
    slug = branch_name.replace("/", "-")
    slug = re.sub(r"[^a-zA-Z0-9_-]", "", slug)
    slug = slug.lower()
    return slug[:63]


def get_db_name(branch_name: str, team_id: str = "1") -> str:
    return f"oduflow_{team_id}_{slugify_branch(branch_name)}"


def get_resource_name(
    branch_name: str, resource_type: str, prefix: str = "oduflow-"
) -> str:
    return f"{prefix}{branch_name.replace('/', '-')}-{resource_type}"


def get_workspace_path(branch_name: str, workspaces_dir: str) -> str:
    return os.path.join(workspaces_dir, branch_name.replace("/", "-"))


def get_repo_path(branch_name: str, workspaces_dir: str) -> str:
    return os.path.join(get_workspace_path(branch_name, workspaces_dir), "repo")


def get_env_hostname(branch_name: str, base_domain: str) -> str:
    slug = slugify_branch(branch_name).replace("_", "-")
    slug = re.sub(r"-+", "-", slug).strip("-")
    return f"{slug}.{base_domain}"


def get_template_db_name(template_name: str, team_id: str = "1") -> str:
    slug = template_name.replace("/", "-")
    return f"oduflow_template_{team_id}_{slug}"


def get_filestore_paths(branch_name: str, workspaces_dir: str) -> dict[str, str]:
    base = get_workspace_path(branch_name, workspaces_dir)
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
