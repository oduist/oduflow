#!/usr/bin/env python3
"""Sync (pull & reload) an Oduflow environment for the current git branch via REST API."""

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


def sync_environment(server_url: str, branch_name: str, password: str) -> dict:
    url = f"{server_url.rstrip('/')}/api/environments/{branch_name}/sync"
    req = urllib.request.Request(url, data=b"", method="POST")
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
    parser = argparse.ArgumentParser(description="Sync Oduflow environment for the current branch")
    parser.add_argument("--server", default="http://localhost:8000",
                        help="Oduflow server URL (default: http://localhost:8000)")
    parser.add_argument("--branch", default="",
                        help="Branch name (default: current git branch)")
    parser.add_argument("--password", default="",
                        help="UI password (default: env ODUFLOW_UI_PASSWORD or interactive prompt)")
    args = parser.parse_args()

    branch = args.branch or get_current_branch()
    password = args.password or os.getenv("ODUFLOW_UI_PASSWORD", "") or getpass.getpass("Oduflow UI password: ")

    print(f"Syncing environment for branch '{branch}'...")
    result = sync_environment(
        server_url=args.server,
        branch_name=branch,
        password=password,
    )

    if result.get("ok"):
        info = result.get("result", {})
        print(f"\n✅ Environment synced!")
        if isinstance(info, dict):
            for key, value in info.items():
                print(f"   {key}: {value}")
        else:
            print(f"   {info}")
    else:
        print(f"\n❌ Error: {result.get('error', 'Unknown error')}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
