#!/usr/bin/env python3
"""
digest.py — Itty Bitty Gaming News
Daily newsletter digest: scores stories intelligently, posts a bold
gamer-flavoured newsletter to Discord with YouTube + Adilo video links.
All feed/filter/fetch logic lives in shared.py.
"""

import json
import os
import re
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from shared import (
    FEEDS,
    Item,
    compute_score,
    fetch_all_feeds,
    getenv,
    post_webhook,
    shorten,
    utcnow,
)

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

DISCORD_WEBHOOK_URL = getenv("DISCORD_WEBHOOK_URL")
DIGEST_TOP_N        = int(getenv("DIGEST_TOP_N", "5"))
DIGEST_MAX_PER_SOURCE = int(getenv("DIGEST_MAX_PER_SOURCE", "2"))
DIGEST_WINDOW_HOURS = int(getenv("DIGEST_WINDOW_HOURS", "24"))
DIGEST_CACHE_FILE   = getenv("DIGEST_CACHE_FILE", ".digest_cache.json")
DIGEST_FORCE_POST   = getenv("DIGEST_FORCE_POST", "").lower() in ("1", "true", "yes", "y")
DIGEST_POST_ONCE_PER_DAY = getenv("DIGEST_POST_ONCE_PER_DAY", "").lower() in ("1", "true", "yes", "y")

DIGEST_GUARD_TZ      = getenv("DIGEST_GUARD_TZ", "America/Los_Angeles")
DIGEST_GUARD_HOUR    = int(getenv("DIGEST_GUARD_LOCAL_HOUR", "19"))
DIGEST_GUARD_MINUTE  = int(getenv("DIGEST_GUARD_LOCAL_MINUTE", "0"))
DIGEST_GUARD_WINDOW  = int(getenv("DIGEST_GUARD_WINDOW_MINUTES", "30"))

NEWSLETTER_NAME    = getenv("NEWSLETTER_NAME", "Itty Bitty Gaming News")
NEWSLETTER_TAGLINE = getenv("NEWSLETTER_TAGLINE", "Your snackable video game news.")
NEWSLETTER_EMOJI   = getenv("NEWSLETTER_EMOJI", "🎮")

YOUTUBE_CHANNEL_ID  = getenv("YOUTUBE_CHANNEL_ID")
YOUTUBE_RSS_URL     = getenv("YOUTUBE_RSS_URL")
YOUTUBE_FILTER_SHORTS = getenv("YOUTUBE_FILTER_SHORTS", "true").lower() in ("1", "true", "yes", "y")

ADILO_PUBLIC_KEY    = getenv("ADILO_PUBLIC_KEY")
ADILO_SECRET_KEY    = getenv("ADILO_SECRET_KEY")
ADILO_PROJECT_ID    = getenv("ADILO_PROJECT_ID")
ADILO_LATEST_PAGE   = getenv("ADILO_PUBLIC_LATEST_PAGE", "https://adilo.bigcommand.com/c/ittybittygamingnews/video")
ADILO_HOME_PAGE     = getenv("ADILO_PUBLIC_HOME_PAGE", "https://adilo.bigcommand.com/c/ittybittygamingnews/home")
ADILO_FALLBACK_VIDEO = getenv("ADILO_FALLBACK_VIDEO", "https://adilo.bigcommand.com/watch/9u7iHmrc")

UA = getenv("USER_AGENT", "IttyBittyGamingNews/Digest")

# ---------------------------------------------------------------------------
# CACHE
# ---------------------------------------------------------------------------

def load_cache() -> Dict:
    try:
        with open(DIGEST_CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache: Dict) -> None:
    try:
        with open(DIGEST_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, sort_keys=True)
    except Exception as e:
        print(f"[CACHE] Save failed: {e}")


# ---------------------------------------------------------------------------
# SCHEDULING GUARDS
# ---------------------------------------------------------------------------

def now_local() -> datetime:
    return datetime.now(ZoneInfo(DIGEST_GUARD_TZ))


def guard_posting_window() -> bool:
    if DIGEST_FORCE_POST:
        print("[GUARD] DIGEST_FORCE_POST — bypassing time guard.")
        return True

    now    = now_local()
    target = now.replace(hour=DIGEST_GUARD_HOUR, minute=DIGEST_GUARD_MINUTE, second=0, microsecond=0)
    candidates = [target - timedelta(days=1), target, target + timedelta(days=1)]
    closest    = min(candidates, key=lambda t: abs((now - t).total_seconds()))
    delta_min  = abs((now - closest).total_seconds()) / 60.0

    if delta_min <= DIGEST_GUARD_WINDOW:
        print(f"[GUARD] ✅ Within posting window. Now={now:%H:%M %Z} | Target={closest:%H:%M %Z} | Δ={delta_min:.1f}min")
        return True

    print(f"[GUARD] ⏸️  Outside window. Now={now:%H:%M %Z} | Target={closest:%H:%M %Z} | Δ={delta_min:.1f}min")
    return False


def guard_once_per_day(cache: Dict) -> bool:
    if DIGEST_FORCE_POST or not DIGEST_POST_ONCE_PER_DAY:
        return True
    today = now_local().strftime("%Y-%m-%d")
    if today in cache.get("posted_dates", []):
        print(f"[GUARD] Already posted for {today}. Skipping.")
        return False
    return True


def mark_posted_today(cache: Dict) -> None:
    today = now_local().strftime("%Y-%m-%d")
    cache.setdefault("posted_dates", [])
    if today not in cache["posted_dates"]:
        cache["posted_dates"].append(today)
        cache["posted_dates"] = cache["posted_dates"][-90:]  # keep 90 days


# ---------------------------------------------------------------------------
# STORY SELECTION  (smarter than pure recency)
# ---------------------------------------------------------------------------

def pick_top_stories(items: List[Item]) -> List[Item]:
    """
    Score every item, then pick top-N with per-source cap.
    Items within the digest window only.
    """
    cutoff = utcnow() - timedelta(hours=DIGEST_WINDOW_HOURS)
    recent = [it for it in items if it.published_at >= cutoff]

    # Compute scores
    for it in recent:
        it.score = compute_score(it)

    # Sort by score descending, then by recency as tiebreaker
    recent.sort(key=lambda x: (x.score, x.published_at.timestamp()), reverse=True)

    picked: List[Item] = []
    per_source: Dict[str, int] = {}
    seen_urls: set = set()

    for it in recent:
        if len(picked) >= DIGEST_TOP_N:
            break
        if it.url in seen_urls:
            continue
        seen_urls.add(it.url)

        per_source.setdefault(it.source, 0)
        if per_source[it.source] >= DIGEST_MAX_PER_SOURCE:
            continue

        per_source[it.source] += 1
        picked.append(it)

    return picked


# ---------------------------------------------------------------------------
# NEWSLETTER FORMATTING  (bold, gamer-y, scannable)
# ---------------------------------------------------------------------------

# Emoji map for tags → colourful Discord display
TAG_DISPLAY = {
    "📣 ANNOUNCEMENT": "📣",
    "🚀 OUT NOW":      "🚀",
    "🔧 PATCH":        "🔧",
    "🔄 UPDATE":       "🔄",
    "⏳ DELAY":        "⏳",
    "💼 LAYOFFS":      "💼",
    "🔒 SHUTDOWN":     "🔒",
    "🤝 M&A":          "🤝",
    "⚖️ LEGAL":        "⚖️",
    "🎖️ RETIREMENT":   "🎖️",
    "💸 PRICE CHANGE": "💸",
    "📅 DATE CONFIRMED":"📅",
    "🆓 FREE":         "🆓",
}

SECTION_DIVIDER = "▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬"

STORY_ICONS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]


def _tag_badges(tags: List[str]) -> str:
    """Convert tag list to compact emoji badges."""
    badges = []
    for t in tags[:4]:
        badges.append(TAG_DISPLAY.get(t, t))
    return "  ".join(badges)


def build_header_embed(top_stories: List[Item]) -> Dict:
    """
    Big splash embed: newsletter title, date, teaser headlines.
    """
    tz     = ZoneInfo(DIGEST_GUARD_TZ)
    today  = datetime.now(tz).strftime("%A, %B %d, %Y")

    # Teaser lines — top 3 headlines as bullet teasers
    teaser_lines = []
    for i, s in enumerate(top_stories[:3]):
        icon = ["🔥", "⚡", "🎯"][i]
        teaser_lines.append(f"{icon} {s.title}")

    desc = "\n".join([
        f"*{NEWSLETTER_TAGLINE}*",
        "",
        f"**📅 {today}**",
        "",
        SECTION_DIVIDER,
        "**Tonight's Headlines**",
        SECTION_DIVIDER,
        "\n".join(teaser_lines),
        "",
        "⬇️ *Full stories below*",
    ])

    return {
        "title":       f"{NEWSLETTER_EMOJI} {NEWSLETTER_NAME}",
        "description": desc,
        "color":       0x7C3AED,   # bold purple — feels gamer-y
    }


def build_story_embed(rank: int, story: Item) -> Dict:
    """
    Individual story embed with rich formatting.
    rank is 0-indexed.
    """
    icon  = STORY_ICONS[rank] if rank < len(STORY_ICONS) else f"{rank + 1}."
    title = f"{icon}  {story.title}"[:256]

    # Build description block
    parts = []

    # Summary
    if story.summary:
        parts.append(f"*{shorten(story.summary, 280)}*")

    # Tag badges
    if story.tags:
        parts.append(_tag_badges(story.tags))

    # Source + score debug (score only shown if DEBUG)
    source_line = f"📰 **{story.source}**"
    if story.published_at:
        source_line += f"  ·  🕐 <t:{int(story.published_at.timestamp())}:R>"
    parts.append(source_line)

    desc = "\n\n".join(p for p in parts if p)[:4096]

    embed: Dict = {
        "title":       title,
        "url":         story.url,
        "description": desc,
        "color":       _rank_color(rank),
    }

    if story.image_url:
        embed["image"] = {"url": story.image_url}

    if story.published_at:
        embed["timestamp"] = story.published_at.isoformat()

    return embed


def _rank_color(rank: int) -> int:
    """Gold → silver → bronze → neutral palette."""
    colors = [0xFFD700, 0xC0C0C0, 0xCD7F32, 0x5865F2, 0x57F287]
    return colors[rank] if rank < len(colors) else 0x5865F2


def build_footer_embed(story_count: int) -> Dict:
    """Closing embed with subscribe nudge."""
    tz    = ZoneInfo(DIGEST_GUARD_TZ)
    today = datetime.now(tz).strftime("%B %d, %Y")

    desc = "\n".join([
        SECTION_DIVIDER,
        f"That's your **{NEWSLETTER_NAME}** for **{today}**!",
        "",
        "🎬 **Video Edition** dropping above 👆",
        "📺 Subscribe on YouTube for daily gaming clips",
        "💬 Drop your reactions below — what story had you talking?",
        "",
        "*Stay small. Stay mighty. Itty Bitty Gaming News.*",
    ])

    return {
        "description": desc,
        "color":       0x7C3AED,
        "footer":      {"text": f"{NEWSLETTER_NAME}  •  {story_count} stories tonight"},
    }


# ---------------------------------------------------------------------------
# VIDEO HELPERS  (YouTube + Adilo — unchanged logic, cleaner structure)
# ---------------------------------------------------------------------------

def youtube_latest() -> Optional[Tuple[str, str]]:
    rss = YOUTUBE_RSS_URL
    if not rss and YOUTUBE_CHANNEL_ID:
        rss = f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}"
    if not rss:
        return None

    try:
        r = requests.get(rss, headers={"User-Agent": UA}, timeout=25)
        r.raise_for_status()
        entries = re.findall(r"<entry\b.*?</entry>", r.text, flags=re.DOTALL)
        for ent in entries[:25]:
            m_vid   = re.search(r"<yt:videoId>([^<]+)</yt:videoId>", ent)
            m_title = re.search(r"<title>([^<]+)</title>", ent)
            if not m_vid:
                continue
            vid   = m_vid.group(1).strip()
            title = m_title.group(1).strip() if m_title else "Latest video"
            if YOUTUBE_FILTER_SHORTS:
                t = title.lower()
                if "#shorts" in t or " shorts" in t or t.endswith("shorts"):
                    continue
            return (f"https://www.youtube.com/watch?v={vid}", title)
    except Exception as ex:
        print(f"[YT] Failed: {ex}")
    return None


def _adilo_via_manual_override() -> Optional[str]:
    """
    Highest-priority override: set ADILO_VIDEO_ID in GitHub secrets/vars
    to always post a specific video (useful when scrape can't reach latest).
    Example: ADILO_VIDEO_ID=abc123xyz
    """
    vid = getenv("ADILO_VIDEO_ID", "").strip()
    if vid:
        url = f"https://adilo.bigcommand.com/watch/{vid}"
        print(f"[ADILO] Manual override via ADILO_VIDEO_ID: {url}")
        return url
    return None


def _adilo_via_api() -> Optional[str]:
    """
    Try Adilo API. Requires ADILO_PUBLIC_KEY + ADILO_SECRET_KEY.
    ADILO_PROJECT_ID is optional — if missing, we list all projects first.
    """
    pub = getenv("ADILO_PUBLIC_KEY", "").strip()
    sec = getenv("ADILO_SECRET_KEY", "").strip()
    if not (pub and sec):
        print("[ADILO] API skipped — ADILO_PUBLIC_KEY or ADILO_SECRET_KEY not set.")
        return None

    api_headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "X-Public-Key": pub,
        "X-Secret-Key": sec,
    }
    base = "https://adilo-api.bigcommand.com/v1"

    # If no project ID, list projects and use the first one
    pid = getenv("ADILO_PROJECT_ID", "").strip()
    if not pid:
        print("[ADILO] ADILO_PROJECT_ID not set — attempting to list projects to find it...")
        try:
            r = requests.get(f"{base}/projects", headers=api_headers, timeout=25)
            r.raise_for_status()
            data = r.json()
            print(f"[ADILO] Projects response keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
            projects = data.get("payload") or data.get("data") or data.get("projects") or []
            if isinstance(data, list):
                projects = data
            if projects:
                pid = projects[0].get("id") or projects[0].get("uuid") or ""
                print(f"[ADILO] Auto-detected project ID: {pid}")
                print(f"[ADILO] TIP: Set ADILO_PROJECT_ID={pid} in your GitHub secrets to skip this lookup.")
        except Exception as ex:
            print(f"[ADILO] Project list failed: {ex}")
            return None

    if not pid:
        print("[ADILO] Could not determine project ID — API skipped.")
        return None

    # Fetch files for this project, sorted newest first
    try:
        url = f"{base}/projects/{pid}/files?From=1&To=10"
        r = requests.get(url, headers=api_headers, timeout=25)
        r.raise_for_status()
        data = r.json()
        payload = data.get("payload") or data.get("data") or data.get("files") or []
        if isinstance(data, list):
            payload = data
        print(f"[ADILO] API returned {len(payload)} file(s) for project {pid}")
        if payload:
            # First item = newest
            fid = payload[0].get("id") or payload[0].get("uuid") or payload[0].get("file_id") or ""
            if fid:
                url = f"https://adilo.bigcommand.com/watch/{fid}"
                print(f"[ADILO] API resolved latest video: {url}")
                return url
            else:
                print(f"[ADILO] First file has no id/uuid. Keys: {list(payload[0].keys())}")
        else:
            print("[ADILO] API returned empty file list.")
    except Exception as ex:
        print(f"[ADILO] Files API failed: {ex}")

    return None


def _adilo_via_scrape(cache: Dict) -> Optional[str]:
    """
    Scrape the Adilo channel page for video IDs.
    NOTE: Adilo is a JS-rendered SPA — raw HTML often won't contain /watch/ IDs.
    This catches cases where the page does embed IDs in its initial HTML/JSON payload.
    """
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Cache-Control": "no-cache",
    }

    for u in [ADILO_LATEST_PAGE, ADILO_HOME_PAGE]:
        try:
            print(f"[ADILO] Scraping: {u}")
            r = requests.get(u, headers=headers, timeout=25, allow_redirects=True)
            r.raise_for_status()
            html = r.text
            final_url = r.url

            print(f"[ADILO] Final URL after redirects: {final_url}")
            print(f"[ADILO] HTML size: {len(html)} chars")

            # 1. Check redirect URL for ?id= or /watch/
            q = parse_qs(urlparse(final_url).query)
            if q.get("id"):
                vid = q["id"][0].strip()
                if vid and vid.lower() not in ("", "latest"):
                    watch = f"https://adilo.bigcommand.com/watch/{vid}"
                    print(f"[ADILO] Found ID in redirect URL: {watch}")
                    cache["last_good_adilo_watch_url"] = watch
                    return watch

            m = re.search(r"/watch/([A-Za-z0-9_-]{6,})", final_url)
            if m:
                watch = f"https://adilo.bigcommand.com/watch/{m.group(1)}"
                print(f"[ADILO] Found /watch/ in redirect URL: {watch}")
                cache["last_good_adilo_watch_url"] = watch
                return watch

            # 2. og:url / og:video meta tags
            soup = BeautifulSoup(html, "html.parser")
            for prop in ("og:url", "og:video", "og:video:url"):
                tag = soup.find("meta", property=prop)
                if tag and tag.get("content"):
                    m = re.search(r"/watch/([A-Za-z0-9_-]{6,})", tag["content"])
                    if m:
                        watch = f"https://adilo.bigcommand.com/watch/{m.group(1)}"
                        print(f"[ADILO] Found ID in {prop}: {watch}")
                        cache["last_good_adilo_watch_url"] = watch
                        return watch

            # 3. JSON blobs embedded in page (SPA initial state)
            json_blobs = re.findall(r'\{[^{}]{20,}\}', html)
            for blob in json_blobs:
                m = re.search(r'"(?:id|videoId|file_id|uuid)"\s*:\s*"([A-Za-z0-9_-]{6,})"', blob)
                if m:
                    vid = m.group(1)
                    # Sanity check: not a generic UUID-like token
                    if len(vid) >= 6 and not vid.startswith("UC"):
                        watch = f"https://adilo.bigcommand.com/watch/{vid}"
                        print(f"[ADILO] Found ID in JSON blob: {watch}")
                        cache["last_good_adilo_watch_url"] = watch
                        return watch

            # 4. Direct /watch/ href anywhere in HTML
            ids = re.findall(r'href=["\'][^"\']*?/watch/([A-Za-z0-9_-]{6,})["\']', html)
            if ids:
                watch = f"https://adilo.bigcommand.com/watch/{ids[0]}"
                print(f"[ADILO] Found /watch/ href in HTML: {watch}")
                cache["last_good_adilo_watch_url"] = watch
                return watch

            # 5. Any /watch/ pattern at all in raw HTML
            ids = re.findall(r'/watch/([A-Za-z0-9_-]{6,})', html)
            if ids:
                watch = f"https://adilo.bigcommand.com/watch/{ids[-1]}"
                print(f"[ADILO] Found /watch/ pattern in raw HTML: {watch}")
                cache["last_good_adilo_watch_url"] = watch
                return watch

            print(f"[ADILO] No video IDs found in scraped HTML from {u}")
            print(f"[ADILO] First 500 chars of HTML: {html[:500]}")

        except Exception as ex:
            print(f"[ADILO] Scrape failed ({u}): {ex}")

    return None


def adilo_latest(cache: Dict) -> str:
    """
    Resolution order:
      1. ADILO_VIDEO_ID manual override (GitHub secret — set this for instant fix)
      2. Adilo API (auto-detects project if ADILO_PROJECT_ID missing)
      3. Scrape channel page
      4. Last-good URL from cache
      5. Hardcoded known-good video (https://adilo.bigcommand.com/watch/9u7iHmrc)

    Always returns a postable URL — never None or empty.
    Update the hardcoded known-good whenever you publish a new video.
    """
    # 1. Manual override
    result = _adilo_via_manual_override()
    if result:
        cache["last_good_adilo_watch_url"] = result
        return result

    # 2. API
    result = _adilo_via_api()
    if result:
        cache["last_good_adilo_watch_url"] = result
        return result

    # 3. Scrape
    result = _adilo_via_scrape(cache)
    if result:
        return result

    # 4. Last-good cache
    last_good = cache.get("last_good_adilo_watch_url", "")
    if last_good:
        print(f"[ADILO] Using cached last-good: {last_good}")
        return last_good

    # 5. Hardcoded known-good video (last manually confirmed URL)
    known_good = "https://adilo.bigcommand.com/watch/9u7iHmrc"
    print(f"[ADILO] Using hardcoded known-good video: {known_good}")
    return known_good


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL is not set.")

    cache = load_cache()

    if not guard_posting_window():
        return
    if not guard_once_per_day(cache):
        return

    # --- Fetch + filter + cluster ---
    print("[DIGEST] Fetching feeds…")
    all_items, reasons = fetch_all_feeds(FEEDS)

    if not all_items:
        print("[DIGEST] No items after filtering. Exiting.")
        return

    # --- Score + pick top stories ---
    top = pick_top_stories(all_items)

    if not top:
        print("[DIGEST] No items selected after scoring. Exiting.")
        return

    print(f"[DIGEST] Selected {len(top)} stories:")
    for i, s in enumerate(top):
        print(f"  {i+1}. [{s.score:>3}pts] {s.source}: {s.title}")

    # --- Build Discord payload ---
    header_embed = build_header_embed(top)
    story_embeds = [build_story_embed(i, s) for i, s in enumerate(top)]
    footer_embed = build_footer_embed(len(top))

    all_embeds = [header_embed] + story_embeds + [footer_embed]

    # Discord limit is 10 embeds per message — split if needed
    CHUNK = 10
    for i in range(0, len(all_embeds), CHUNK):
        chunk = all_embeds[i:i + CHUNK]
        content = "" if i > 0 else None
        post_webhook(DISCORD_WEBHOOK_URL, content="", embeds=chunk)

    # --- Video links (Adilo first, then YouTube) ---
    # Adilo: try API → scrape → last-good cache → home page fallback
    adilo_url = adilo_latest(cache)
    if adilo_url:
        print(f"[ADILO] Posting: {adilo_url}")
        post_webhook(DISCORD_WEBHOOK_URL, content=f"🎬 **Tonight's Video Edition:**\n{adilo_url}")
    else:
        print("[ADILO] No URL resolved — skipping.")

    # YouTube: always attempt; skip only if channel not configured
    yt = youtube_latest()
    if yt:
        yt_url, yt_title = yt
        print(f"[YT] Posting: {yt_url}")
        post_webhook(DISCORD_WEBHOOK_URL, content=f"📺 **Latest on YouTube — {yt_title}:**\n{yt_url}")
    else:
        print("[YT] No video found or channel not configured — skipping.")

    # --- Finalise ---
    mark_posted_today(cache)
    save_cache(cache)

    print("\n════════════════════════════════")
    print(f"  {NEWSLETTER_NAME} digest posted!")
    print(f"  Stories: {len(top)}")
    if reasons:
        top_reasons = sorted(reasons.items(), key=lambda x: x[1], reverse=True)[:5]
        print("  Top filter reasons:")
        for k, v in top_reasons:
            print(f"    • {k}: {v}")
    print("════════════════════════════════\n")


if __name__ == "__main__":
    main()
