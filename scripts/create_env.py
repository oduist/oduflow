#!/usr/bin/env python3
"""Create an Oduflow environment for the current git branch via REST API."""

import argparse
import base64
import getpass
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error


def get_current_branch() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def create_environment(
    server_url: str,
    branch_name: str,
    password: str,
    template_name: str = "",
    repo_url: str = "",
    odoo_image: str = "",
    extra_addons: str = "",
    env_vars: str = "",
) -> dict:
    url = f"{server_url.rstrip('/')}/api/environments/create"
    payload = {"branch_name": branch_name}
    if template_name:
        payload["template_name"] = template_name
    if repo_url:
        payload["repo_url"] = repo_url
    if odoo_image:
        payload["odoo_image"] = odoo_image
    if extra_addons:
        payload["extra_addons"] = extra_addons
    if env_vars:
        payload["env_vars"] = env_vars

    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")

    if password:
        credentials = base64.b64encode(f"admin:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {credentials}")

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"ok": False, "error": f"HTTP {e.code}: {body}"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Create Oduflow environment for the current branch")
    parser.add_argument("--server", default="http://localhost:8000",
                        help="Oduflow server URL (default: http://localhost:8000)")
    parser.add_argument("--branch", default="",
                        help="Branch name (default: current git branch)")
    parser.add_argument("--template", default="",
                        help="Template profile name (default: 'default')")
    parser.add_argument("--repo-url", default="",
                        help="Git repository URL (default: from template metadata)")
    parser.add_argument("--odoo-image", default="",
                        help="Odoo Docker image (default: from template metadata)")
    parser.add_argument("--extra-addons", default="",
                        help="Extra addons, e.g. 'enterprise:18.0,themes'")
    parser.add_argument("--env", default="",
                        help="Environment variables, comma-separated KEY=VALUE (e.g. 'WORKERS=2,LIMIT_TIME_CPU=600')")
    parser.add_argument("--password", default="",
                        help="UI password (default: env ODUFLOW_UI_PASSWORD or interactive prompt)")
    args = parser.parse_args()

    branch = args.branch or get_current_branch()
    password = args.password or os.getenv("ODUFLOW_UI_PASSWORD", "") or getpass.getpass("Oduflow UI password: ")

    print(f"Creating environment for branch '{branch}'...")
    result = create_environment(
        server_url=args.server,
        branch_name=branch,
        password=password,
        template_name=args.template,
        repo_url=args.repo_url,
        odoo_image=args.odoo_image,
        extra_addons=args.extra_addons,
        env_vars=args.env,
    )

    if result.get("ok"):
        info = result.get("result", {})
        print(f"\n✅ Environment created!")
        print(f"   URL:       {info.get('url', '—')}")
        print(f"   Container: {info.get('odoo_container', '—')}")
        print(f"   Database:  {info.get('database', '—')}")
        print(f"   Time:      {info.get('elapsed_seconds', '?')}s")
    else:
        print(f"\n❌ Error: {result.get('error', 'Unknown error')}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
