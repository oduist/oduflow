import os
import re


def slugify_branch(branch_name: str) -> str:
    slug = branch_name.replace("/", "-")
    slug = re.sub(r"[^a-zA-Z0-9_-]", "", slug)
    slug = slug.lower()
    return slug[:63]


def get_db_name(branch_name: str) -> str:
    return f"flow_{slugify_branch(branch_name)}"


def get_resource_name(branch_name: str, resource_type: str, prefix: str = "flow-") -> str:
    return f"{prefix}{branch_name.replace('/', '-')}-{resource_type}"


def get_workspace_path(branch_name: str, workspaces_dir: str) -> str:
    return os.path.join(workspaces_dir, branch_name.replace("/", "-"))


def get_repo_path(branch_name: str, workspaces_dir: str) -> str:
    return os.path.join(get_workspace_path(branch_name, workspaces_dir), "repo")


def get_filestore_paths(branch_name: str, workspaces_dir: str) -> dict[str, str]:
    base = get_workspace_path(branch_name, workspaces_dir)
    return {
        "upper": os.path.join(base, "filestore_upper"),
        "work": os.path.join(base, "filestore_work"),
        "merged": os.path.join(base, "filestore"),
    }
