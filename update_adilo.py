#!/usr/bin/env python3
"""
update_adilo.py — Itty Bitty Gaming News
Detects the most recently uploaded video in the Adilo project and
updates the GitHub Actions repository variable ADILO_CURRENT_VIDEO_ID.

How it works:
  1. Calls the Adilo API to get all files in the project
  2. Since Adilo only returns ['id', 'name', 'type'] with no date field,
     we fetch ALL pages and log every video so we can inspect the order.
     On the first run this tells us definitively which end is newest.
  3. Compares against ADILO_CURRENT_VIDEO_ID (a readable repo variable)
  4. If different, updates the variable via the GitHub API
  5. digest.py reads ADILO_CURRENT_VIDEO_ID at runtime — fully automated,
     no manual secret editing needed.

Required GitHub secrets:
  ADILO_PUBLIC_KEY, ADILO_SECRET_KEY, ADILO_PROJECT_ID
  GH_PAT  — a GitHub Personal Access Token with repo variable write access
             (Settings > Developer settings > PAT > repo scope)

Required GitHub variables (create once, then auto-managed):
  ADILO_CURRENT_VIDEO_ID — will be created/updated by this script
"""

import os
import sys
import json
import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


ADILO_PUBLIC_KEY  = env("ADILO_PUBLIC_KEY")
ADILO_SECRET_KEY  = env("ADILO_SECRET_KEY")
ADILO_PROJECT_ID  = env("ADILO_PROJECT_ID")
ADILO_API_BASE    = "https://adilo-api.bigcommand.com/v1"
ADILO_WATCH_BASE  = "https://adilo.bigcommand.com/watch"

# GitHub API — needed to update the repo variable
GH_PAT            = env("GH_PAT")           # Personal Access Token
GH_REPO           = env("GITHUB_REPOSITORY") # e.g. "rasmith2447-cell/itty-bitty-news-bot"
GH_API_BASE       = "https://api.github.com"

# The repo variable name we read/write
VARIABLE_NAME     = "ADILO_CURRENT_VIDEO_ID"

# How many files to fetch per page
PAGE_SIZE         = 50

# Fallback known-good video ID (update this whenever you confirm a new one)
FALLBACK_VIDEO_ID = env("ADILO_VIDEO_ID", "9u7iHmrc")


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


def fetch_all_adilo_files() -> list:
    """
    Fetch ALL files from the project across multiple pages.
    Logs every video so we can inspect the order on the first run.
    """
    if not (ADILO_PUBLIC_KEY and ADILO_SECRET_KEY and ADILO_PROJECT_ID):
        print("[ADILO] Missing API credentials — cannot fetch files.")
        return []

    all_files = []
    page_from = 1

    while True:
        page_to = page_from + PAGE_SIZE - 1
        url = f"{ADILO_API_BASE}/projects/{ADILO_PROJECT_ID}/files?From={page_from}&To={page_to}"
        print(f"[ADILO] Fetching files {page_from}–{page_to}...")

        try:
            r = requests.get(url, headers=adilo_headers(), timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception as ex:
            print(f"[ADILO] API request failed: {ex}")
            break

        payload = (
            data.get("payload") or data.get("data") or
            data.get("files") or (data if isinstance(data, list) else [])
        )

        if not payload:
            print(f"[ADILO] No more files returned at page {page_from}.")
            break

        all_files.extend(payload)
        print(f"[ADILO] Got {len(payload)} file(s) this page. Total so far: {len(all_files)}")

        # If we got fewer than PAGE_SIZE, we've hit the last page
        if len(payload) < PAGE_SIZE:
            break

        page_from += PAGE_SIZE

    return all_files


def log_all_files(files: list) -> None:
    """
    Print every file's position, ID, and name so we can
    confirm which end of the list is newest.
    """
    print(f"\n[ADILO] ── Full file list ({len(files)} total) ──")
    for i, f in enumerate(files):
        fid   = f.get("id") or f.get("uuid") or "???"
        name  = f.get("name") or f.get("title") or "(no name)"
        ftype = f.get("type") or ""
        print(f"  [{i:>3}] id={fid}  type={ftype:<12}  name={name}")
    print(f"[ADILO] ────────────────────────────────\n")


def pick_newest_id(files: list) -> str:
    """
    Pick the newest video file ID.

    Since Adilo returns no date field we use position heuristics:
    - Try last item first  (upload order: oldest → newest)
    - Fall back to first item
    - Skip non-video types if 'type' field hints at folders/images

    The full log above will confirm which strategy is correct after
    the first run — update ADILO_LIST_ORDER if needed.
    """
    # Respect env override for list order if we've confirmed it
    # ADILO_LIST_ORDER=first  → payload[0]  is newest
    # ADILO_LIST_ORDER=last   → payload[-1] is newest  (default)
    order = env("ADILO_LIST_ORDER", "last").lower()

    video_files = [
        f for f in files
        if not f.get("type", "").lower() in ("folder", "image", "audio")
    ]
    if not video_files:
        video_files = files  # fallback: try everything

    candidates = (
        [video_files[-1], video_files[0]]
        if order == "last"
        else [video_files[0], video_files[-1]]
    )

    for candidate in candidates:
        fid = (
            candidate.get("id") or candidate.get("uuid") or
            candidate.get("file_id") or candidate.get("fileId") or ""
        ).strip()
        if fid:
            print(f"[ADILO] Picked video ID: {fid}  (name: {candidate.get('name', '?')})")
            return fid

    return ""


# ---------------------------------------------------------------------------
# GITHUB API — read/write repo variables
# ---------------------------------------------------------------------------

def gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_current_variable() -> str:
    """Read the current value of ADILO_CURRENT_VIDEO_ID from repo variables."""
    if not GH_PAT or not GH_REPO:
        print("[GH] GH_PAT or GITHUB_REPOSITORY not set — cannot read variable.")
        return ""

    url = f"{GH_API_BASE}/repos/{GH_REPO}/actions/variables/{VARIABLE_NAME}"
    try:
        r = requests.get(url, headers=gh_headers(), timeout=15)
        if r.status_code == 404:
            print(f"[GH] Variable {VARIABLE_NAME} does not exist yet — will create it.")
            return ""
        r.raise_for_status()
        val = r.json().get("value", "").strip()
        print(f"[GH] Current {VARIABLE_NAME} = {val or '(empty)'}")
        return val
    except Exception as ex:
        print(f"[GH] Failed to read variable: {ex}")
        return ""


def set_variable(value: str) -> bool:
    """Create or update ADILO_CURRENT_VIDEO_ID repo variable."""
    if not GH_PAT or not GH_REPO:
        print("[GH] GH_PAT or GITHUB_REPOSITORY not set — cannot update variable.")
        return False

    # Try PATCH (update) first, then POST (create) if 404
    url     = f"{GH_API_BASE}/repos/{GH_REPO}/actions/variables/{VARIABLE_NAME}"
    payload = {"name": VARIABLE_NAME, "value": value}

    try:
        r = requests.patch(url, headers=gh_headers(), json=payload, timeout=15)
        if r.status_code == 404:
            # Variable doesn't exist yet — create it
            create_url = f"{GH_API_BASE}/repos/{GH_REPO}/actions/variables"
            r = requests.post(create_url, headers=gh_headers(), json=payload, timeout=15)

        if r.status_code in (200, 201, 204):
            print(f"[GH] ✅ {VARIABLE_NAME} updated to: {value}")
            return True
        else:
            print(f"[GH] ❌ Failed to update variable. Status: {r.status_code}  Body: {r.text[:300]}")
            return False
    except Exception as ex:
        print(f"[GH] Exception updating variable: {ex}")
        return False


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 50)
    print("  Adilo Video ID Updater")
    print("=" * 50)

    # 1. Fetch all files
    files = fetch_all_adilo_files()

    if not files:
        print(f"[UPDATER] No files returned. Keeping current value unchanged.")
        sys.exit(0)

    # 2. Log everything so we can confirm ordering
    log_all_files(files)

    # 3. Pick newest
    newest_id = pick_newest_id(files)

    if not newest_id:
        print(f"[UPDATER] Could not determine newest video ID. Exiting.")
        sys.exit(1)

    print(f"[UPDATER] Newest video ID: {newest_id}")
    print(f"[UPDATER] Watch URL: {ADILO_WATCH_BASE}/{newest_id}")

    # 4. Compare with current stored value
    current_id = get_current_variable()

    if current_id == newest_id:
        print(f"[UPDATER] No change — {VARIABLE_NAME} is already up to date.")
        sys.exit(0)

    # 5. Update the repo variable
    print(f"[UPDATER] Change detected: {current_id or '(none)'} → {newest_id}")
    success = set_variable(newest_id)

    if success:
        print(f"[UPDATER] ✅ Done. Next digest will use: {ADILO_WATCH_BASE}/{newest_id}")
    else:
        print(f"[UPDATER] ❌ Variable update failed. digest.py will fall back to cached/hardcoded ID.")
        sys.exit(1)


if __name__ == "__main__":
    main()
