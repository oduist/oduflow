import configparser
import json
import logging
import os
import re
import shutil
import subprocess

from oduflow.docker_ops.client import get_client
from oduflow.errors import ConflictError, ExternalCommandError, NotFoundError
from oduflow.git_ops import RepoAuthError
from oduflow.naming import sanitize_repo_url
from oduflow.settings import Settings

logger = logging.getLogger("oduflow")

GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,63}$")

_AUTH_ERROR_KEYWORDS = (
    "Authentication failed",
    "could not read Username",
    "Permission denied",
    "Repository not found",
    "terminal prompts disabled",
    "Invalid username or password",
)


def clone_extra_repo(settings: Settings, name: str, repo_url: str) -> dict:
    if not _NAME_RE.match(name):
        raise ValueError(
            f"Invalid repo name '{name}': only [a-zA-Z0-9_-] allowed, "
            "no dots or slashes, max 63 chars."
        )

    target = os.path.join(settings.shared_repos_dir, name)
    if os.path.exists(target):
        raise ConflictError(f"Extra repo '{name}' already exists at {target}")

    os.makedirs(settings.shared_repos_dir, exist_ok=True)

    try:
        subprocess.run(
            ["git", "clone", "--bare", repo_url, target],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
            env=GIT_ENV,
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        if any(kw in stderr for kw in _AUTH_ERROR_KEYWORDS):
            raise RepoAuthError(
                f"Authentication failed for '{repo_url}'. "
                "Use setup_repo_auth to configure credentials first."
            )
        raise ExternalCommandError("git clone --bare", e.returncode, stderr)
    except subprocess.TimeoutExpired:
        raise ExternalCommandError(
            "git clone --bare", -1, "Clone timed out (120s)."
        )

    logger.info("Cloned extra repo '%s' from %s", name, repo_url)
    return {"name": name, "repo_url": repo_url, "path": target}


def list_extra_repos(settings: Settings) -> list[dict]:
    repos_dir = settings.shared_repos_dir
    if not os.path.isdir(repos_dir):
        return []

    result = []
    for entry in sorted(os.listdir(repos_dir)):
        path = os.path.join(repos_dir, entry)
        if not os.path.isdir(path):
            continue

        try:
            url = subprocess.run(
                ["git", "-C", path, "config", "--get", "remote.origin.url"],
                check=True,
                capture_output=True,
                text=True,
                env=GIT_ENV,
            ).stdout.strip()
        except subprocess.CalledProcessError:
            url = ""

        try:
            branches_raw = subprocess.run(
                ["git", "-C", path, "branch", "-a", "--format=%(refname:short)"],
                check=True,
                capture_output=True,
                text=True,
                env=GIT_ENV,
            ).stdout.strip()
            branches = [b for b in branches_raw.splitlines() if b]
        except subprocess.CalledProcessError:
            branches = []

        result.append({"name": entry, "repo_url": sanitize_repo_url(url), "branches": branches})

    return result


def delete_extra_repo(settings: Settings, name: str) -> dict:
    path = os.path.join(settings.shared_repos_dir, name)
    if not os.path.exists(path):
        raise NotFoundError(f"Extra repo '{name}' not found.")

    client = get_client()
    filters = {
        "label": [
            f"{settings.managed_label}=true",
            f"{settings.instance_label}={settings.instance_id}",
        ]
    }
    dependent: list[str] = []
    for c in client.containers.list(all=True, filters=filters):
        raw = c.labels.get("oduflow.extra_addons", "")
        if not raw:
            continue
        try:
            extras = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        if name in extras:
            branch = c.labels.get(settings.branch_label, c.name)
            dependent.append(branch)

    if dependent:
        raise ConflictError(
            f"Cannot delete extra repo '{name}': used by environments: "
            f"{', '.join(dependent)}"
        )

    shutil.rmtree(path)
    logger.info("Deleted extra repo '%s'", name)
    return {"name": name, "deleted": True}


def fetch_extra_repo(settings: Settings, name: str) -> None:
    path = os.path.join(settings.shared_repos_dir, name)
    if not os.path.isdir(path):
        raise NotFoundError(f"Extra repo '{name}' not found.")

    try:
        subprocess.run(
            ["git", "-C", path, "fetch", "--all", "--prune"],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
            env=GIT_ENV,
        )
    except subprocess.CalledProcessError as e:
        raise ExternalCommandError(
            "git fetch --all --prune", e.returncode, e.stderr or ""
        )
    except subprocess.TimeoutExpired:
        raise ExternalCommandError(
            "git fetch --all --prune", -1, "Fetch timed out (120s)."
        )

    logger.info("Fetched extra repo '%s'", name)


def create_worktree(
    settings: Settings, repo_name: str, branch: str, target_path: str
) -> str:
    bare_path = os.path.join(settings.shared_repos_dir, repo_name)
    if not os.path.isdir(bare_path):
        raise NotFoundError(
            f"Extra repo '{repo_name}' not found. Add it first."
        )

    fetch_extra_repo(settings, repo_name)

    subprocess.run(
        ["git", "-C", bare_path, "worktree", "prune"],
        capture_output=True,
        text=True,
        timeout=30,
        env=GIT_ENV,
    )

    try:
        subprocess.run(
            ["git", "-C", bare_path, "worktree", "add", "--detach", target_path, branch],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
            env=GIT_ENV,
        )
    except subprocess.CalledProcessError as e:
        raise ExternalCommandError(
            "git worktree add",
            e.returncode,
            f"Failed to create worktree for branch '{branch}': {e.stderr or ''}",
        )

    logger.info(
        "Created worktree for %s branch '%s' at %s",
        repo_name, branch, target_path,
    )
    return target_path


def remove_worktree(
    settings: Settings, repo_name: str, target_path: str
) -> None:
    bare_path = os.path.join(settings.shared_repos_dir, repo_name)
    try:
        subprocess.run(
            ["git", "-C", bare_path, "worktree", "remove", target_path, "--force"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            env=GIT_ENV,
        )
        logger.info("Removed worktree %s from repo %s", target_path, repo_name)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        logger.debug(
            "Ignoring worktree removal error for %s (may already be cleaned up)",
            target_path,
        )


def generate_odoo_conf(
    base_conf_path: str, output_path: str, extra_paths: list[str]
) -> str:
    parser = configparser.RawConfigParser()
    parser.optionxform = str
    parser.read(base_conf_path)

    existing = parser.get("options", "addons_path", fallback="/mnt/extra-addons")
    parts = [p.strip() for p in existing.split(",") if p.strip()]
    for p in extra_paths:
        if p not in parts:
            parts.append(p)
    parser.set("options", "addons_path", ",".join(parts))

    with open(output_path, "w") as f:
        parser.write(f)

    logger.info("Generated Odoo config at %s with extra paths: %s", output_path, extra_paths)
    return output_path
