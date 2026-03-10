import logging
import os
import re
import subprocess
from urllib.parse import urlparse

from oduflow.errors import ExternalCommandError, FlowError

logger = logging.getLogger("oduflow")

# Base env for git operations (no credentials — just disables interactive prompts)
_GIT_BASE_ENV = {
    **os.environ,
    "GIT_TERMINAL_PROMPT": "0",
    "HOME": os.environ.get("HOME", "/root"),
}


def git_env_for_team(cred_file: str) -> dict[str, str]:
    """Build a git environment dict with per-team credential helper."""
    return {
        **_GIT_BASE_ENV,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": f"store --file {cred_file}",
    }


class InvalidRepoURLError(FlowError):
    """Repository URL is invalid or missing credentials."""


class RepoAuthError(FlowError):
    """Repository authentication failed. Call setup_repo_auth first."""


def validate_repo_url(repo_url: str) -> None:
    """Reject non-HTTPS repository URLs early.

    SSH URLs (``git@host:path`` or ``ssh://``) cause the server to hang
    because SSH prompts for host-key verification interactively.
    """
    # SCP-like SSH syntax: git@github.com:owner/repo.git
    if re.match(r"^[a-zA-Z0-9._-]+@[a-zA-Z0-9._-]+:", repo_url):
        raise InvalidRepoURLError(
            "SSH repository URLs are not supported. "
            "Use HTTPS format: https://github.com/owner/repo.git"
        )
    parsed = urlparse(repo_url)
    if parsed.scheme not in ("https", "http"):
        raise InvalidRepoURLError(
            f"Only HTTPS repository URLs are supported (got {parsed.scheme or 'unknown'}://). "
            "Use format: https://github.com/owner/repo.git"
        )


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


def _store_git_credentials(
    host: str, username: str, password: str, cred_file: str
) -> None:
    os.makedirs(os.path.dirname(cred_file), exist_ok=True)
    env = git_env_for_team(cred_file)

    credential_input = (
        f"protocol=https\nhost={host}\nusername={username}\npassword={password}\n\n"
    )
    subprocess.run(
        ["git", "credential", "approve"],
        input=credential_input.encode(),
        check=True,
        capture_output=True,
        env=env,
    )
    logger.info("Git credentials stored for host=%s user=%s", host, username)


def setup_repo_auth(repo_url: str, cred_file: str) -> dict[str, str]:
    clean_url, host, username, password = _parse_authenticated_url(repo_url)

    _store_git_credentials(host, username, password, cred_file)

    env = git_env_for_team(cred_file)
    try:
        subprocess.run(
            ["git", "ls-remote", "--heads", clean_url],
            check=True,
            capture_output=True,
            timeout=30,
            env=env,
        )
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.decode("utf-8") if e.stderr else str(e)
        raise ExternalCommandError(
            "git ls-remote (auth test)",
            e.returncode,
            f"Credentials saved but access check failed: {error_msg}",
        )
    except subprocess.TimeoutExpired:
        raise ExternalCommandError(
            "git ls-remote (auth test)",
            -1,
            "Access check timed out (30s).",
        )

    logger.info("Repo auth verified for %s", clean_url)
    return {"repo_url": clean_url, "host": host, "status": "authenticated"}


def inject_credential_user(repo_url: str, git_user: str) -> str:
    """Inject username into repo URL for credential matching."""
    if not git_user:
        return repo_url
    parsed = urlparse(repo_url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        return repo_url
    if parsed.username:
        return repo_url
    netloc = f"{git_user}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return parsed._replace(netloc=netloc).geturl()


def list_credentials(cred_file: str) -> list[dict]:
    if not os.path.exists(cred_file):
        return []

    results = []
    with open(cred_file, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = urlparse(line)
            except Exception:
                continue
            if not parsed.hostname or not parsed.username:
                continue
            results.append(
                {
                    "host": parsed.hostname,
                    "username": parsed.username,
                    "token_masked": (parsed.password or "")[:4] + "****"
                    if parsed.password and len(parsed.password) > 4
                    else "****",
                }
            )
    return results


def validate_credential(host: str, username: str, cred_file: str) -> str:
    from urllib.request import Request, urlopen
    from urllib.error import URLError, HTTPError

    if not os.path.exists(cred_file):
        return "invalid"

    token = None
    with open(cred_file, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                parsed = urlparse(line)
            except Exception:
                continue
            if parsed.hostname == host and parsed.username == username:
                token = parsed.password
                break

    if not token:
        return "invalid"

    api_urls = {
        "github.com": "https://api.github.com/user",
        "gitlab.com": "https://gitlab.com/api/v4/user",
        "bitbucket.org": "https://api.bitbucket.org/2.0/user",
    }

    api_url = api_urls.get(host)
    if not api_url:
        return "valid"

    try:
        req = Request(api_url)
        if host == "github.com":
            req.add_header("Authorization", f"token {token}")
        elif host == "gitlab.com":
            req.add_header("PRIVATE-TOKEN", token)
        elif host == "bitbucket.org":
            import base64

            b64 = base64.b64encode(f"{username}:{token}".encode()).decode()
            req.add_header("Authorization", f"Basic {b64}")
        req.add_header("User-Agent", "oduflow")
        resp = urlopen(req, timeout=10)
        return "valid" if resp.status == 200 else "invalid"
    except HTTPError as e:
        return "invalid" if e.code in (401, 403) else "unknown"
    except (URLError, OSError):
        return "unknown"


def delete_credential(host: str, username: str, cred_file: str) -> bool:
    if not os.path.exists(cred_file):
        return False

    with open(cred_file, "r") as f:
        lines = f.readlines()

    new_lines = []
    removed = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = urlparse(stripped)
        except Exception:
            new_lines.append(line)
            continue
        if parsed.hostname == host and parsed.username == username:
            removed = True
            continue
        new_lines.append(line)

    if removed:
        with open(cred_file, "w") as f:
            f.writelines(new_lines)
        logger.info("Credential deleted for host=%s user=%s", host, username)

    return removed


def pull_repo(
    repo_path: str, branch: str, cred_file: str = ""
) -> tuple[str, list[str]]:
    """Pull latest changes and return ``(old_head, changed_files)``.

    *old_head* is the commit hash before the pull — callers can pass it
    as ``base_ref`` to :func:`git_analysis.classify_changes` so that
    manifest / field comparisons cover the full range of pulled commits.
    """
    env = git_env_for_team(cred_file) if cred_file else _GIT_BASE_ENV

    old_head = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()

    try:
        subprocess.run(
            [
                "git",
                "-C",
                repo_path,
                "fetch",
                "origin",
                f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        subprocess.run(
            ["git", "-C", repo_path, "reset", "--hard", f"origin/{branch}"],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
    except subprocess.CalledProcessError as e:
        error_msg = e.stderr or str(e)
        raise ExternalCommandError("git pull", e.returncode, error_msg)
    except subprocess.TimeoutExpired:
        raise ExternalCommandError("git pull", -1, "Fetch timed out (60s).")

    new_head = subprocess.run(
        ["git", "-C", repo_path, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()

    if old_head == new_head:
        return old_head, []

    result = subprocess.run(
        [
            "git",
            "-C",
            repo_path,
            "diff",
            "--name-only",
            f"{old_head}..{new_head}",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return old_head, [f for f in result.stdout.strip().splitlines() if f]


def parse_manifest(manifest_path: str) -> dict:
    """Parse an Odoo __manifest__.py file and return its dict."""
    import ast

    with open(manifest_path, "r") as f:
        return ast.literal_eval(f.read())
