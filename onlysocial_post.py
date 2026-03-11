#!/usr/bin/env python3
"""
onlysocial_post.py — Itty Bitty Gaming News
Reads the digest summary written by digest.py and posts it to all
connected social platforms via the OnlySocial API.

Platforms: Bluesky, Facebook Page, LinkedIn
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

# Only these providers (text-only platforms)
TARGET_PROVIDERS = {"blue_sky", "facebook_page", "linkedin", "linkedin_page", "threads"}

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

        # For LinkedIn, Bluesky, and Threads include all connected accounts
        if provider in ("linkedin", "linkedin_page", "blue_sky", "threads"):
            targeted.append(acc)
            print(f"[ONLYSOCIAL] Targeting: {acc.get('name')} (@{acc.get('username')}) [{provider}] id={acc.get('id')}")
            continue

        # For Facebook, filter to IBGN-related accounts only
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

# Single-word known mappings — ONLY these will ever become hashtags
KNOWN_HASHTAGS = {
    # Platforms
    "playstation": "PlayStation",
    "xbox": "Xbox",
    "nintendo": "Nintendo",
    "switch": "NintendoSwitch",
    "steam": "Steam",
    "pc": "PCGaming",
    "vr": "VR",
    "ps5": "PS5",
    "ps4": "PS4",
    # Publishers / Studios
    "sony": "Sony",
    "microsoft": "Microsoft",
    "activision": "Activision",
    "blizzard": "Blizzard",
    "ubisoft": "Ubisoft",
    "capcom": "Capcom",
    "ea": "EA",
    "rockstar": "Rockstar",
    "bethesda": "Bethesda",
    "bungie": "Bungie",
    "epic": "EpicGames",
    "valve": "Valve",
    "sega": "Sega",
    "bandai": "BandaiNamco",
    "namco": "BandaiNamco",
    "konami": "Konami",
    "2k": "2KGames",
    "naughtydog": "NaughtyDog",
    "insomniac": "InsomniacGames",
    "fromsoftware": "FromSoftware",
    # Games / Franchises
    "fortnite": "Fortnite",
    "minecraft": "Minecraft",
    "zelda": "Zelda",
    "mario": "Mario",
    "pokemon": "Pokemon",
    "halo": "Halo",
    "overwatch": "Overwatch",
    "cod": "COD",
    "callofduty": "CallOfDuty",
    "gta": "GTA",
    "gta6": "GTA6",
    "cyberpunk": "Cyberpunk",
    "elden": "EldenRing",
    "diablo": "Diablo",
    "starfield": "Starfield",
    "palworld": "Palworld",
    "baldur": "BaldursGate",
    "hogwarts": "HogwartsLegacy",
    "spiderman": "SpiderMan",
    "godofwar": "GodOfWar",
    "horizon": "Horizon",
    "assassin": "AssassinsCreed",
    "resident": "ResidentEvil",
    "finalfantasy": "FinalFantasy",
    "streetfighter": "StreetFighter",
    "mortal": "MortalKombat",
    "tekken": "Tekken",
    "persona": "Persona",
    "metroid": "Metroid",
    "kirby": "Kirby",
    "donkey": "DonkeyKong",
    "splatoon": "Splatoon",
    "smash": "SmashBros",
    "animal": "AnimalCrossing",
    "stardew": "StardewValley",
    "harvest": "HarvestMoon",
    "solasta": "Solasta",
    "dispatch": "Dispatch",
    "pickmon": "Pickmon",
    # Tech / Industry
    "ai": "AI",
    "esports": "Esports",
    "indie": "IndieGames",
    "twitch": "Twitch",
    "youtube": "YouTube",
    "discord": "Discord",
    "tiktok": "TikTok",
    "bluesky": "Bluesky",
    "gamepass": "GamePass",
    "gameawards": "GameAwards",
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
    """
    Only generate hashtags from known game/brand names.
    Never guess from random words in titles.
    """
    seen = set()
    tags = []

    combined = " ".join(titles).lower()

    # Check multi-word phrases first
    for phrase, hashtag in PHRASE_HASHTAGS.items():
        if phrase in combined and hashtag not in seen:
            seen.add(hashtag)
            tags.append(hashtag)
            if len(tags) >= MAX_HASHTAGS - 1:
                break

    # Then check individual words against known hashtags only
    if len(tags) < MAX_HASHTAGS - 1:
        words = re.findall(r"[a-z0-9]+", combined)
        for word in words:
            if word in KNOWN_HASHTAGS and KNOWN_HASHTAGS[word] not in seen:
                tag = KNOWN_HASHTAGS[word]
                seen.add(tag)
                tags.append(tag)
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
        url   = story.get("url", "").strip()
        titles.append(title)
        if url:
            lines.append(f"{icon} {title}")
            lines.append(f"   {url}")
        else:
            lines.append(f"{icon} {title}")

    lines.append("")
    lines.append(f"🎬 Watch daily: {YOUTUBE_URL}")
    lines.append("")

    hashtags = title_to_hashtags(titles)
    lines.append(" ".join(hashtags))

    return "\n".join(lines)


def build_bluesky_content(stories: list, hashtags: list) -> str:
    """
    Bluesky hard limit: 300 chars.
    Format: header + top 3 headlines only (no URLs) + brand hashtag.
    """
    today = datetime.now(timezone.utc).strftime("%B %-d")
    icons = ["🥇", "🥈", "🥉"]
    lines = [f"🎮 Itty Bitty Gaming News — {today}", ""]
    for i, story in enumerate(stories[:3]):
        title = story.get("title", "").strip()
        # Truncate long titles
        if len(title) > 80:
            title = title[:77] + "..."
        lines.append(f"{icons[i]} {title}")
    lines.append("")
    lines.append("#IttyBittyGamingNews")
    content = "\n".join(lines)
    # Hard trim as safety net
    if len(content) > 295:
        content = content[:292] + "..."
    return content


def build_threads_content(stories: list, hashtags: list) -> str:
    """
    Threads limit: 500 chars.
    Format: header + top 3 headlines (no URLs) + YouTube link + hashtags.
    """
    today = datetime.now(timezone.utc).strftime("%B %-d")
    icons = ["🥇", "🥈", "🥉"]
    lines = [f"🎮 Itty Bitty Gaming News — {today}", ""]
    for i, story in enumerate(stories[:3]):
        title = story.get("title", "").strip()
        if len(title) > 80:
            title = title[:77] + "..."
        lines.append(f"{icons[i]} {title}")
    lines.append("")
    lines.append(f"🎬 Watch daily: {YOUTUBE_URL}")
    lines.append("")
    lines.append("#IttyBittyGamingNews")
    content = "\n".join(lines)
    if len(content) > 495:
        content = content[:492] + "..."
    return content



# ---------------------------------------------------------------------------
# POST CREATION
# ---------------------------------------------------------------------------

def create_and_post(workspace: str, accounts: list, full_content: str, bluesky_content: str, threads_content: str) -> None:
    """
    OnlySocial API: create post with one version per account,
    then immediately schedule with postNow=true.
    """
    # API requires integer IDs in the accounts array, not UUIDs
    account_ids = [acc.get("id") for acc in accounts if acc.get("id")]

    # Build one version per account to handle per-platform content differences
    versions = []
    is_first = True
    for acc in accounts:
        provider = (acc.get("provider") or "").lower()
        acc_id   = acc.get("id", 0)
        if provider == "blue_sky":
            body = bluesky_content
        elif provider == "threads":
            body = threads_content
        else:
            body = full_content

        version = {
            "account_id": acc_id,
            "is_original": is_first,
            "content": [{"body": body, "media": [], "url": ""}],
            "options": {},
        }

        if provider == "facebook_page":
            version["options"]["facebook_page"] = {"type": "post"}
        elif provider == "blue_sky":
            version["options"]["blue_sky"] = {"tags": []}
        elif provider in ("linkedin", "linkedin_page"):
            version["options"]["linkedin"] = {"visibility": "PUBLIC", "document": None, "document_title": None}
        elif provider == "threads":
            version["options"]["threads"] = {"type": "post"}

        versions.append(version)
        is_first = False

    payload = {
        "accounts":              account_ids,
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
    for acc in all_accounts:
        print(f"[ONLYSOCIAL] Found: {acc.get('name')} (@{acc.get('username')}) [{acc.get('provider')}] id={acc.get('id')} authorized={acc.get('authorized')}")
    targeted = filter_target_accounts(all_accounts)

    if not targeted:
        print("[ONLYSOCIAL] No matching IBGN accounts found.")
        sys.exit(1)

    # Load stories
    stories = load_digest_stories()
    print(f"[ONLYSOCIAL] Loaded {len(stories)} stories.")

    # Build content
    full_content     = build_post_content(stories)
    bluesky_content  = build_bluesky_content(stories, [])
    threads_content  = build_threads_content(stories, [])

    print(f"\n[ONLYSOCIAL] Post content ({len(full_content)} chars):")
    print("-" * 40)
    print(full_content)
    print("-" * 40)

    print(f"\n[ONLYSOCIAL] Bluesky version ({len(bluesky_content)} chars):")
    print(bluesky_content)
    print("-" * 40)

    print(f"\n[ONLYSOCIAL] Threads version ({len(threads_content)} chars):")
    print(threads_content)
    print("-" * 40)

    # Create and post
    try:
        create_and_post(WORKSPACE_UUID, targeted, full_content, bluesky_content, threads_content)
    except Exception as ex:
        print(f"[ONLYSOCIAL] Failed: {ex}")
        sys.exit(1)


if __name__ == "__main__":
    main()
