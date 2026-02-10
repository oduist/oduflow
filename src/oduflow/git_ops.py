import logging
import os
import shutil
import subprocess
import tempfile
from urllib.parse import urlparse

from oduflow.errors import ExternalCommandError, FlowError

logger = logging.getLogger("oduflow")

GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


class InvalidRepoURLError(FlowError):
    """Repository URL is invalid or missing credentials."""


class RepoAuthError(FlowError):
    """Repository authentication failed. Call setup_repo_auth first."""


def _parse_authenticated_url(repo_url: str) -> tuple[str, str, str, str]:
    parsed = urlparse(repo_url)
    if parsed.scheme not in ("https", "http"):
        raise InvalidRepoURLError(
            f"URL must use https:// scheme, got: {parsed.scheme}://"
        )
    if not parsed.username or not parsed.password:
        raise InvalidRepoURLError(
            "URL must contain credentials: https://user:PAT@github.com/owner/repo.git"
        )
    clean_url = parsed._replace(netloc=parsed.hostname or "").geturl()
    return clean_url, parsed.hostname or "", parsed.username, parsed.password


def _store_git_credentials(host: str, username: str, password: str) -> None:
    subprocess.run(
        ["git", "config", "--global", "credential.helper", "store"],
        check=True,
        capture_output=True,
        env=GIT_ENV,
    )

    credential_input = (
        f"protocol=https\n"
        f"host={host}\n"
        f"username={username}\n"
        f"password={password}\n"
        f"\n"
    )
    subprocess.run(
        ["git", "credential", "approve"],
        input=credential_input.encode(),
        check=True,
        capture_output=True,
        env=GIT_ENV,
    )
    logger.info("Git credentials stored for host=%s user=%s", host, username)


def setup_repo_auth(repo_url: str) -> dict[str, str]:
    clean_url, host, username, password = _parse_authenticated_url(repo_url)

    _store_git_credentials(host, username, password)

    tmp_dir = tempfile.mkdtemp(prefix="flow-auth-test-")
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", clean_url, tmp_dir],
            check=True,
            capture_output=True,
            timeout=60,
            env=GIT_ENV,
        )
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.decode("utf-8") if e.stderr else str(e)
        raise ExternalCommandError(
            "git clone (auth test)", e.returncode,
            f"Credentials saved but test clone failed: {error_msg}",
        )
    except subprocess.TimeoutExpired:
        raise ExternalCommandError(
            "git clone (auth test)", -1,
            "Test clone timed out (60s).",
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    logger.info("Repo auth verified for %s", clean_url)
    return {"repo_url": clean_url, "host": host, "status": "authenticated"}


def pull_repo(repo_path: str, branch: str) -> list[str]:
    """Pull latest changes and return list of changed file paths (relative to repo root)."""
    old_head = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=GIT_ENV,
    ).stdout.strip()

    try:
        subprocess.run(
            ["git", "-C", repo_path, "pull", "--rebase", "origin", branch],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
            env=GIT_ENV,
        )
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr or str(e)
        raise ExternalCommandError("git pull", e.returncode, error_msg)
    except subprocess.TimeoutExpired:
        raise ExternalCommandError("git pull", -1, "Pull timed out (60s).")

    new_head = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=GIT_ENV,
    ).stdout.strip()

    if old_head == new_head:
        return []

    result = subprocess.run(
        ["git", "-C", repo_path, "diff", "--name-only", f"{old_head}..{new_head}"],
        check=True,
        capture_output=True,
        text=True,
        env=GIT_ENV,
    )
    return [f for f in result.stdout.strip().splitlines() if f]


def parse_manifest(manifest_path: str) -> dict:
    """Parse an Odoo __manifest__.py file and return its dict."""
    import ast
    with open(manifest_path, "r") as f:
        return ast.literal_eval(f.read())
