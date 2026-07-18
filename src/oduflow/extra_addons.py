import configparser
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from typing import Any

from oduflow.docker_ops.client import get_client
from oduflow.errors import (
    ConflictError,
    ExternalCommandError,
    NotFoundError,
    ProtectedError,
)
from oduflow.git_ops import RepoAuthError, git_env_for_team
from oduflow.naming import sanitize_repo_url
from oduflow.settings import Settings, TeamSettings

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


def clone_extra_repo(
    team: TeamSettings, name: str, repo_url: str, git_user: str = ""
) -> dict[str, Any]:
    if not _NAME_RE.match(name):
        raise ValueError(
            f"Invalid repo name '{name}': only [a-zA-Z0-9_-] allowed, "
            "no dots or slashes, max 63 chars."
        )

    target = os.path.join(team.shared_repos_dir, name)
    if os.path.exists(target):
        raise ConflictError(f"Extra repo '{name}' already exists at {target}")

    os.makedirs(team.shared_repos_dir, exist_ok=True)

    from oduflow.git_ops import inject_credential_user

    clone_url = inject_credential_user(repo_url, git_user)
    cred_env = git_env_for_team(team.git_credentials_file())

    try:
        # Shallow clone (--depth 1): drop history so large repos like Odoo
        # Enterprise (years of commits) clone fast instead of timing out.
        # --no-single-branch keeps the tip of *every* branch, so a single bare
        # repo still serves worktrees for any branch/version (16.0, 17.0, 18.0,
        # ...) exactly as the full clone did.
        subprocess.run(
            [
                "git",
                "clone",
                "--bare",
                "--depth",
                "1",
                "--no-single-branch",
                clone_url,
                target,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
            env=cred_env,
        )
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        if any(kw in stderr for kw in _AUTH_ERROR_KEYWORDS):
            raise RepoAuthError(
                f"Authentication failed for '{sanitize_repo_url(repo_url)}'. "
                "Use setup_repo_auth to configure credentials first."
            )
        raise ExternalCommandError("git clone --bare", e.returncode, stderr)
    except subprocess.TimeoutExpired:
        raise ExternalCommandError("git clone --bare", -1, "Clone timed out (300s).")

    # git clone --bare does not set a fetch refspec, so subsequent
    # git fetch --all would only write to FETCH_HEAD without updating
    # local branches.  Configure the refspec explicitly.
    subprocess.run(
        [
            "git",
            "-C",
            target,
            "config",
            "remote.origin.fetch",
            "+refs/heads/*:refs/heads/*",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
        env=GIT_ENV,
    )

    logger.info("Cloned extra repo '%s' from %s", name, repo_url)
    return {"name": name, "repo_url": repo_url, "path": target}


def create_local_repo(
    team: TeamSettings, name: str, source_dir: str, branch: str
) -> dict[str, Any]:
    """Create an extra-addons repo from local files, with no remote origin.

    Used by the Odoo.sh import for addons that cannot be cloned (Enterprise,
    Themes, private extra repos): the uploaded directory is seeded into a real
    bare git repo with a single *branch*, so the normal worktree / mount /
    pull_and_apply machinery works unchanged. A ``.local`` marker file records
    that the repo has no origin; :func:`fetch_extra_repo` short-circuits on it,
    so worktree creation and pulls never attempt a (non-existent) fetch.
    """
    if not _NAME_RE.match(name):
        raise ValueError(
            f"Invalid repo name '{name}': only [a-zA-Z0-9_-] allowed, "
            "no dots or slashes, max 63 chars."
        )
    if not branch:
        raise ValueError("A branch name is required for a local extra repo.")

    target = os.path.join(team.shared_repos_dir, name)
    if os.path.exists(target):
        raise ConflictError(f"Extra repo '{name}' already exists at {target}")

    os.makedirs(team.shared_repos_dir, exist_ok=True)

    # A clean environment has no git identity configured, so the seed commit
    # would fail — supply one explicitly for this operation only.
    seed_env = {
        **GIT_ENV,
        "GIT_AUTHOR_NAME": "Oduflow",
        "GIT_AUTHOR_EMAIL": "import@oduflow.local",
        "GIT_COMMITTER_NAME": "Oduflow",
        "GIT_COMMITTER_EMAIL": "import@oduflow.local",
    }

    def _git(args: list[str], timeout: int = 300) -> None:
        subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=seed_env,
        )

    tmp = tempfile.mkdtemp(prefix="oduflow-localrepo-")
    try:
        _git(["git", "init", "--bare", target], timeout=30)
        _git(["git", "-C", tmp, "init"], timeout=30)
        # Copy the source tree in, skipping any stray VCS metadata (Odoo.sh
        # worktrees leave a .git gitdir pointer; the tar upload excludes it, but
        # be defensive here too).
        for entry in os.listdir(source_dir):
            if entry == ".git":
                continue
            src = os.path.join(source_dir, entry)
            dst = os.path.join(tmp, entry)
            if os.path.isdir(src):
                shutil.copytree(src, dst, symlinks=True)
            else:
                shutil.copy2(src, dst)
        _git(["git", "-C", tmp, "add", "-A"], timeout=120)
        _git(
            [
                "git",
                "-C",
                tmp,
                "commit",
                "--allow-empty",
                "-m",
                "Imported from Odoo.sh",
            ],
            timeout=120,
        )
        _git(["git", "-C", tmp, "branch", "-M", branch], timeout=30)
        _git(["git", "-C", tmp, "push", target, f"{branch}:{branch}"], timeout=300)
    except subprocess.CalledProcessError as e:
        shutil.rmtree(target, ignore_errors=True)
        raise ExternalCommandError(
            "git (create local repo)", e.returncode, e.stderr or ""
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(target, ignore_errors=True)
        raise ExternalCommandError("git (create local repo)", -1, "Seed timed out.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # Mark as local (no origin) — the marker is what fetch_extra_repo keys on.
    open(os.path.join(target, ".local"), "w").close()

    logger.info("Created local extra repo '%s' (branch '%s')", name, branch)
    return {
        "name": name,
        "repo_url": "",
        "path": target,
        "local": True,
        "branch": branch,
    }


def is_local_repo(team: TeamSettings, name: str) -> bool:
    """True if the extra repo is a remote-less local repo (Odoo.sh import)."""
    return os.path.exists(os.path.join(team.shared_repos_dir, name, ".local"))


def list_extra_repos(team: TeamSettings) -> list[dict[str, Any]]:
    repos_dir = team.shared_repos_dir
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

        protected = os.path.exists(os.path.join(path, ".protected"))
        local = os.path.exists(os.path.join(path, ".local"))
        result.append(
            {
                "name": entry,
                "repo_url": sanitize_repo_url(url),
                "branches": branches,
                "protected": protected,
                "local": local,
            }
        )

    return result


def is_extra_repo_protected(team: TeamSettings, name: str) -> bool:
    path = os.path.join(team.shared_repos_dir, name)
    return os.path.exists(os.path.join(path, ".protected"))


def protect_extra_repo(team: TeamSettings, name: str) -> dict[str, Any]:
    path = os.path.join(team.shared_repos_dir, name)
    if not os.path.isdir(path):
        raise NotFoundError(f"Extra repo '{name}' not found.")
    marker = os.path.join(path, ".protected")
    open(marker, "w").close()
    logger.info("Extra repo protected: %s", name)
    return {"name": name, "protected": True}


def unprotect_extra_repo(team: TeamSettings, name: str) -> dict[str, Any]:
    path = os.path.join(team.shared_repos_dir, name)
    if not os.path.isdir(path):
        raise NotFoundError(f"Extra repo '{name}' not found.")
    marker = os.path.join(path, ".protected")
    if os.path.exists(marker):
        os.remove(marker)
    logger.info("Extra repo unprotected: %s", name)
    return {"name": name, "protected": False}


def delete_extra_repo(
    settings: Settings, team: TeamSettings, name: str
) -> dict[str, Any]:
    path = os.path.join(team.shared_repos_dir, name)
    if not os.path.exists(path):
        raise NotFoundError(f"Extra repo '{name}' not found.")

    if is_extra_repo_protected(team, name):
        raise ProtectedError(
            f"Extra repo '{name}' is protected. Unprotect it before deleting."
        )

    client = get_client()
    filters = {
        "label": [
            f"{settings.managed_label}=true",
            f"{settings.team_label}={team.team_id}",
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


def _get_branch_refs(repo_path: str) -> dict[str, str]:
    """Return a mapping of branch name → commit SHA for all local branches."""
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                repo_path,
                "for-each-ref",
                "--format=%(refname:short) %(objectname)",
                "refs/heads/",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            env=GIT_ENV,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {}
    refs: dict[str, str] = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            refs[parts[0]] = parts[1]
    return refs


def fetch_extra_repo(
    team: TeamSettings, name: str, branch: str | None = None
) -> dict[str, Any]:
    """Fetch latest changes and return a summary of what changed.

    When *branch* is given, only that one branch is fetched (a targeted
    ``git fetch origin +refs/heads/<branch>:refs/heads/<branch>``) instead of
    ``--all``. Creating/updating a worktree needs just its own branch, and
    fetching every branch of a large repo like Odoo Enterprise otherwise times
    out (issue: "Fetch timed out"). The explicit "update repo" path passes no
    branch and still fetches everything to report changes across all branches.

    Returns a dict with keys: name, up_to_date, new_branches,
    deleted_branches, updated_branches.
    """
    path = os.path.join(team.shared_repos_dir, name)
    if not os.path.isdir(path):
        raise NotFoundError(f"Extra repo '{name}' not found.")

    # Local (remote-less) repos have no origin to fetch from. Short-circuit so
    # every caller — create_worktree, pull_extra_worktree, the pull REST/MCP
    # path — treats them as always up to date instead of failing on git fetch.
    if os.path.exists(os.path.join(path, ".local")):
        return {
            "name": name,
            "local": True,
            "up_to_date": True,
            "new_branches": [],
            "deleted_branches": [],
            "updated_branches": [],
        }

    # Ensure fetch refspec is configured (bare repos created before the fix lack it)
    try:
        result = subprocess.run(
            ["git", "-C", path, "config", "--get", "remote.origin.fetch"],
            capture_output=True,
            text=True,
            timeout=10,
            env=GIT_ENV,
        )
        if result.returncode != 0 or not result.stdout.strip():
            subprocess.run(
                [
                    "git",
                    "-C",
                    path,
                    "config",
                    "remote.origin.fetch",
                    "+refs/heads/*:refs/heads/*",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
                env=GIT_ENV,
            )
            logger.info("Added missing fetch refspec for extra repo '%s'", name)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass  # best-effort; fetch will still run

    refs_before = _get_branch_refs(path)
    cred_env = git_env_for_team(team.git_credentials_file())

    if branch:
        # Targeted single-branch fetch: pull only the requested branch's tip so
        # a large repo doesn't time out fetching every version branch. The '+'
        # forces the local ref to match after a rebase/force-push upstream.
        fetch_args = [
            "git",
            "-C",
            path,
            "fetch",
            "origin",
            f"+refs/heads/{branch}:refs/heads/{branch}",
            "--recurse-submodules=no",
        ]
        fetch_label = f"git fetch origin {branch}"
    else:
        fetch_args = [
            "git",
            "-C",
            path,
            "fetch",
            "--all",
            "--prune",
            "--recurse-submodules=no",
        ]
        fetch_label = "git fetch --all --prune"

    try:
        subprocess.run(
            fetch_args,
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
            env=cred_env,
        )
    except subprocess.CalledProcessError as e:
        raise ExternalCommandError(fetch_label, e.returncode, e.stderr or "")
    except subprocess.TimeoutExpired:
        raise ExternalCommandError(fetch_label, -1, "Fetch timed out (120s).")

    refs_after = _get_branch_refs(path)

    new_branches = sorted(set(refs_after) - set(refs_before))
    deleted_branches = sorted(set(refs_before) - set(refs_after))
    updated_branches: list[dict[str, Any]] = []
    for branch_name in sorted(set(refs_before) & set(refs_after)):
        old_sha = refs_before[branch_name]
        new_sha = refs_after[branch_name]
        if old_sha != new_sha:
            try:
                count_result = subprocess.run(
                    ["git", "-C", path, "rev-list", "--count", f"{old_sha}..{new_sha}"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    env=GIT_ENV,
                )
                new_commits = int(count_result.stdout.strip())
            except (
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
                ValueError,
            ):
                new_commits = 0
            updated_branches.append(
                {
                    "branch": branch_name,
                    "new_commits": new_commits,
                }
            )

    up_to_date = not new_branches and not deleted_branches and not updated_branches
    logger.info("Fetched extra repo '%s' (up_to_date=%s)", name, up_to_date)
    return {
        "name": name,
        "up_to_date": up_to_date,
        "new_branches": new_branches,
        "deleted_branches": deleted_branches,
        "updated_branches": updated_branches,
    }


def create_worktree(
    team: TeamSettings, repo_name: str, branch: str, target_path: str
) -> str:
    bare_path = os.path.join(team.shared_repos_dir, repo_name)
    if not os.path.isdir(bare_path):
        raise NotFoundError(f"Extra repo '{repo_name}' not found. Add it first.")

    fetch_extra_repo(team, repo_name, branch=branch)

    subprocess.run(
        ["git", "-C", bare_path, "worktree", "prune"],
        capture_output=True,
        text=True,
        timeout=30,
        env=GIT_ENV,
    )

    try:
        subprocess.run(
            [
                "git",
                "-C",
                bare_path,
                "worktree",
                "add",
                "--detach",
                target_path,
                branch,
            ],
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
        repo_name,
        branch,
        target_path,
    )
    return target_path


def remove_worktree(team: TeamSettings, repo_name: str, target_path: str) -> None:
    bare_path = os.path.join(team.shared_repos_dir, repo_name)
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
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        logger.debug(
            "Ignoring worktree removal error for %s (may already be cleaned up)",
            target_path,
        )


def pull_extra_worktree(
    team: TeamSettings, repo_name: str, branch: str, worktree_path: str
) -> tuple[str, list[str]]:
    """Fetch the bare repo and reset the worktree to the branch tip.

    Returns ``(old_head, changed_files)`` where *old_head* is the
    commit hash before the pull and *changed_files* are paths relative
    to the worktree root.  Returns ``("", [])`` if already up to date.
    """
    fetch_extra_repo(team, repo_name, branch=branch)

    old_head = subprocess.run(
        ["git", "-C", worktree_path, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=GIT_ENV,
    ).stdout.strip()

    try:
        subprocess.run(
            ["git", "-C", worktree_path, "reset", "--hard", branch],
            check=True,
            capture_output=True,
            text=True,
            env=GIT_ENV,
        )
    except subprocess.CalledProcessError as e:
        raise ExternalCommandError("git reset --hard", e.returncode, e.stderr or "")

    new_head = subprocess.run(
        ["git", "-C", worktree_path, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=GIT_ENV,
    ).stdout.strip()

    if old_head == new_head:
        return "", []

    result = subprocess.run(
        ["git", "-C", worktree_path, "diff", "--name-only", f"{old_head}..{new_head}"],
        check=True,
        capture_output=True,
        text=True,
        env=GIT_ENV,
    )
    changed = [f for f in result.stdout.strip().splitlines() if f]
    logger.info(
        "Updated worktree %s/%s: %d files changed",
        repo_name,
        branch,
        len(changed),
    )
    return old_head, changed


def resolve_main_addons_path(repo_path: str) -> str:
    """Container addons path for the main repo.

    Odoo scans addons_path non-recursively, so when a repo keeps its modules in
    a top-level ``addons/`` directory, point Odoo at ``/mnt/extra-addons/addons``
    instead of the repo root.
    """
    if os.path.isdir(os.path.join(repo_path, "addons")):
        return "/mnt/extra-addons/addons"
    return "/mnt/extra-addons"


def generate_odoo_conf(
    base_conf_path: str,
    output_path: str,
    extra_paths: list[str],
    main_addons_path: str = "/mnt/extra-addons",
    overrides: dict[str, str] | None = None,
) -> str:
    parser = configparser.RawConfigParser()
    # Preserve option case (Odoo config keys are case-sensitive); the default
    # optionxform lowercases them. Assigning to this method is the documented
    # configparser idiom but trips mypy's method-assignment check.
    parser.optionxform = str  # type: ignore[method-assign,assignment]
    parser.read(base_conf_path)

    existing = parser.get("options", "addons_path", fallback="/mnt/extra-addons")
    parts = [p.strip() for p in existing.split(",") if p.strip()]
    # Repo keeps modules in addons/ → point at that subdir, not the repo root.
    parts = [main_addons_path if p == "/mnt/extra-addons" else p for p in parts]
    if main_addons_path not in parts:
        parts.insert(0, main_addons_path)
    for p in extra_paths:
        if p not in parts:
            parts.append(p)
    parser.set("options", "addons_path", ",".join(parts))

    # Applied after the merge: the production profile injects auto-tuned
    # worker/limit settings that must win over whatever the base conf says.
    for key, value in (overrides or {}).items():
        parser.set("options", key, value)

    # Strip DB connection keys — these are managed via container env vars
    # (HOST, USER, PASSWORD).  If left in the conf file the Odoo entrypoint
    # uses them instead of the env vars, breaking per-environment credentials.
    for key in ("db_host", "db_port", "db_user", "db_password"):
        parser.remove_option("options", key)

    with open(output_path, "w") as f:
        parser.write(f)

    logger.info(
        "Generated Odoo config at %s with extra paths: %s", output_path, extra_paths
    )
    return output_path
