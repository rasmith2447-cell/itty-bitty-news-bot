#!/usr/bin/env python3
"""
onlysocial_post.py — Itty Bitty Gaming News
Reads the digest summary written by digest.py and posts it to all
connected social platforms via the OnlySocial API.

Platforms: Bluesky, TikTok, Facebook Pages, Instagram
Flow: discover workspace → list accounts → create post → post now
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()

BASE                 = "https://app.onlysocial.io/os/api"
TOKEN                = env("ONLYSOCIAL_TOKEN")
WORKSPACE_UUID       = env("ONLYSOCIAL_WORKSPACE_UUID")   # set after first discovery run
DIGEST_EXPORT_FILE   = env("DIGEST_EXPORT_FILE", "digest_latest.json")
YOUTUBE_URL          = env("YOUTUBE_URL", "https://www.youtube.com/@IttyBittyGamingNews")
MAX_HASHTAGS         = int(env("ONLYSOCIAL_MAX_HASHTAGS", "8"))
BLUESKY_CHAR_LIMIT   = 300

# Providers we want to post to (OnlySocial provider names)
TARGET_PROVIDERS = {"bluesky", "tiktok", "facebook_page", "instagram"}

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def headers() -> dict:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "IttyBittyGamingNews/OnlySocial",
    }


def api_get(path: str) -> dict:
    r = requests.get(f"{BASE}{path}", headers=headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def api_post(path: str, payload: dict) -> dict:
    r = requests.post(
        f"{BASE}{path}",
        headers=headers(),
        data=json.dumps(payload),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# WORKSPACE DISCOVERY
# ---------------------------------------------------------------------------

def discover_workspace_uuid() -> str:
    """
    OnlySocial doesn't have a /workspaces endpoint, but the workspace UUID
    is embedded in the URL when you're logged in. We try a few known patterns
    to find it by probing the accounts endpoint with common UUID formats.

    If this fails, the user needs to find it manually in their browser URL
    while logged into OnlySocial (it appears as /os/{uuid}/ in the URL).
    """
    print("[ONLYSOCIAL] No workspace UUID set — attempting discovery...")
    print("[ONLYSOCIAL] Log into OnlySocial in your browser.")
    print("[ONLYSOCIAL] Look at the URL — it will contain your workspace UUID.")
    print("[ONLYSOCIAL] Example: https://app.onlysocial.io/os/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx/dashboard")
    print("[ONLYSOCIAL] Copy that UUID and save it as repo secret ONLYSOCIAL_WORKSPACE_UUID")
    return ""


# ---------------------------------------------------------------------------
# ACCOUNT LISTING
# ---------------------------------------------------------------------------

def list_accounts(workspace: str) -> list:
    data = api_get(f"/{workspace}/accounts")
    if isinstance(data, dict):
        for key in ("data", "accounts", "payload", "result"):
            if key in data and isinstance(data[key], list):
                return data[key]
    if isinstance(data, list):
        return data
    return []


def filter_target_accounts(accounts: list) -> list:
    """Return only accounts for platforms we want to post to."""
    targeted = []
    for acc in accounts:
        provider = (acc.get("provider") or "").lower()
        if provider in TARGET_PROVIDERS:
            targeted.append(acc)
            print(f"[ONLYSOCIAL] Found account: {acc.get('name')} (@{acc.get('username')}) [{provider}]")
    return targeted


# ---------------------------------------------------------------------------
# HASHTAG GENERATION
# ---------------------------------------------------------------------------

# Words to skip when generating hashtags
HASHTAG_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "has", "have", "had", "will", "would", "could", "should", "may", "might",
    "its", "it", "this", "that", "not", "no", "new", "gets", "get", "got",
    "out", "up", "now", "over", "back", "just", "more", "than", "about",
    "into", "after", "as", "says", "amid", "amid", "amid", "amid",
    "coming", "goes", "hit", "hits", "reveals", "announces", "announced",
    "launches", "launched", "releasing", "released", "confirms", "confirmed",
    "update", "updates", "adds", "added", "leaves", "reveal",
}

# Known game/brand names → clean hashtag form
KNOWN_HASHTAGS = {
    "gta": "GTA",
    "gta6": "GTA6",
    "ps5": "PS5",
    "ps4": "PS4",
    "xbox": "Xbox",
    "nintendo": "Nintendo",
    "playstation": "PlayStation",
    "fortnite": "Fortnite",
    "minecraft": "Minecraft",
    "zelda": "Zelda",
    "mario": "Mario",
    "pokemon": "Pokemon",
    "halo": "Halo",
    "cod": "COD",
    "overwatch": "Overwatch",
    "steam": "Steam",
    "epic": "EpicGames",
    "microsoft": "Microsoft",
    "sony": "Sony",
    "capcom": "Capcom",
    "ubisoft": "Ubisoft",
    "ea": "EA",
    "activision": "Activision",
    "blizzard": "Blizzard",
    "rockstar": "Rockstar",
    "bethesda": "Bethesda",
    "bungie": "Bungie",
    "youtube": "YouTube",
    "twitch": "Twitch",
    "discord": "Discord",
    "pc": "PCGaming",
    "vr": "VR",
    "ai": "AI",
}


def title_to_hashtags(titles: list) -> list:
    """Extract dynamic hashtags from story titles."""
    seen = set()
    tags = []

    for title in titles:
        # Strip punctuation, split words
        words = re.findall(r"[A-Za-z0-9]+", title)
        for word in words:
            lower = word.lower()
            if lower in HASHTAG_STOPWORDS:
                continue
            if len(lower) < 3:
                continue

            # Check known hashtags first
            if lower in KNOWN_HASHTAGS:
                tag = KNOWN_HASHTAGS[lower]
            elif len(word) >= 4:
                # Title-case the word for use as hashtag
                tag = word.capitalize()
            else:
                continue

            if tag not in seen:
                seen.add(tag)
                tags.append(tag)

            if len(tags) >= MAX_HASHTAGS - 1:  # leave room for #IttyBittyGamingNews
                break
        if len(tags) >= MAX_HASHTAGS - 1:
            break

    # Always include brand hashtag
    brand = "IttyBittyGamingNews"
    if brand not in seen:
        tags.append(brand)

    return [f"#{t}" for t in tags]


# ---------------------------------------------------------------------------
# POST CONTENT BUILDER
# ---------------------------------------------------------------------------

def build_post_content(stories: list) -> str:
    """
    Build the social post text from story list.
    Format:
      🎮 Itty Bitty Gaming News — March 10

      🥇 Story one headline
      🥈 Story two headline
      🥉 Story three headline
      4️⃣ Story four headline
      5️⃣ Story five headline

      🎬 Watch daily: https://youtube.com/@IttyBittyGamingNews

      #Hashtag1 #Hashtag2 #IttyBittyGamingNews
    """
    icons = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    today = datetime.now(timezone.utc).strftime("%B %-d")  # e.g. "March 10"

    lines = [f"🎮 Itty Bitty Gaming News — {today}", ""]

    titles = []
    for i, story in enumerate(stories[:5]):
        icon = icons[i] if i < len(icons) else f"{i+1}."
        title = story.get("title", "").strip()
        titles.append(title)
        lines.append(f"{icon} {title}")

    lines.append("")
    lines.append(f"🎬 Watch daily: {YOUTUBE_URL}")
    lines.append("")

    hashtags = title_to_hashtags(titles)
    lines.append(" ".join(hashtags))

    return "\n".join(lines)


def build_bluesky_content(full_content: str) -> str:
    """Bluesky has a 300 char limit — trim if needed."""
    if len(full_content) <= BLUESKY_CHAR_LIMIT:
        return full_content

    # Trim to fit — keep header + as many stories as fit + YouTube link
    lines = full_content.split("\n")
    trimmed = []
    for line in lines:
        candidate = "\n".join(trimmed + [line])
        if len(candidate) > BLUESKY_CHAR_LIMIT - 5:
            break
        trimmed.append(line)

    return "\n".join(trimmed).strip()


# ---------------------------------------------------------------------------
# POST CREATION
# ---------------------------------------------------------------------------

def build_versions(accounts: list, full_content: str, bluesky_content: str) -> list:
    """
    OnlySocial uses a 'versions' array. The first version with is_original=true
    is the base post. Each account can optionally have its own version.
    We'll use one version per account to handle Bluesky's char limit separately.
    """
    versions = []
    is_first = True

    for acc in accounts:
        provider = (acc.get("provider") or "").lower()
        acc_id   = acc.get("id", 0)

        content_body = bluesky_content if provider == "bluesky" else full_content

        version = {
            "account_id": acc_id,
            "is_original": is_first,
            "content": [
                {
                    "body": content_body,
                    "media": [],
                    "url": YOUTUBE_URL,
                }
            ],
            "options": {},
        }

        # Platform-specific options
        if provider == "instagram":
            version["options"]["instagram"] = {"type": "post", "collaborators": []}
        elif provider == "facebook_page":
            version["options"]["facebook_page"] = {"type": "post"}
        elif provider == "bluesky":
            version["options"]["blue_sky"] = {"tags": []}
        elif provider == "tiktok":
            version["options"]["tiktok"] = {
                "privacy_level": {f"account-{acc_id}": "PUBLIC_TO_EVERYONE"},
                "allow_comments": {f"account-{acc_id}": True},
                "allow_duet": {f"account-{acc_id}": False},
                "allow_stitch": {f"account-{acc_id}": False},
                "content_disclosure": {f"account-{acc_id}": False},
                "brand_organic_toggle": {f"account-{acc_id}": False},
                "brand_content_toggle": {f"account-{acc_id}": False},
            }

        versions.append(version)
        is_first = False

    return versions


def create_post(workspace: str, accounts: list, full_content: str, bluesky_content: str) -> dict:
    account_uuids = [acc.get("uuid") for acc in accounts if acc.get("uuid")]
    versions      = build_versions(accounts, full_content, bluesky_content)

    payload = {
        "accounts": account_uuids,
        "versions": versions,
        "tags": [],
        "date": None,
        "time": "",
        "repeat_frequency": None,
        "short_link_provider": None,
        "short_link_provider_id": None,
    }

    return api_post(f"/{workspace}/posts", payload)


def post_now(workspace: str, post_uuid: str) -> dict:
    return api_post(f"/{workspace}/posts/schedule/{post_uuid}", {"postNow": True})


# ---------------------------------------------------------------------------
# DIGEST LOADER
# ---------------------------------------------------------------------------

def load_digest_stories() -> list:
    """
    Load stories from digest_latest.json written by digest.py.
    Falls back to a minimal placeholder if the file doesn't exist.
    """
    if os.path.exists(DIGEST_EXPORT_FILE):
        try:
            with open(DIGEST_EXPORT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
                if isinstance(data, dict) and "stories" in data:
                    return data["stories"]
        except Exception as ex:
            print(f"[ONLYSOCIAL] Could not read {DIGEST_EXPORT_FILE}: {ex}")

    print(f"[ONLYSOCIAL] {DIGEST_EXPORT_FILE} not found — using placeholder.")
    return [{"title": "Itty Bitty Gaming News is live! Check out today's top gaming stories."}]


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    if not TOKEN:
        print("[ONLYSOCIAL] ONLYSOCIAL_TOKEN is not set. Skipping.")
        sys.exit(0)

    workspace = WORKSPACE_UUID
    if not workspace:
        discover_workspace_uuid()
        sys.exit(1)

    print(f"[ONLYSOCIAL] Using workspace: {workspace}")

    # List and filter accounts
    print("[ONLYSOCIAL] Fetching connected accounts...")
    try:
        all_accounts = list_accounts(workspace)
    except Exception as ex:
        print(f"[ONLYSOCIAL] Failed to list accounts: {ex}")
        sys.exit(1)

    print(f"[ONLYSOCIAL] Total accounts found: {len(all_accounts)}")
    targeted = filter_target_accounts(all_accounts)

    if not targeted:
        print("[ONLYSOCIAL] No matching accounts found for target providers.")
        print(f"[ONLYSOCIAL] Target providers: {TARGET_PROVIDERS}")
        print("[ONLYSOCIAL] All accounts:")
        for acc in all_accounts:
            print(f"  - {acc.get('name')} [{acc.get('provider')}] uuid={acc.get('uuid')}")
        sys.exit(1)

    # Load stories
    stories = load_digest_stories()
    print(f"[ONLYSOCIAL] Loaded {len(stories)} stories from digest.")

    # Build content
    full_content     = build_post_content(stories)
    bluesky_content  = build_bluesky_content(full_content)

    print(f"\n[ONLYSOCIAL] Post content ({len(full_content)} chars):")
    print("-" * 40)
    print(full_content)
    print("-" * 40)

    if bluesky_content != full_content:
        print(f"\n[ONLYSOCIAL] Bluesky version ({len(bluesky_content)} chars):")
        print(bluesky_content)
        print("-" * 40)

    # Create post
    print("\n[ONLYSOCIAL] Creating post...")
    try:
        created = create_post(workspace, targeted, full_content, bluesky_content)
    except Exception as ex:
        print(f"[ONLYSOCIAL] Failed to create post: {ex}")
        sys.exit(1)

    print(f"[ONLYSOCIAL] Create response: {json.dumps(created, indent=2)}")

    # Find post UUID
    post_uuid = None
    if isinstance(created, dict):
        for key in ("uuid", "id", "postUuid", "post_uuid"):
            if key in created and isinstance(created[key], str):
                post_uuid = created[key]
                break
        if not post_uuid:
            for key in ("data", "payload", "post"):
                if key in created and isinstance(created[key], dict):
                    for k2 in ("uuid", "id", "postUuid", "post_uuid"):
                        if k2 in created[key]:
                            post_uuid = str(created[key][k2])
                            break

    if not post_uuid:
        print("[ONLYSOCIAL] Could not find post UUID in response. Cannot schedule.")
        sys.exit(1)

    print(f"[ONLYSOCIAL] Post UUID: {post_uuid}")

    # Post now
    print("[ONLYSOCIAL] Scheduling post now...")
    try:
        scheduled = post_now(workspace, post_uuid)
        print(f"[ONLYSOCIAL] Scheduled: {json.dumps(scheduled, indent=2)}")
        print("[ONLYSOCIAL] Done! Post sent to all platforms.")
    except Exception as ex:
        print(f"[ONLYSOCIAL] Failed to schedule post: {ex}")
        sys.exit(1)


if __name__ == "__main__":
    main()
