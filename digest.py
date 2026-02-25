#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Optional, Dict, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

# =========================
# CONFIG (Env)
# =========================

USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNews/Digest").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

NEWSLETTER_NAME = os.getenv("NEWSLETTER_NAME", "Itty Bitty Gaming News").strip()
NEWSLETTER_TAGLINE = os.getenv(
    "NEWSLETTER_TAGLINE",
    "Snackable daily gaming news — five days a week."
).strip()

DIGEST_WINDOW_HOURS = int(os.getenv("DIGEST_WINDOW_HOURS", "24").strip())
DIGEST_TOP_N = int(os.getenv("DIGEST_TOP_N", "5").strip())
DIGEST_MAX_PER_SOURCE = int(os.getenv("DIGEST_MAX_PER_SOURCE", "1").strip())

# Guard (posting time)
DIGEST_GUARD_TZ = os.getenv("DIGEST_GUARD_TZ", "America/Los_Angeles").strip()
DIGEST_GUARD_LOCAL_HOUR = int(os.getenv("DIGEST_GUARD_LOCAL_HOUR", "19").strip())   # 7pm PT
DIGEST_GUARD_LOCAL_MINUTE = int(os.getenv("DIGEST_GUARD_LOCAL_MINUTE", "0").strip())
DIGEST_GUARD_WINDOW_MINUTES = int(os.getenv("DIGEST_GUARD_WINDOW_MINUTES", "120").strip())  # allow delays
DIGEST_FORCE_POST = os.getenv("DIGEST_FORCE_POST", "").strip().lower() in ("1", "true", "yes", "y")

# Featured video controls
FEATURED_VIDEO_TITLE = os.getenv("FEATURED_VIDEO_TITLE", "Watch today’s Itty Bitty Gaming News").strip()

# Adilo
ADILO_PUBLIC_LATEST_PAGE = os.getenv(
    "ADILO_PUBLIC_LATEST_PAGE",
    "https://adilo.bigcommand.com/c/ittybittygamingnews/video"
).strip()
ADILO_PUBLIC_HOME_PAGE = os.getenv(
    "ADILO_PUBLIC_HOME_PAGE",
    "https://adilo.bigcommand.com/c/ittybittygamingnews/home"
).strip()
FEATURED_VIDEO_FALLBACK_URL = os.getenv("FEATURED_VIDEO_FALLBACK_URL", ADILO_PUBLIC_HOME_PAGE).strip()
FEATURED_VIDEO_FORCE_ID = os.getenv("FEATURED_VIDEO_FORCE_ID", "").strip()  # e.g. K4AxdfCP

# YouTube (optional, from content_board workflow or env)
YOUTUBE_FEATURED_URL = os.getenv("YOUTUBE_FEATURED_URL", "").strip()
YOUTUBE_FEATURED_TITLE = os.getenv("YOUTUBE_FEATURED_TITLE", "").strip()

# RSS sources (you can add more later)
# If you set FEED_URLS env, it will override this default list.
DEFAULT_FEED_URLS = [
    # Blue's News (you gave this one)
    "https://www.bluesnews.com/news/news_1_0.rdf",
    # IGN Games RSS (from awesome-rss-feeds)
    "https://www.ign.com/rss/v2/articles/feed?categories=games",
    # GameSpot RSS (from awesome-rss-feeds)
    "https://www.gamespot.com/feeds/mashup/",
    # GameRant (commonly /feed; some environments block it but it often works in Actions)
    "https://gamerant.com/feed",
    # Polygon (all)
    "https://www.polygon.com/rss/index.xml",
    # VGC (commonly /feed)
    "https://www.videogameschronicle.com/feed/",
    # Gematsu (commonly /feed)
    "https://www.gematsu.com/feed",
]

FEED_URLS = []
_env_feeds = os.getenv("FEED_URLS", "").strip()
if _env_feeds:
    FEED_URLS = [f.strip() for f in _env_feeds.splitlines() if f.strip()]
else:
    FEED_URLS = DEFAULT_FEED_URLS

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/rss+xml, application/xml;q=0.9, text/xml;q=0.9, */*;q=0.8",
}

# Files for “post only once per day”
DIGEST_POST_STATE_FILE = "digest_post_state.json"


# =========================
# DATA MODEL
# =========================

@dataclass
class Item:
    title: str
    url: str
    source: str
    published: datetime
    summary: str = ""
    image_url: str = ""
    tags: List[str] = None


# =========================
# HELPERS
# =========================

def log(msg: str):
    print(msg, flush=True)

def safe_get(url: str, timeout: int = 25) -> requests.Response:
    resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
    return resp

def normalize_url(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return u
    # Drop tracking query params (light normalization)
    try:
        parsed = urlparse(u)
        clean = parsed._replace(query="")
        return clean.geturl()
    except Exception:
        return u

def source_from_url(u: str) -> str:
    try:
        host = urlparse(u).netloc.lower()
        host = host.replace("www.", "")
        # short labels
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

def parse_rss_datetime(text: str) -> Optional[datetime]:
    if not text:
        return None
    t = text.strip()

    # Common RSS patterns: RFC 822, ISO-ish, etc.
    fmts = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
    ]
    for fmt in fmts:
        try:
            dt = datetime.strptime(t, fmt)
            # If naive, treat as UTC
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("UTC"))
            return dt.astimezone(ZoneInfo("UTC"))
        except Exception:
            continue

    # last resort: try dateutil if available
    try:
        from dateutil import parser
        dt = parser.parse(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(ZoneInfo("UTC"))
    except Exception:
        return None

def looks_like_non_news(title: str, summary: str) -> bool:
    """
    Your rules:
    - No deals / shopping / discounts
    - No lists like “Best X”
    - No polls/debates/opinion
    - No rumors/speculation
    - Keep video game / gaming-adjacent only
    """
    t = (title or "").lower()
    s = (summary or "").lower()
    blob = f"{t} {s}"

    bad_phrases = [
        "best ", "top ", "ranked", "ranking", "review:", "reviews:",
        "poll:", "debate:", "opinion", "letters", "mailbox",
        "deal", "deals", "discount", "% off", "price drop", "drops to $", "drops to",
        "black friday", "cyber monday", "woot", "amazon", "buy now",
        "power bank", "controller available", "accessories",
        "history of", "update)", "ultimate guide", "walkthrough",
        "rumor", "rumours", "leak", "leaked", "speculation", "reportedly", "might be",
        "could be", "possibly", "allegedly", "insider claims",
        "cosplay",  # tends to skew opinion pieces
        "how to draw", "disney", "walt disney world", "olaf",  # off-topic example you gave
    ]

    # quick reject if any bad phrase hits
    for p in bad_phrases:
        if p in blob:
            return True

    # Keep it “gaming + adjacent”: allow game/platform/hardware/industry terms
    gaming_signals = [
        "game", "gaming", "xbox", "playstation", "ps5", "ps4", "nintendo", "switch",
        "pc", "steam", "epic", "ubisoft", "ea", "bethesda", "blizzard", "activision",
        "sony", "microsoft", "valve", "riot", "unity", "unreal", "studio", "developer",
        "patch", "update", "launch", "reveal", "announced", "announcement", "trailer",
        "release date", "release", "dlc", "expansion", "demo", "early access",
        "next fest", "game pass", "ps plus", "marathon", "no man's sky",
        "hardware", "gpu", "nvidia", "amd", "intel", "console",
    ]

    if not any(sig in blob for sig in gaming_signals):
        return True

    return False

def extract_meta_summary_and_image(url: str) -> Tuple[str, str]:
    """
    Fetch the article and try to extract:
    - meta description (or og:description)
    - og:image (thumbnail)
    This is best-effort and failures should not break the digest.
    """
    try:
        resp = safe_get(url, timeout=18)
        if resp.status_code != 200 or not resp.text:
            return "", ""
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")

        # description
        desc = ""
        ogd = soup.find("meta", attrs={"property": "og:description"})
        if ogd and ogd.get("content"):
            desc = ogd["content"].strip()

        if not desc:
            md = soup.find("meta", attrs={"name": "description"})
            if md and md.get("content"):
                desc = md["content"].strip()

        # image
        img = ""
        ogi = soup.find("meta", attrs={"property": "og:image"})
        if ogi and ogi.get("content"):
            img = ogi["content"].strip()

        return desc, img
    except Exception:
        return "", ""

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
        ("ubisoft", "#Ubisoft"),
        ("ea", "#EA"),
        ("bethesda", "#Bethesda"),
        ("blizzard", "#Blizzard"),
        ("activision", "#Activision"),
        ("marathon", "#Marathon"),
        ("no man's sky", "#NoMansSky"),
        ("arc raiders", "#ArcRaiders"),
    ]

    for key, tag in mapping:
        if key in blob and tag not in tags:
            tags.append(tag)

    # Keep it tidy
    return tags[:4]

def short_source_link(source: str, url: str) -> str:
    return f"Source: {source} — {url}"

def md_escape(s: str) -> str:
    return (s or "").replace("_", "\\_")

def now_local() -> datetime:
    return datetime.now(ZoneInfo(DIGEST_GUARD_TZ))

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
        log(
            f"[GUARD] OK. Local now: {nl.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            f"Closest target: {closest.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            f"Delta={delta_min:.1f}min <= {DIGEST_GUARD_WINDOW_MINUTES}min"
        )
        return True

    log(
        f"[GUARD] Not within posting window. Local now: {nl.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
        f"Closest target: {closest.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
        f"Delta={delta_min:.1f}min > {DIGEST_GUARD_WINDOW_MINUTES}min. Exiting without posting."
    )
    return False

def should_skip_already_posted_today() -> bool:
    """
    Prevents double posts when we run cron frequently.
    Uses local date (America/Los_Angeles).
    """
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

def fetch_feed_items(feed_url: str) -> List[Item]:
    log(f"[RSS] GET {feed_url}")
    resp = safe_get(feed_url, timeout=25)
    resp.raise_for_status()
    xml = resp.text

    soup = BeautifulSoup(xml, "xml")

    # RSS 2.0
    items = soup.find_all("item")
    out: List[Item] = []

    for it in items[:50]:
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()

        # Some feeds use <guid>
        if not link:
            link = (it.findtext("guid") or "").strip()

        pub = (it.findtext("pubDate") or it.findtext("dc:date") or it.findtext("published") or "").strip()
        dt = parse_rss_datetime(pub) or datetime.now(ZoneInfo("UTC"))

        desc = (it.findtext("description") or "").strip()

        url = normalize_url(link)
        if not url or not title:
            continue

        source = source_from_url(url)
        out.append(Item(title=title, url=url, source=source, published=dt, summary=desc, tags=[]))

    return out

def dedupe_items(items: List[Item]) -> List[Item]:
    seen = set()
    out = []
    for it in items:
        key = normalize_url(it.url).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out

def pick_top(items: List[Item], top_n: int, max_per_source: int) -> List[Item]:
    """
    Sort by published desc, then enforce per-source cap to keep variety.
    """
    items = sorted(items, key=lambda x: x.published, reverse=True)
    picked: List[Item] = []
    per_source: Dict[str, int] = {}

    for it in items:
        if len(picked) >= top_n:
            break
        c = per_source.get(it.source, 0)
        if c >= max_per_source:
            continue
        picked.append(it)
        per_source[it.source] = c + 1

    # If we didn't fill, relax the per-source cap
    if len(picked) < top_n:
        for it in items:
            if len(picked) >= top_n:
                break
            if it in picked:
                continue
            picked.append(it)

    return picked[:top_n]

def clean_summary(text: str) -> str:
    # Strip HTML and keep it short-ish
    if not text:
        return ""
    try:
        soup = BeautifulSoup(text, "html.parser")
        s = soup.get_text(" ", strip=True)
    except Exception:
        s = text

    s = re.sub(r"\s+", " ", s).strip()

    # hard cap
    if len(s) > 260:
        s = s[:257].rstrip() + "…"
    return s

def adilo_latest_watch_url() -> str:
    """
    Best-effort:
    1) If FEATURED_VIDEO_FORCE_ID set -> use it.
    2) Scrape ADILO_PUBLIC_LATEST_PAGE and try to find newest id.
    3) fallback -> FEATURED_VIDEO_FALLBACK_URL
    """
    if FEATURED_VIDEO_FORCE_ID:
        watch = f"https://adilo.bigcommand.com/watch/{FEATURED_VIDEO_FORCE_ID}"
        log(f"[ADILO] Using FEATURED_VIDEO_FORCE_ID: {watch}")
        return watch

    # scrape latest page
    try:
        for attempt in range(1, 4):
            log(f"[ADILO] SCRAPE {ADILO_PUBLIC_LATEST_PAGE} attempt={attempt}")
            resp = safe_get(ADILO_PUBLIC_LATEST_PAGE, timeout=18)
            log(f"[ADILO] SCRAPE status={resp.status_code}")
            if resp.status_code != 200 or not resp.text:
                time.sleep(1.2)
                continue

            html = resp.text
            # Find patterns like: video?id=XXXX or watch/XXXX
            m = re.search(r"video\?id=([A-Za-z0-9_-]+)", html)
            if m:
                vid = m.group(1)
                watch = f"https://adilo.bigcommand.com/watch/{vid}"
                log(f"[ADILO] Found latest via video?id=: {watch}")
                return watch

            m2 = re.search(r"https://adilo\.bigcommand\.com/watch/([A-Za-z0-9_-]+)", html)
            if m2:
                vid = m2.group(1)
                watch = f"https://adilo.bigcommand.com/watch/{vid}"
                log(f"[ADILO] Found latest via watch/: {watch}")
                return watch

            time.sleep(1.2)

    except Exception as e:
        log(f"[ADILO] SCRAPE failed: {e}")

    log(f"[ADILO] Falling back to: {FEATURED_VIDEO_FALLBACK_URL}")
    return FEATURED_VIDEO_FALLBACK_URL

def discord_post(content: str, embeds: Optional[List[Dict]] = None):
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL")

    payload = {"content": content}
    if embeds:
        payload["embeds"] = embeds

    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, headers={"User-Agent": USER_AGENT}, timeout=25)
    r.raise_for_status()

def build_digest_message(items: List[Item]) -> Tuple[str, List[Dict]]:
    # Header
    date_line = now_local().strftime("%B %d, %Y")
    header = f"**{date_line}**\n\n**In Tonight’s Edition of {NEWSLETTER_NAME}…**\n"

    # Teaser bullets (3)
    teaser = ""
    for it in items[:3]:
        teaser += f"► 🎮 {md_escape(it.title)}\n"

    intro = f"\n{teaser}\n"

    # Body
    body = "## Tonight’s Top Stories\n\n"

    embeds: List[Dict] = []

    for idx, it in enumerate(items, start=1):
        # best-effort: summary + image
        feed_sum = clean_summary(it.summary)
        desc, img = ("", "")
        if not feed_sum or len(feed_sum) < 40:
            desc, img = extract_meta_summary_and_image(it.url)
        if not feed_sum:
            feed_sum = clean_summary(desc)

        it.summary = feed_sum
        if img:
            it.image_url = img

        it.tags = build_tags(it.title, it.summary)

        tag_line = ""
        if it.tags:
            tag_line = " " + " ".join(it.tags)

        body += f"**{idx}) {md_escape(it.title)}**{tag_line}\n"
        if it.summary:
            body += f"{md_escape(it.summary)}\n"
        body += f"{short_source_link(it.source, it.url)}\n\n"

        # Add an embed per story if we have an image
        if it.image_url:
            embeds.append({
                "title": it.title,
                "url": it.url,
                "image": {"url": it.image_url},
                "footer": {"text": f"{it.source}"},
            })

    # Featured video section (YouTube above Adilo, as you wanted)
    yt_block = ""
    if YOUTUBE_FEATURED_URL:
        yt_title = YOUTUBE_FEATURED_TITLE or "Latest episode on YouTube"
        yt_block = f"## ▶️ YouTube (same episode)\n**{md_escape(yt_title)}**\n{YOUTUBE_FEATURED_URL}\n\n"

    adilo_url = adilo_latest_watch_url()
    adilo_block = f"## 📺 Featured Video (Adilo)\n**{md_escape(FEATURED_VIDEO_TITLE)}**\n{adilo_url}\n\n"

    signoff = "—\nThat’s it for tonight’s Itty Bitty.\nCatch the snackable breakdown tomorrow.\n"

    full = header + intro + body + yt_block + adilo_block + signoff
    return full, embeds[:10]  # Discord embed cap safety

def main():
    # Guard logic
    if should_skip_already_posted_today():
        return

    if not guard_should_post_now():
        # Exit cleanly so Actions doesn't show failure
        return

    # Fetch RSS
    window_utc = datetime.now(ZoneInfo("UTC")) - timedelta(hours=DIGEST_WINDOW_HOURS)

    all_items: List[Item] = []
    for feed in FEED_URLS:
        try:
            items = fetch_feed_items(feed)
            all_items.extend(items)
        except Exception as e:
            log(f"[RSS] Feed failed: {feed} ({e})")

    if not all_items:
        log("[DIGEST] No feed items fetched. Exiting without posting.")
        return

    # Window filter
    all_items = [it for it in all_items if it.published >= window_utc]

    # Remove non-news
    filtered: List[Item] = []
    for it in all_items:
        if looks_like_non_news(it.title, it.summary):
            continue
        filtered.append(it)

    filtered = dedupe_items(filtered)

    if not filtered:
        log("[DIGEST] No items after filtering. Exiting without posting.")
        return

    top = pick_top(filtered, DIGEST_TOP_N, DIGEST_MAX_PER_SOURCE)

    msg, embeds = build_digest_message(top)

    # Post to Discord
    discord_post(msg, embeds=embeds)

    # Mark posted today (prevents doubles when cron runs often)
    mark_posted_today()

    log(f"Digest posted. Items: {len(top)}")


if __name__ == "__main__":
    main()
