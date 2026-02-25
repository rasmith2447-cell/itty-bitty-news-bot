#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Dict
from urllib.parse import urlparse

import requests
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dateparser


# =========================
# ENV CONFIG
# =========================
USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNews/Digest").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

NEWSLETTER_NAME = os.getenv("NEWSLETTER_NAME", "Itty Bitty Gaming News").strip()
NEWSLETTER_TAGLINE = os.getenv("NEWSLETTER_TAGLINE", "Snackable daily gaming news — five days a week.").strip()

DIGEST_WINDOW_HOURS = int(os.getenv("DIGEST_WINDOW_HOURS", "24").strip())
DIGEST_TOP_N = int(os.getenv("DIGEST_TOP_N", "5").strip())
DIGEST_MAX_PER_SOURCE = int(os.getenv("DIGEST_MAX_PER_SOURCE", "1").strip())

DIGEST_GUARD_TZ = os.getenv("DIGEST_GUARD_TZ", "America/Los_Angeles").strip()
DIGEST_GUARD_LOCAL_HOUR = int(os.getenv("DIGEST_GUARD_LOCAL_HOUR", "19").strip())
DIGEST_GUARD_LOCAL_MINUTE = int(os.getenv("DIGEST_GUARD_LOCAL_MINUTE", "0").strip())
DIGEST_GUARD_WINDOW_MINUTES = int(os.getenv("DIGEST_GUARD_WINDOW_MINUTES", "120").strip())
DIGEST_FORCE_POST = os.getenv("DIGEST_FORCE_POST", "").strip().lower() in ("1", "true", "yes", "y")

# Featured video
FEATURED_VIDEO_TITLE = os.getenv("FEATURED_VIDEO_TITLE", "Watch today’s Itty Bitty Gaming News").strip()
FEATURED_VIDEO_FALLBACK_URL = os.getenv("FEATURED_VIDEO_FALLBACK_URL", "https://adilo.bigcommand.com/c/ittybittygamingnews/home").strip()
FEATURED_VIDEO_FORCE_ID = os.getenv("FEATURED_VIDEO_FORCE_ID", "").strip()

# YouTube
YOUTUBE_FEATURED_URL = os.getenv("YOUTUBE_FEATURED_URL", "").strip()
YOUTUBE_FEATURED_TITLE = os.getenv("YOUTUBE_FEATURED_TITLE", "").strip()

# Feeds
DEFAULT_FEED_URLS = [
    "https://www.bluesnews.com/news/news_1_0.rdf",
    "https://www.ign.com/rss/v2/articles/feed?categories=games",
    "https://www.gamespot.com/feeds/mashup/",
    "https://gamerant.com/feed",
    "https://www.polygon.com/rss/index.xml",
    "https://www.videogameschronicle.com/feed/",
    "https://www.gematsu.com/feed",
]
_env_feeds = os.getenv("FEED_URLS", "").strip()
FEED_URLS = [f.strip() for f in _env_feeds.splitlines() if f.strip()] if _env_feeds else DEFAULT_FEED_URLS

DIGEST_POST_STATE_FILE = "digest_post_state.json"

# Discord hard limit is 2000 chars; stay safely below
DISCORD_CHAR_LIMIT = 1900


# =========================
# DATA
# =========================
@dataclass
class Item:
    title: str
    url: str
    source: str
    published_utc: datetime
    summary: str
    tags: List[str]


# =========================
# HELPERS
# =========================
def log(msg: str):
    print(msg, flush=True)

def now_local() -> datetime:
    return datetime.now(ZoneInfo(DIGEST_GUARD_TZ))

def normalize_url(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    try:
        p = urlparse(u)
        return p._replace(query="").geturl()
    except Exception:
        return u

def source_from_url(u: str) -> str:
    try:
        host = urlparse(u).netloc.lower().replace("www.", "")
        if "ign.com" in host: return "IGN"
        if "gamespot.com" in host: return "GameSpot"
        if "gamerant.com" in host: return "GameRant"
        if "polygon.com" in host: return "Polygon"
        if "videogameschronicle.com" in host: return "VGC"
        if "gematsu.com" in host: return "Gematsu"
        if "bluesnews.com" in host: return "Blue's News"
        return host
    except Exception:
        return "Source"

def clean_html(text: str, max_len: int = 420) -> str:
    if not text:
        return ""
    try:
        soup = BeautifulSoup(text, "html.parser")
        s = soup.get_text(" ", strip=True)
    except Exception:
        s = text
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "…"
    return s

def looks_like_non_news(title: str, summary: str) -> bool:
    t = (title or "").lower()
    s = (summary or "").lower()
    blob = f"{t} {s}"

    reject = [
        "best ", "top ", "ranked", "ranking", "guide", "walkthrough",
        "poll:", "debate:", "opinion", "letters", "mailbox",
        "deal", "deals", "discount", "% off", "price drop", "drops to $", "drops to",
        "woot", "amazon", "power bank", "controller",
        "history of", "(2026 update)", "update)",
        "rumor", "rumours", "leak", "leaked", "speculation", "reportedly",
        "allegedly", "might be", "could be", "possibly",
        "how to draw", "walt disney world", "olaf", "disney",
    ]
    if any(p in blob for p in reject):
        return True

    gaming_signals = [
        "game", "gaming", "xbox", "playstation", "ps5", "ps4", "nintendo", "switch",
        "steam", "pc", "console", "gpu", "nvidia", "amd", "intel",
        "ubisoft", "ea", "bethesda", "blizzard", "activision", "sony", "microsoft", "valve",
        "studio", "developer", "patch", "update", "launch", "reveal", "announced", "trailer",
        "dlc", "expansion", "demo", "early access", "game pass", "ps plus",
    ]
    if not any(sig in blob for sig in gaming_signals):
        return True

    return False

def build_tags(title: str, summary: str) -> List[str]:
    blob = f"{title} {summary}".lower()
    tags = []
    mapping = [
        ("xbox", "#Xbox"),
        ("playstation", "#PlayStation"),
        ("ps5", "#PS5"),
        ("nintendo", "#Nintendo"),
        ("switch", "#Switch"),
        ("steam", "#Steam"),
        ("pc", "#PCGaming"),
    ]
    for key, tag in mapping:
        if key in blob and tag not in tags:
            tags.append(tag)
    return tags[:4]

def load_post_state() -> Dict:
    if not os.path.exists(DIGEST_POST_STATE_FILE):
        return {}
    try:
        with open(DIGEST_POST_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def save_post_state(d: Dict):
    try:
        with open(DIGEST_POST_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass

def should_skip_already_posted_today() -> bool:
    if DIGEST_FORCE_POST:
        return False
    st = load_post_state()
    last_date = (st.get("last_post_local_date") or "").strip()
    today = now_local().strftime("%Y-%m-%d")
    if last_date == today:
        log(f"[GUARD] Already posted for local date {today}. Skipping.")
        return True
    return False

def mark_posted_today():
    st = load_post_state()
    st["last_post_local_date"] = now_local().strftime("%Y-%m-%d")
    st["last_post_utc"] = datetime.now(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")
    save_post_state(st)

def guard_should_post_now() -> bool:
    if DIGEST_FORCE_POST:
        log("[GUARD] DIGEST_FORCE_POST enabled — bypassing time guard.")
        return True

    nl = now_local()
    target = nl.replace(hour=DIGEST_GUARD_LOCAL_HOUR, minute=DIGEST_GUARD_LOCAL_MINUTE, second=0, microsecond=0)
    candidates = [target - timedelta(days=1), target, target + timedelta(days=1)]
    closest = min(candidates, key=lambda t: abs((nl - t).total_seconds()))
    delta_min = abs((nl - closest).total_seconds()) / 60.0

    if delta_min <= DIGEST_GUARD_WINDOW_MINUTES:
        log(f"[GUARD] OK. Local now: {nl.strftime('%Y-%m-%d %H:%M:%S %Z')} Delta={delta_min:.1f}min")
        return True

    log(f"[GUARD] Not within posting window. Local now: {nl.strftime('%Y-%m-%d %H:%M:%S %Z')} Delta={delta_min:.1f}min")
    return False

def discord_post(content: str):
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL")

    payload = {"content": content}
    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, headers={"User-Agent": USER_AGENT}, timeout=25)

    # Helpful debug if Discord rejects
    if r.status_code >= 400:
        log(f"[DISCORD] HTTP {r.status_code} response: {r.text[:300]}")
    r.raise_for_status()


# =========================
# RSS FETCH
# =========================
def fetch_feed_items(feed_url: str) -> List[Item]:
    log(f"[RSS] GET {feed_url}")
    fp = feedparser.parse(feed_url, request_headers={"User-Agent": USER_AGENT})

    if getattr(fp, "bozo", 0):
        log(f"[RSS] bozo=1 for {feed_url}: {getattr(fp, 'bozo_exception', '')}")

    out: List[Item] = []
    for e in (fp.entries or [])[:80]:
        title = (getattr(e, "title", "") or "").strip()
        link = (getattr(e, "link", "") or "").strip()
        if not title or not link:
            continue

        published = getattr(e, "published", "") or getattr(e, "updated", "") or ""
        dt = None
        if published:
            try:
                dt = dateparser.parse(published)
            except Exception:
                dt = None
        if dt is None:
            dt = datetime.now(ZoneInfo("UTC"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        dt_utc = dt.astimezone(ZoneInfo("UTC"))

        summary = getattr(e, "summary", "") or getattr(e, "description", "") or ""
        summary = clean_html(summary, max_len=420)

        url = normalize_url(link)
        source = source_from_url(url)

        out.append(Item(title=title, url=url, source=source, published_utc=dt_utc, summary=summary, tags=[]))

    return out

def dedupe(items: List[Item]) -> List[Item]:
    seen = set()
    out = []
    for it in items:
        key = normalize_url(it.url).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out

def pick_top(items: List[Item]) -> List[Item]:
    items = sorted(items, key=lambda x: x.published_utc, reverse=True)

    picked: List[Item] = []
    per_source: Dict[str, int] = {}

    for it in items:
        if len(picked) >= DIGEST_TOP_N:
            break
        c = per_source.get(it.source, 0)
        if c >= DIGEST_MAX_PER_SOURCE:
            continue
        picked.append(it)
        per_source[it.source] = c + 1

    if len(picked) < DIGEST_TOP_N:
        for it in items:
            if len(picked) >= DIGEST_TOP_N:
                break
            if it in picked:
                continue
            picked.append(it)

    return picked[:DIGEST_TOP_N]

def adilo_latest_watch_url() -> str:
    if FEATURED_VIDEO_FORCE_ID:
        return f"https://adilo.bigcommand.com/watch/{FEATURED_VIDEO_FORCE_ID}"
    return FEATURED_VIDEO_FALLBACK_URL


# =========================
# DISCORD MESSAGE BUILD + SAFE TRIM
# =========================
def build_message(items: List[Item], summary_max_len: int = 260) -> str:
    date_line = now_local().strftime("%B %d, %Y")

    teaser_lines = []
    for it in items[:3]:
        teaser_lines.append(f"► 🎮 {it.title}")

    header = (
        f"**{date_line}**\n\n"
        f"**In Tonight’s Edition of {NEWSLETTER_NAME}…**\n"
        + "\n".join(teaser_lines)
        + "\n\n"
        f"## Tonight’s Top Stories\n\n"
    )

    parts = [header]

    for idx, it in enumerate(items, start=1):
        it.tags = build_tags(it.title, it.summary)
        tag_line = (" " + " ".join(it.tags)) if it.tags else ""

        sm = clean_html(it.summary or "", max_len=summary_max_len)
        parts.append(f"**{idx}) {it.title}**{tag_line}\n{sm}\nSource: {it.source} — {it.url}\n\n")

    # Featured video blocks (YouTube above Adilo)
    if YOUTUBE_FEATURED_URL:
        yt_title = YOUTUBE_FEATURED_TITLE or "Latest episode on YouTube"
        parts.append(f"## ▶️ YouTube (same episode)\n**{yt_title}**\n{YOUTUBE_FEATURED_URL}\n\n")

    adilo_url = adilo_latest_watch_url()
    parts.append(f"## 📺 Featured Video (Adilo)\n**{FEATURED_VIDEO_TITLE}**\n{adilo_url}\n\n")

    parts.append("—\nThat’s it for tonight’s Itty Bitty.\nCatch the snackable breakdown tomorrow.\n")
    return "".join(parts)

def fit_to_discord(items: List[Item]) -> str:
    # Try with full-ish summaries, then progressively shrink.
    for max_len in (320, 260, 200, 140, 100):
        msg = build_message(items, summary_max_len=max_len)
        if len(msg) <= DISCORD_CHAR_LIMIT:
            log(f"[DISCORD] Message size OK: {len(msg)} chars (summary_max_len={max_len})")
            return msg

    # Still too big: drop story 5 if necessary, then re-fit.
    if len(items) > 4:
        log("[DISCORD] Still too long. Dropping story #5 to fit Discord limits.")
        return fit_to_discord(items[:4])

    # Absolute last resort: hard truncate.
    msg = build_message(items, summary_max_len=80)
    if len(msg) > DISCORD_CHAR_LIMIT:
        msg = msg[: DISCORD_CHAR_LIMIT - 1] + "…"
    return msg


def main():
    if should_skip_already_posted_today():
        return

    if not guard_should_post_now():
        return

    window_start = datetime.now(ZoneInfo("UTC")) - timedelta(hours=DIGEST_WINDOW_HOURS)

    all_items: List[Item] = []
    for feed in FEED_URLS:
        try:
            all_items.extend(fetch_feed_items(feed))
        except Exception as e:
            log(f"[RSS] Feed failed: {feed} ({e})")

    if not all_items:
        log("[DIGEST] No feed items fetched. Exiting without posting.")
        return

    all_items = [it for it in all_items if it.published_utc >= window_start]

    filtered: List[Item] = []
    for it in all_items:
        if looks_like_non_news(it.title, it.summary):
            continue
        filtered.append(it)

    filtered = dedupe(filtered)

    if not filtered:
        log("[DIGEST] No items after filtering. Exiting without posting.")
        return

    top = pick_top(filtered)

    msg = fit_to_discord(top)
    discord_post(msg)
    mark_posted_today()

    log(f"Digest posted. Items: {len(top)}")


if __name__ == "__main__":
    main()
