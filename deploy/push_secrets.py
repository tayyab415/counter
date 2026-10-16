#!/usr/bin/env python3
"""Push ~/.config/counter/.env values to Secret Manager as `counter-<key>`.

For each key: create the secret if missing, add a new version, and grant the
Cloud Run runtime service account read access. Prints key names and status
only. Values are piped to `gcloud secrets versions add --data-file=-` on
stdin, so they never appear in argv, logs or the output.

    uv run --with python-dotenv python deploy/push_secrets.py

GCP_PROJECT and RUNTIME_SA override the project and the service account.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from dotenv import dotenv_values

PROJECT = os.environ.get("GCP_PROJECT", "gen-lang-client-0261050164")
KEYS = [
    "ARMORIQ_API_KEY",
    "ARMORIQ_LOGIN",
    "GEMINI_API_KEY",
    "OWNER_PASSWORD",
    "MCP_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_CHANNEL",
]
ROLE = "roles/secretmanager.secretAccessor"


def secret_name(key: str) -> str:
    return "counter-" + key.lower().replace("_", "-")


def gcloud(*args: str, data: bytes | None = None) -> subprocess.CompletedProcess:
    exe = shutil.which("gcloud") or "/snap/bin/gcloud"
    return subprocess.run([exe, *args], input=data, capture_output=True)


def runtime_member() -> str:
    account = os.environ.get("RUNTIME_SA", "").strip()
    if not account:
        r = gcloud("config", "get-value", "account")
        account = r.stdout.decode().strip()
    kind = "serviceAccount" if account.endswith(".gserviceaccount.com") else "user"
    return f"{kind}:{account}"


def main() -> int:
    env_path = Path(os.path.expanduser(os.environ.get("COUNTER_ENV_FILE", "~/.config/counter/.env")))
    if not env_path.is_file():
        print(f"env file not found: {env_path}")
        return 1
    values = dotenv_values(env_path)
    member = runtime_member()
    failures = 0

    for key in KEYS:
        value = values.get(key)
        name = secret_name(key)
        if not value:
            print(f"{name}: {key} not in the env file, skipped")
            continue

        existing = gcloud("secrets", "describe", name, "--project", PROJECT)
        if existing.returncode != 0:
            created = gcloud("secrets", "create", name, "--project", PROJECT,
                             "--replication-policy", "automatic",
                             "--labels", "app=counter")
            if created.returncode != 0:
                detail = created.stderr.decode().strip().splitlines()[-1:] or [""]
                print(f"{name}: create failed: {detail[0][:200]}")
                failures += 1
                continue
            print(f"{name}: created")

        added = gcloud("secrets", "versions", "add", name, "--project", PROJECT, "--data-file=-",
                       data=value.encode())
        if added.returncode != 0:
            detail = added.stderr.decode().strip().splitlines()[-1:] or [""]
            print(f"{name}: version add failed: {detail[0][:200]}")
            failures += 1
            continue
        print(f"{name}: version added")

        bound = gcloud("secrets", "add-iam-policy-binding", name, "--project", PROJECT,
                       f"--member={member}", f"--role={ROLE}")
        if bound.returncode != 0:
            detail = bound.stderr.decode().strip().splitlines()[-1:] or [""]
            print(f"{name}: IAM grant failed: {detail[0][:200]}")
            print("  -> fall back to --env-vars-file from a temp file (see deploy.sh)")
            failures += 1
            continue
        print(f"{name}: {ROLE} granted to {member}")

    print(f"\n{len(KEYS) - failures}/{len(KEYS)} keys ok (project {PROJECT})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
