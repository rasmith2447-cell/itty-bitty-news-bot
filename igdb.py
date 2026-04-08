#!/usr/bin/env python3
"""
igdb_releases.py — Itty Bitty Gaming News
Fetches upcoming game releases from the IGDB API for the next 14 days.
Returns a list of release dicts for use in the email newsletter.
"""

import os
import time
from datetime import datetime, timedelta, timezone

import requests


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


IGDB_CLIENT_ID     = env("IGDB_CLIENT_ID")
IGDB_CLIENT_SECRET = env("IGDB_CLIENT_SECRET")
IGDB_DAYS_AHEAD    = int(env("IGDB_DAYS_AHEAD", "14"))

# Platforms we care about (IGDB platform IDs)
# 6=PC, 48=PS4, 167=PS5, 49=Xbox One, 169=Xbox Series X, 130=Nintendo Switch
TARGET_PLATFORMS = [6, 48, 167, 49, 169, 130]


def get_twitch_token() -> str:
    """Get an OAuth token from Twitch for IGDB access."""
    r = requests.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id":     IGDB_CLIENT_ID,
            "client_secret": IGDB_CLIENT_SECRET,
            "grant_type":    "client_credentials",
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def igdb_query(token: str, endpoint: str, query: str) -> list:
    """Run an IGDB API query."""
    r = requests.post(
        f"https://api.igdb.com/v4/{endpoint}",
        headers={
            "Client-ID":     IGDB_CLIENT_ID,
            "Authorization": f"Bearer {token}",
            "Content-Type":  "text/plain",
        },
        data=query,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def fetch_upcoming_releases(days_ahead: int = 14) -> list:
    """
    Fetch upcoming game releases from IGDB for the next N days.
    Returns a list of dicts with name, date, platforms, cover_url.
    """
    if not IGDB_CLIENT_ID or not IGDB_CLIENT_SECRET:
        print("[IGDB] Credentials not set — skipping releases.")
        return []

    try:
        token = get_twitch_token()
    except Exception as ex:
        print(f"[IGDB] Failed to get token: {ex}")
        return []

    now       = datetime.now(timezone.utc)
    start     = int(now.timestamp())
    end       = int((now + timedelta(days=days_ahead)).timestamp())
    platforms = ",".join(str(p) for p in TARGET_PLATFORMS)

    query = f"""
    fields game.name, game.cover.url, game.genres.name, date, platform.name, platform.id;
    where date >= {start}
      & date <= {end}
      & platform = ({platforms})
      & game.category = 0
      & game.version_parent = null;
    sort date asc;
    limit 50;
    """

    try:
        results = igdb_query(token, "release_dates", query)
    except Exception as ex:
        print(f"[IGDB] Query failed: {ex}")
        return []

    # Deduplicate by game name, keeping earliest release date
    seen = {}
    for item in results:
        game = item.get("game", {})
        if not game:
            continue
        name = game.get("name", "").strip()
        if not name:
            continue

        date_ts  = item.get("date", 0)
        platform = item.get("platform", {}).get("name", "")
        cover    = game.get("cover", {})
        cover_url = ""
        if cover and cover.get("url"):
            # Convert thumbnail to larger image
            cover_url = "https:" + cover["url"].replace("t_thumb", "t_cover_big")

        if name not in seen:
            seen[name] = {
                "name":      name,
                "date":      date_ts,
                "platforms": [platform] if platform else [],
                "cover_url": cover_url,
            }
        else:
            # Add platform if not already listed
            if platform and platform not in seen[name]["platforms"]:
                seen[name]["platforms"].append(platform)
            # Keep earliest date
            if date_ts < seen[name]["date"]:
                seen[name]["date"] = date_ts

    # Sort by date and return top 8
    releases = sorted(seen.values(), key=lambda x: x["date"])[:8]

    # Format date for display
    for r in releases:
        try:
            r["date_str"] = datetime.fromtimestamp(r["date"], tz=timezone.utc).strftime("%B %-d")
        except Exception:
            r["date_str"] = "Coming Soon"

    print(f"[IGDB] Found {len(releases)} upcoming releases.")
    return releases


if __name__ == "__main__":
    releases = fetch_upcoming_releases()
    for r in releases:
        print(f"  {r['date_str']} — {r['name']} ({', '.join(r['platforms'])})")
