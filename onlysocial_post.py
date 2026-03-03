import os
import json
import sys
import requests
from datetime import datetime, timezone

BASE = "https://app.onlysocial.io/os/api"

def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()

def onlysocial_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": env("USER_AGENT", "IttyBittyGamingNews/OnlySocial"),
    }

def list_accounts(token: str, workspace_uuid: str) -> list:
    url = f"{BASE}/{workspace_uuid}/accounts"
    r = requests.get(url, headers=onlysocial_headers(token), timeout=30)
    r.raise_for_status()
    data = r.json()
    # Docs don’t guarantee exact shape in the snippet, so be defensive:
    if isinstance(data, dict):
        for key in ("data", "accounts", "payload", "result"):
            if key in data and isinstance(data[key], list):
                return data[key]
    if isinstance(data, list):
        return data
    return []

def create_post(token: str, workspace_uuid: str, account_uuids: list, content: str) -> dict:
    """
    Create post endpoint per docs:
    POST https://app.onlysocial.io/os/api/{workspaceUuid}/posts
    """
    url = f"{BASE}/{workspace_uuid}/posts"
    payload = {
        "accounts": account_uuids,
        "content": content,
    }
    r = requests.post(url, headers=onlysocial_headers(token), data=json.dumps(payload), timeout=30)
    r.raise_for_status()
    return r.json()

def schedule_post_now(token: str, workspace_uuid: str, post_uuid: str) -> dict:
    """
    Schedule endpoint per docs:
    POST https://app.onlysocial.io/os/api/{workspaceUuid}/posts/schedule/{postUuid}
    Body example: {"postNow": true}
    """
    url = f"{BASE}/{workspace_uuid}/posts/schedule/{post_uuid}"
    payload = {"postNow": True}
    r = requests.post(url, headers=onlysocial_headers(token), data=json.dumps(payload), timeout=30)
    r.raise_for_status()
    return r.json()

def load_digest_text() -> str:
    """
    We’ll read the digest output from a file your workflow creates.
    If you don’t have this file yet, we fall back to a minimal message.
    """
    path = env("DIGEST_EXPORT_FILE", "digest_latest.txt")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()

    # fallback
    return f"Itty Bitty Gaming News — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"

def main():
    token = env("ONLYSOCIAL_TOKEN")
    workspace = env("ONLYSOCIAL_WORKSPACE_UUID")
    account_csv = env("ONLYSOCIAL_ACCOUNT_UUIDS")

    if not token or not workspace:
        print("[ONLYSOCIAL] Missing ONLYSOCIAL_TOKEN or ONLYSOCIAL_WORKSPACE_UUID. Skipping.")
        sys.exit(0)

    # If no accounts provided, print accounts so you can copy UUIDs into secrets
    if not account_csv:
        print("[ONLYSOCIAL] ONLYSOCIAL_ACCOUNT_UUIDS is empty. Listing accounts to help you set it...")
        accounts = list_accounts(token, workspace)
        print(json.dumps(accounts, indent=2))
        sys.exit(0)

    account_uuids = [a.strip() for a in account_csv.split(",") if a.strip()]
    if not account_uuids:
        print("[ONLYSOCIAL] No valid account UUIDs parsed from ONLYSOCIAL_ACCOUNT_UUIDS.")
        sys.exit(1)

    content = load_digest_text()

    # Create
    created = create_post(token, workspace, account_uuids, content)
    print("[ONLYSOCIAL] Create response keys:", list(created.keys()) if isinstance(created, dict) else type(created))

    # Try to locate post uuid in common places
    post_uuid = None
    if isinstance(created, dict):
        for key in ("uuid", "id", "postUuid", "post_uuid"):
            if key in created and isinstance(created[key], str):
                post_uuid = created[key]
                break
        # nested
        if not post_uuid:
            for key in ("data", "payload", "post"):
                if key in created and isinstance(created[key], dict):
                    for k2 in ("uuid", "id", "postUuid", "post_uuid"):
                        if k2 in created[key] and isinstance(created[key][k2], str):
                            post_uuid = created[key][k2]
                            break

    if not post_uuid:
        print("[ONLYSOCIAL] Could not find post UUID in create response. Full response:")
        print(json.dumps(created, indent=2))
        sys.exit(1)

    # Schedule "post now"
    scheduled = schedule_post_now(token, workspace, post_uuid)
    print("[ONLYSOCIAL] Scheduled:", json.dumps(scheduled, indent=2))

if __name__ == "__main__":
    main()
