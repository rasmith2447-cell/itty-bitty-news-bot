#!/usr/bin/env python3
"""
onlysocial_post.py — Itty Bitty Gaming News
Reads the digest summary written by digest.py and posts it to all
connected social platforms via the OnlySocial API.

Platforms: Bluesky, TikTok, Facebook Page, Instagram
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()

BASE               = "https://app.onlysocial.io/os/api"
TOKEN              = env("ONLYSOCIAL_TOKEN")
WORKSPACE_UUID     = env("ONLYSOCIAL_WORKSPACE_UUID")
DIGEST_EXPORT_FILE = env("DIGEST_EXPORT_FILE", "digest_latest.json")
YOUTUBE_URL        = env("YOUTUBE_URL", "https://www.youtube.com/@smitty-2447")
MAX_HASHTAGS       = int(env("ONLYSOCIAL_MAX_HASHTAGS", "8"))
BLUESKY_CHAR_LIMIT = 300

# Only post to these specific accounts by username (lowercase)
TARGET_USERNAMES = {"smitty2447", "ryanandrewsmith247"}

# Only these providers
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
        json=payload,
        timeout=30,
    )
    if not r.ok:
        print(f"[ONLYSOCIAL] HTTP {r.status_code}: {r.text[:500]}")
    r.raise_for_status()
    return r.json()


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
    """Return only IBGN accounts on target platforms."""
    targeted = []
    for acc in accounts:
        provider = (acc.get("provider") or "").lower()
        username = (acc.get("username") or "").lower().replace("@", "")
        name     = (acc.get("name") or "").lower()

        if provider not in TARGET_PROVIDERS:
            continue

        # Match by username OR name containing "smitty" or "itty bitty"
        is_ibgn = (
            username in TARGET_USERNAMES
            or "smitty" in username
            or "smitty" in name
            or "itty bitty" in name
            or "ittybittygaming" in username
        )

        if is_ibgn:
            targeted.append(acc)
            print(f"[ONLYSOCIAL] Targeting: {acc.get('name')} (@{acc.get('username')}) [{provider}] id={acc.get('id')}")
        else:
            print(f"[ONLYSOCIAL] Skipping:  {acc.get('name')} (@{acc.get('username')}) [{provider}]")

    return targeted


# ---------------------------------------------------------------------------
# HASHTAG GENERATION
# ---------------------------------------------------------------------------

HASHTAG_STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "has", "have", "had", "will", "would", "could", "should", "may", "might",
    "its", "it", "this", "that", "not", "no", "new", "gets", "get", "got",
    "out", "up", "now", "over", "back", "just", "more", "than", "about",
    "into", "after", "as", "says", "amid", "coming", "goes", "hit", "hits",
    "reveals", "announces", "announced", "launches", "launched", "releasing",
    "released", "confirms", "confirmed", "update", "updates", "adds", "added",
    "leaves", "reveal", "why", "did", "how", "what", "when", "where", "who",
    "despite", "through", "make", "made", "push", "pushed", "they", "their",
    "its", "our", "your", "all", "day", "days", "also", "even", "still",
    "already", "never", "ever", "both", "between", "too", "very", "can",
    "cannot", "won", "won't", "can't", "don't", "isn't", "aren't",
    "talking", "point", "style", "fittest", "survival", "initial",
    "arrogance", "stupidity", "investors", "perfect", "mysterious",
}

# Multi-word phrases to extract as single hashtags (checked before word-by-word)
PHRASE_HASHTAGS = {
    "silent bob": "SilentBob",
    "jay and silent": "JayAndSilentBob",
    "mario day": "MarioDay",
    "game pass": "GamePass",
    "game awards": "GameAwards",
    "ps5": "PS5",
    "ps4": "PS4",
    "xbox series": "XboxSeries",
    "game boy": "GameBoy",
    "grand theft auto": "GTA",
    "gta 6": "GTA6",
    "gta vi": "GTA6",
    "steam deck": "SteamDeck",
    "ghost of": "GhostOf",
}

# Single-word known mappings
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
    "yoshi": "Yoshi",
    "pokemon": "Pokemon",
    "halo": "Halo",
    "overwatch": "Overwatch",
    "steam": "Steam",
    "epic": "EpicGames",
    "microsoft": "Microsoft",
    "sony": "Sony",
    "capcom": "Capcom",
    "ubisoft": "Ubisoft",
    "activision": "Activision",
    "blizzard": "Blizzard",
    "rockstar": "Rockstar",
    "bethesda": "Bethesda",
    "bungie": "Bungie",
    "tiktok": "TikTok",
    "instagram": "Instagram",
    "youtube": "YouTube",
    "twitch": "Twitch",
    "discord": "Discord",
    "bluesky": "Bluesky",
    "vr": "VR",
    "ai": "AI",
    "indie": "IndieGames",
    "esports": "Esports",
    "dispatch": "Dispatch",
    "avalanche": "AvalancheStudios",
    "highlander": "Highlander",
    "megaman": "MegaMan",
    "capcom": "Capcom",
}

# Words that are too generic to be useful hashtags even if long enough
GENERIC_WORDS = {
    "game", "games", "gaming", "news", "release", "date", "announced",
    "studio", "studios", "developer", "developers", "players", "player",
    "update", "version", "series", "story", "stories", "content",
    "features", "feature", "trailer", "reveal", "launch", "report",
    "says", "confirms", "according", "following", "including", "title",
}


def title_to_hashtags(titles: list) -> list:
    """Extract smart hashtags from story titles."""
    seen = set()
    tags = []

    combined = " ".join(titles).lower()

    # Check multi-word phrases first
    for phrase, hashtag in PHRASE_HASHTAGS.items():
        if phrase in combined and hashtag not in seen:
            seen.add(hashtag)
            tags.append(hashtag)

    # Then word-by-word
    for title in titles:
        words = re.findall(r"[A-Za-z0-9]+", title)
        i = 0
        while i < len(words):
            word = words[i]
            lower = word.lower()

            # Skip stopwords, generic words, short words
            if lower in HASHTAG_STOPWORDS or lower in GENERIC_WORDS or len(lower) < 4:
                i += 1
                continue

            # Check known hashtags
            if lower in KNOWN_HASHTAGS:
                tag = KNOWN_HASHTAGS[lower]
            else:
                # Only use as hashtag if it looks like a proper noun (starts uppercase)
                # or is a known game/brand term
                if word[0].isupper() and len(word) >= 4:
                    tag = word
                else:
                    i += 1
                    continue

            if tag not in seen:
                seen.add(tag)
                tags.append(tag)

            if len(tags) >= MAX_HASHTAGS - 1:
                break
            i += 1

        if len(tags) >= MAX_HASHTAGS - 1:
            break

    # Always append brand hashtag last
    tags.append("IttyBittyGamingNews")
    return [f"#{t}" for t in tags]


# ---------------------------------------------------------------------------
# POST CONTENT BUILDER
# ---------------------------------------------------------------------------

def build_post_content(stories: list) -> str:
    icons = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    today = datetime.now(timezone.utc).strftime("%B %-d")

    lines = [f"🎮 Itty Bitty Gaming News — {today}", ""]

    titles = []
    for i, story in enumerate(stories[:5]):
        icon  = icons[i] if i < len(icons) else f"{i+1}."
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
    """Trim to fit Bluesky's 300 char limit."""
    if len(full_content) <= BLUESKY_CHAR_LIMIT:
        return full_content

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

def create_and_post(workspace: str, accounts: list, full_content: str, bluesky_content: str) -> None:
    """
    OnlySocial API: create post with one version per account,
    then immediately schedule with postNow=true.
    """
    account_uuids = [acc.get("uuid") for acc in accounts if acc.get("uuid")]

    # Build one version per account to handle per-platform content differences
    versions = []
    is_first = True
    for acc in accounts:
        provider = (acc.get("provider") or "").lower()
        acc_id   = acc.get("id", 0)
        body     = bluesky_content if provider == "bluesky" else full_content

        version = {
            "account_id": acc_id,
            "is_original": is_first,
            "content": [{"body": body, "media": [], "url": ""}],
            "options": {},
        }

        if provider == "instagram":
            version["options"]["instagram"] = {"type": "post", "collaborators": []}
        elif provider == "facebook_page":
            version["options"]["facebook_page"] = {"type": "post"}
        elif provider == "bluesky":
            version["options"]["blue_sky"] = {"tags": []}
        elif provider == "tiktok":
            version["options"]["tiktok"] = {
                "privacy_level":      {f"account-{acc_id}": "PUBLIC_TO_EVERYONE"},
                "allow_comments":     {f"account-{acc_id}": True},
                "allow_duet":         {f"account-{acc_id}": False},
                "allow_stitch":       {f"account-{acc_id}": False},
                "content_disclosure": {f"account-{acc_id}": False},
                "brand_organic_toggle": {f"account-{acc_id}": False},
                "brand_content_toggle": {f"account-{acc_id}": False},
            }

        versions.append(version)
        is_first = False

    payload = {
        "accounts":              account_uuids,
        "versions":              versions,
        "tags":                  [],
        "date":                  None,
        "time":                  "",
        "until_date":            None,
        "until_time":            "",
        "repeat_frequency":      None,
        "short_link_provider":   None,
        "short_link_provider_id": None,
    }

    print("[ONLYSOCIAL] Creating post...")
    created = api_post(f"/{workspace}/posts", payload)
    print(f"[ONLYSOCIAL] Create response: {json.dumps(created, indent=2)}")

    # Find post UUID
    post_uuid = None
    if isinstance(created, dict):
        for key in ("uuid", "id", "postUuid", "post_uuid"):
            if key in created and created[key]:
                post_uuid = str(created[key])
                break
        if not post_uuid:
            for key in ("data", "payload", "post"):
                if key in created and isinstance(created[key], dict):
                    for k2 in ("uuid", "id", "postUuid", "post_uuid"):
                        if created[key].get(k2):
                            post_uuid = str(created[key][k2])
                            break

    if not post_uuid:
        print("[ONLYSOCIAL] Could not find post UUID in response.")
        sys.exit(1)

    print(f"[ONLYSOCIAL] Post UUID: {post_uuid} — scheduling now...")
    scheduled = api_post(f"/{workspace}/posts/schedule/{post_uuid}", {"postNow": True})
    print(f"[ONLYSOCIAL] Scheduled: {json.dumps(scheduled, indent=2)}")
    print("[ONLYSOCIAL] Done! Post sent to all platforms.")


# ---------------------------------------------------------------------------
# DIGEST LOADER
# ---------------------------------------------------------------------------

def load_digest_stories() -> list:
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

    if not WORKSPACE_UUID:
        print("[ONLYSOCIAL] ONLYSOCIAL_WORKSPACE_UUID is not set.")
        print("[ONLYSOCIAL] Log into OnlySocial, copy the UUID from your browser URL,")
        print("[ONLYSOCIAL] and save it as repo secret ONLYSOCIAL_WORKSPACE_UUID.")
        sys.exit(1)

    print(f"[ONLYSOCIAL] Using workspace: {WORKSPACE_UUID}")

    # List and filter accounts
    print("[ONLYSOCIAL] Fetching connected accounts...")
    try:
        all_accounts = list_accounts(WORKSPACE_UUID)
    except Exception as ex:
        print(f"[ONLYSOCIAL] Failed to list accounts: {ex}")
        sys.exit(1)

    print(f"[ONLYSOCIAL] Total accounts: {len(all_accounts)}")
    targeted = filter_target_accounts(all_accounts)

    if not targeted:
        print("[ONLYSOCIAL] No matching IBGN accounts found.")
        sys.exit(1)

    # Load stories
    stories = load_digest_stories()
    print(f"[ONLYSOCIAL] Loaded {len(stories)} stories.")

    # Build content
    full_content    = build_post_content(stories)
    bluesky_content = build_bluesky_content(full_content)

    print(f"\n[ONLYSOCIAL] Post content ({len(full_content)} chars):")
    print("-" * 40)
    print(full_content)
    print("-" * 40)

    if bluesky_content != full_content:
        print(f"\n[ONLYSOCIAL] Bluesky version ({len(bluesky_content)} chars):")
        print(bluesky_content)
        print("-" * 40)

    # Create and post
    try:
        create_and_post(WORKSPACE_UUID, targeted, full_content, bluesky_content)
    except Exception as ex:
        print(f"[ONLYSOCIAL] Failed: {ex}")
        sys.exit(1)


if __name__ == "__main__":
    main()
