#!/usr/bin/env python3
"""Deploy tieulam-relay CF Worker (API + stream proxy HD1)."""
import hashlib
import json
import os
import sys
import uuid

import requests

CF_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")
ACCOUNT = "1c17b9b516c9a00478f2e538883c7e3b"
RELAY_SECRET = os.environ.get("RELAY_SECRET", "")
CRON = "*/5 * * * *"
WORKER = "tieulam-relay"
SCRIPT = "workers/tieulam-relay.js"

if not CF_TOKEN:
    print("No CLOUDFLARE_API_TOKEN — skipping worker deploy")
    sys.exit(0)


def cf_headers():
    return {"Authorization": f"Bearer {CF_TOKEN}"}


def get_remote_checksum() -> str | None:
    r = requests.get(
        f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/workers/scripts/{WORKER}",
        headers={**cf_headers(), "Accept": "application/javascript"},
        timeout=10,
    )
    if r.ok:
        return hashlib.md5(r.text.encode()).hexdigest()
    return None


def deploy_script() -> bool:
    if not os.path.exists(SCRIPT):
        print(f"{WORKER}: {SCRIPT} not found")
        return False

    with open(SCRIPT, encoding="utf-8") as f:
        code = f.read()

    local_md5 = hashlib.md5(code.encode()).hexdigest()
    remote_md5 = get_remote_checksum()
    if local_md5 == remote_md5:
        print(f"{WORKER}: unchanged (md5={local_md5[:8]})")
        return True

    print(f"{WORKER}: deploying ({len(code)} chars)...")
    boundary = uuid.uuid4().hex
    metadata = json.dumps({"main_module": "worker.js", "compatibility_date": "2024-01-01"})
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="metadata"\r\n'
        f"Content-Type: application/json\r\n\r\n"
        f"{metadata}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="worker.js"; filename="worker.js"\r\n'
        f"Content-Type: application/javascript+module\r\n\r\n"
        f"{code}\r\n"
        f"--{boundary}--\r\n"
    ).encode()

    r = requests.put(
        f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/workers/scripts/{WORKER}",
        headers={**cf_headers(), "Content-Type": f"multipart/form-data; boundary={boundary}"},
        data=body,
        timeout=30,
    )
    j = r.json()
    ok = j.get("success", False)
    print(f"{WORKER}: HTTP {r.status_code} success={ok} errors={j.get('errors', [])}")
    return ok


def sync_secret() -> bool:
    if not RELAY_SECRET:
        print(f"{WORKER}: RELAY_SECRET not set — skip secret")
        return False
    r = requests.put(
        f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/workers/scripts/{WORKER}/secrets",
        headers={**cf_headers(), "Content-Type": "application/json"},
        json={"name": "RELAY_SECRET", "text": RELAY_SECRET, "type": "secret_text"},
        timeout=15,
    )
    j = r.json()
    ok = j.get("success", False)
    print(f"{WORKER}: secret sync success={ok}")
    return ok


def ensure_cron() -> bool:
    r = requests.put(
        f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/workers/scripts/{WORKER}/schedules",
        headers={**cf_headers(), "Content-Type": "application/json"},
        json={"schedules": [{"cron": CRON}]},
        timeout=15,
    )
    j = r.json()
    ok = j.get("success", False)
    print(f"{WORKER}: cron {CRON} success={ok}")
    return ok


if __name__ == "__main__":
    print("=== Deploy tieulam-relay (v6 stream) ===")
    deploy_script()
    sync_secret()
    ensure_cron()
    print("=== Done ===")
