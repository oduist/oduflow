import json
import logging
import os
import secrets

from oduflow.naming import get_workspace_path, slugify_branch

logger = logging.getLogger("oduflow")

_CREDENTIALS_FILE = "env_credentials.json"


def generate_pg_username(env_name: str, team_id: str) -> str:
    slug = slugify_branch(env_name)
    username = f"u_{team_id}_{slug}"
    return username[:63]


def generate_pg_password() -> str:
    return secrets.token_urlsafe(18)


def create_credentials(
    env_name: str, team_id: str, workspaces_dir: str
) -> dict[str, str]:
    username = generate_pg_username(env_name, team_id)
    password = generate_pg_password()
    creds = {"pg_user": username, "pg_password": password}

    workspace_path = get_workspace_path(env_name, workspaces_dir)
    os.makedirs(workspace_path, exist_ok=True)
    creds_path = os.path.join(workspace_path, _CREDENTIALS_FILE)
    with open(creds_path, "w") as f:
        json.dump(creds, f)

    logger.info(
        "Created PG credentials for environment '%s' (user=%s)", env_name, username
    )
    return creds


def load_credentials(
    env_name: str,
    workspaces_dir: str,
    fallback_user: str,
    fallback_password: str,
) -> dict[str, str]:
    workspace_path = get_workspace_path(env_name, workspaces_dir)
    creds_path = os.path.join(workspace_path, _CREDENTIALS_FILE)
    if os.path.isfile(creds_path):
        with open(creds_path) as f:
            return json.load(f)
    return {"pg_user": fallback_user, "pg_password": fallback_password}


def delete_credentials(env_name: str, workspaces_dir: str) -> None:
    workspace_path = get_workspace_path(env_name, workspaces_dir)
    creds_path = os.path.join(workspace_path, _CREDENTIALS_FILE)
    if os.path.isfile(creds_path):
        os.remove(creds_path)
        logger.info("Deleted PG credentials for environment '%s'", env_name)
