#!/usr/bin/env python3
"""
update_adilo.py — Itty Bitty Gaming News
Detects the most recently uploaded video in the Adilo project and
updates the GitHub Actions repository variable ADILO_CURRENT_VIDEO_ID.

Strategy: fetch from a high page offset to get the newest videos.
The list is oldest-first, so newest videos are at the end.
We fetch the last 50 files by starting at a high offset and working
backwards until we find videos, then pick the last one.
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

# Start fetching from this offset. Set higher than your total video count
# so we land near the end of the list where newest videos are.
# Override with repo variable ADILO_FETCH_FROM if needed.
FETCH_FROM       = int(env("ADILO_FETCH_FROM", "500"))
PAGE_SIZE        = 50


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


def fetch_page(from_idx: int, to_idx: int) -> list:
    """Fetch a single page of files."""
    url = f"{ADILO_API_BASE}/projects/{ADILO_PROJECT_ID}/files?From={from_idx}&To={to_idx}"
    try:
        r = requests.get(url, headers=adilo_headers(), timeout=30)
        r.raise_for_status()
        data = r.json()
        payload = (
            data.get("payload") or data.get("data") or
            data.get("files") or (data if isinstance(data, list) else [])
        )
        return payload
    except Exception as ex:
        print(f"[ADILO] Request failed ({from_idx}-{to_idx}): {ex}")
        return []


def find_newest_video() -> str:
    """
    Walk backwards from FETCH_FROM in 50-file pages until we find
    a non-empty page. The last item on the last non-empty page
    is the newest video.
    """
    if not (ADILO_PUBLIC_KEY and ADILO_SECRET_KEY and ADILO_PROJECT_ID):
        print("[ADILO] Missing credentials.")
        return ""

    # Try pages walking backwards from FETCH_FROM until we find files
    # Start high, step down by PAGE_SIZE each time if page is empty
    from_idx = FETCH_FROM
    last_good_files = []
    attempts = 0
    max_attempts = 10  # safety limit

    while attempts < max_attempts:
        to_idx = from_idx + PAGE_SIZE - 1
        print(f"[ADILO] Trying page {from_idx}–{to_idx}...")
        files = fetch_page(from_idx, to_idx)

        if files:
            print(f"[ADILO] Got {len(files)} file(s).")
            last_good_files = files

            # If we got a full page, there might be more ahead — try next page
            if len(files) == PAGE_SIZE:
                from_idx += PAGE_SIZE
                attempts += 1
                continue
            else:
                # Partial page = this is the last page, stop here
                print(f"[ADILO] Partial page — this is the end of the list.")
                break
        else:
            # Empty page — step back if we haven't found anything yet
            if not last_good_files:
                from_idx = max(1, from_idx - PAGE_SIZE)
                print(f"[ADILO] Empty page — stepping back to {from_idx}.")
                attempts += 1
                continue
            else:
                # We already have a good page, empty means we went too far
                print(f"[ADILO] Empty page after good data — using last good page.")
                break

    if not last_good_files:
        print("[ADILO] Could not find any files. Falling back to page 1.")
        last_good_files = fetch_page(1, PAGE_SIZE)

    if not last_good_files:
        return ""

    # Log the files we found
    print(f"\n[ADILO] -- Final page ({len(last_good_files)} files) --")
    for i, f in enumerate(last_good_files):
        fid   = f.get("id") or f.get("uuid") or "???"
        name  = f.get("name") or f.get("title") or "(no name)"
        ftype = f.get("type") or ""
        print(f"  [{i:>2}] id={fid:<12}  type={ftype:<10}  name={name}")
    print(f"[ADILO] ----------------------------------\n")

    # Newest = last item on the last page (oldest-first ordering confirmed)
    video_files = [
        f for f in last_good_files
        if f.get("type", "").lower() not in ("folder", "image", "audio")
    ] or last_good_files

    candidate = video_files[-1]
    fid = (
        candidate.get("id") or candidate.get("uuid") or
        candidate.get("file_id") or candidate.get("fileId") or ""
    ).strip()

    if fid:
        name = candidate.get("name") or candidate.get("title") or "?"
        print(f"[ADILO] Picked newest: id={fid}  name={name}")

    return fid


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
            print(f"[GH] {VARIABLE_NAME} does not exist yet — will create it.")
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
        print("[GH] GH_PAT or GITHUB_REPOSITORY not set.")
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
    print(f"  Starting search from position {FETCH_FROM}")
    print("=" * 50)

    newest_id = find_newest_video()

    if not newest_id:
        print("[UPDATER] Could not determine newest video ID.")
        sys.exit(1)

    print(f"[UPDATER] Newest video URL: {ADILO_WATCH_BASE}/{newest_id}")

    current_id = get_current_variable()

    if current_id == newest_id:
        print(f"[UPDATER] Already up to date — no change needed.")
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
