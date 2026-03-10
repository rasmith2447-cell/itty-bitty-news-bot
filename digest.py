#!/usr/bin/env python3
"""
digest.py — Itty Bitty Gaming News
Daily newsletter digest: scores stories intelligently, posts a bold
gamer-flavoured newsletter to Discord with a YouTube video link.
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
    topic_similarity,
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
    Score every item, apply topic-similarity penalty, then pick top-N.

    Topic penalty: once a story is picked, any remaining candidate that
    is about the same topic (similarity >= TOPIC_SIMILARITY_THRESHOLD)
    gets a score penalty. This means the newsletter will prefer diverse
    stories over 4 variations of the same Mario movie trailer.

    Threshold 72 is intentionally loose — catches near-duplicates like
    'Donald Glover confirmed as Yoshi' appearing from 4 different outlets,
    while still allowing genuinely different angles on the same franchise
    (e.g. a Zelda delay AND a Zelda DLC announcement on the same day).
    """
    TOPIC_SIMILARITY_THRESHOLD = 72
    TOPIC_PENALTY = 40  # subtracted from score for each similar picked story

    cutoff = utcnow() - timedelta(hours=DIGEST_WINDOW_HOURS)
    recent = [it for it in items if it.published_at >= cutoff]

    # Compute base scores
    for it in recent:
        it.score = compute_score(it)

    picked: List[Item] = []
    per_source: Dict[str, int] = {}
    seen_urls: set = set()

    # We loop up to top_n * 6 times to allow re-sorting after penalties
    # without an infinite loop risk.
    max_iterations = DIGEST_TOP_N * 6
    iterations = 0

    while len(picked) < DIGEST_TOP_N and iterations < max_iterations:
        iterations += 1

        # Re-sort after each pick (penalties may have reshuffled the queue)
        recent.sort(key=lambda x: (x.score, x.published_at.timestamp()), reverse=True)

        advanced = False
        for it in recent:
            if it.url in seen_urls:
                continue

            per_source.setdefault(it.source, 0)
            if per_source[it.source] >= DIGEST_MAX_PER_SOURCE:
                continue

            # Pick this story
            seen_urls.add(it.url)
            per_source[it.source] += 1
            picked.append(it)

            # Apply similarity penalty to remaining candidates
            for other in recent:
                if other.url in seen_urls:
                    continue
                sim = topic_similarity(it.title, other.title)
                if sim >= TOPIC_SIMILARITY_THRESHOLD:
                    penalty = TOPIC_PENALTY + int((sim - TOPIC_SIMILARITY_THRESHOLD) * 0.5)
                    other.score -= penalty
                    if other.score < 0:
                        other.score = 0

            advanced = True
            break

        if not advanced:
            break  # No more eligible stories

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
