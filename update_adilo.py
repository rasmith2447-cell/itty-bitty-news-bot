#!/usr/bin/env python3
"""
update_adilo.py — Itty Bitty Gaming News
Detects the most recently uploaded video in the Adilo project and
updates the GitHub Actions repository variable ADILO_CURRENT_VIDEO_ID.

Fetches ONE page of 50 files only — no pagination — since the Adilo
API was returning thousands of files across all projects when paginated.
We try both ends of the 50-file list to find the newest video.
"""

import os
import sys
import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


ADILO_PUBLIC_KEY = env("ADILO_PUBLIC_KEY")
ADILO_SECRET_KEY = env("ADILO_SECRET_KEY")
ADILO_PROJECT_ID = env("ADILO_PROJECT_ID")
ADILO_API_BASE   = "https://adilo-api.bigcommand.com/v1"
ADILO_WATCH_BASE = "https://adilo.bigcommand.com/watch"

GH_PAT           = env("GH_PAT")
GH_REPO          = env("GITHUB_REPOSITORY")
GH_API_BASE      = "https://api.github.com"
VARIABLE_NAME    = "ADILO_CURRENT_VIDEO_ID"

# ADILO_LIST_ORDER controls which end of the list is treated as newest.
# "last"  = payload[-1] is newest (default — upload order oldest->newest)
# "first" = payload[0]  is newest
# Set as a GitHub repo variable if the default picks the wrong video.
LIST_ORDER       = env("ADILO_LIST_ORDER", "last").lower()


# ---------------------------------------------------------------------------
# ADILO API
# ---------------------------------------------------------------------------

def adilo_headers() -> dict:
    return {
        "User-Agent": "IttyBittyGamingNews/AdiloUpdater",
        "Accept": "application/json",
        "X-Public-Key": ADILO_PUBLIC_KEY,
        "X-Secret-Key": ADILO_SECRET_KEY,
    }


def fetch_adilo_files() -> list:
    """Fetch a single page of 50 files from the project. No pagination."""
    if not (ADILO_PUBLIC_KEY and ADILO_SECRET_KEY and ADILO_PROJECT_ID):
        print("[ADILO] Missing credentials (PUBLIC_KEY / SECRET_KEY / PROJECT_ID).")
        return []

    url = f"{ADILO_API_BASE}/projects/{ADILO_PROJECT_ID}/files?From=1&To=50"
    print(f"[ADILO] Fetching up to 50 files from project {ADILO_PROJECT_ID}...")

    try:
        r = requests.get(url, headers=adilo_headers(), timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as ex:
        print(f"[ADILO] API request failed: {ex}")
        return []

    payload = (
        data.get("payload") or data.get("data") or
        data.get("files") or (data if isinstance(data, list) else [])
    )

    print(f"[ADILO] Received {len(payload)} file(s).")
    return payload


def log_files(files: list) -> None:
    """Print every file so we can confirm which end is newest."""
    print(f"\n[ADILO] -- File list ({len(files)} total) --")
    for i, f in enumerate(files):
        fid   = f.get("id") or f.get("uuid") or "???"
        name  = f.get("name") or f.get("title") or "(no name)"
        ftype = f.get("type") or ""
        print(f"  [{i:>2}] id={fid:<12}  type={ftype:<10}  name={name}")
    print(f"[ADILO] -----------------------------------\n")


def pick_newest_id(files: list) -> str:
    """
    Pick the newest video ID from the file list.
    Tries LIST_ORDER end first, then the other end as fallback.
    Skips non-video types (folder, image, audio) if type field is present.
    """
    video_files = [
        f for f in files
        if f.get("type", "").lower() not in ("folder", "image", "audio")
    ] or files

    if not video_files:
        return ""

    candidates = (
        [video_files[-1], video_files[0]]
        if LIST_ORDER == "last"
        else [video_files[0], video_files[-1]]
    )

    for candidate in candidates:
        fid = (
            candidate.get("id") or candidate.get("uuid") or
            candidate.get("file_id") or candidate.get("fileId") or ""
        ).strip()
        if fid:
            name = candidate.get("name") or candidate.get("title") or "?"
            print(f"[ADILO] Picked: id={fid}  name={name}  (order='{LIST_ORDER}')")
            return fid

    return ""


# ---------------------------------------------------------------------------
# GITHUB API
# ---------------------------------------------------------------------------

def gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_current_variable() -> str:
    if not GH_PAT or not GH_REPO:
        print("[GH] GH_PAT or GITHUB_REPOSITORY not set.")
        return ""

    url = f"{GH_API_BASE}/repos/{GH_REPO}/actions/variables/{VARIABLE_NAME}"
    try:
        r = requests.get(url, headers=gh_headers(), timeout=15)
        if r.status_code == 404:
            print(f"[GH] {VARIABLE_NAME} does not exist yet -- will create it.")
            return ""
        r.raise_for_status()
        val = r.json().get("value", "").strip()
        print(f"[GH] Current {VARIABLE_NAME} = '{val or '(empty)'}'")
        return val
    except Exception as ex:
        print(f"[GH] Failed to read variable: {ex}")
        return ""


def set_variable(value: str) -> bool:
    if not GH_PAT or not GH_REPO:
        print("[GH] GH_PAT or GITHUB_REPOSITORY not set -- cannot write variable.")
        return False

    url     = f"{GH_API_BASE}/repos/{GH_REPO}/actions/variables/{VARIABLE_NAME}"
    payload = {"name": VARIABLE_NAME, "value": value}

    try:
        r = requests.patch(url, headers=gh_headers(), json=payload, timeout=15)
        if r.status_code == 404:
            create_url = f"{GH_API_BASE}/repos/{GH_REPO}/actions/variables"
            r = requests.post(create_url, headers=gh_headers(), json=payload, timeout=15)

        if r.status_code in (200, 201, 204):
            print(f"[GH] {VARIABLE_NAME} set to: {value}")
            return True

        print(f"[GH] Failed. Status={r.status_code}  Body={r.text[:300]}")
        return False
    except Exception as ex:
        print(f"[GH] Exception: {ex}")
        return False


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 50)
    print("  Adilo Video ID Updater")
    print("=" * 50)

    files = fetch_adilo_files()

    if not files:
        print("[UPDATER] No files returned -- cannot update variable.")
        sys.exit(0)

    log_files(files)

    newest_id = pick_newest_id(files)

    if not newest_id:
        print("[UPDATER] Could not pick a video ID from the file list.")
        sys.exit(1)

    print(f"[UPDATER] Newest video: {ADILO_WATCH_BASE}/{newest_id}")

    current_id = get_current_variable()

    if current_id == newest_id:
        print(f"[UPDATER] Already up to date -- no change needed.")
        sys.exit(0)

    print(f"[UPDATER] Updating: '{current_id or 'none'}' -> '{newest_id}'")
    success = set_variable(newest_id)

    if success:
        print(f"[UPDATER] Done. Digest will use: {ADILO_WATCH_BASE}/{newest_id}")
    else:
        print(f"[UPDATER] Variable update failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
