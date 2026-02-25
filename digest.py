#!/usr/bin/env python3
import os
import re
import json
import time
import html
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Dict, Any, Optional, Tuple

import requests
import feedparser
from bs4 import BeautifulSoup

# =========================================================
# SETTINGS / ENV
# =========================================================

DEFAULT_FEEDS = [
    "https://www.bluesnews.com/news/news_1_0.rdf",
    "https://www.ign.com/rss/v2/articles/feed?categories=games",
    "https://www.gamespot.com/feeds/mashup/",
    "https://gamerant.com/feed",
    "https://www.polygon.com/rss/index.xml",
    "https://www.videogameschronicle.com/feed/",
    "https://www.gematsu.com/feed",
]

CACHE_PATH = ".digest_cache.json"
USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNews/Digest").strip()

NEWSLETTER_NAME = os.getenv("NEWSLETTER_NAME", "Itty Bitty Gaming News").strip()
NEWSLETTER_TAGLINE = os.getenv("NEWSLETTER_TAGLINE", "Snackable daily gaming news — five days a week.").strip()

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

DIGEST_WINDOW_HOURS = int(os.getenv("DIGEST_WINDOW_HOURS", "24").strip())
DIGEST_TOP_N = int(os.getenv("DIGEST_TOP_N", "5").strip())
DIGEST_MAX_PER_SOURCE = int(os.getenv("DIGEST_MAX_PER_SOURCE", "1").strip())

DIGEST_FORCE_POST = os.getenv("DIGEST_FORCE_POST", "").strip().lower() in ("1", "true", "yes", "y")

DIGEST_GUARD_TZ = os.getenv("DIGEST_GUARD_TZ", "America/Los_Angeles").strip()
DIGEST_GUARD_LOCAL_HOUR = int(os.getenv("DIGEST_GUARD_LOCAL_HOUR", "19").strip())
DIGEST_GUARD_LOCAL_MINUTE = int(os.getenv("DIGEST_GUARD_LOCAL_MINUTE", "0").strip())
DIGEST_GUARD_WINDOW_MINUTES = int(os.getenv("DIGEST_GUARD_WINDOW_MINUTES", "120").strip())

# YouTube
YOUTUBE_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "").strip()
YOUTUBE_RSS_URL = os.getenv("YOUTUBE_RSS_URL", "").strip()

# Adilo public pages
ADILO_PUBLIC_LATEST_PAGE = os.getenv(
    "ADILO_PUBLIC_LATEST_PAGE",
    "https://adilo.bigcommand.com/c/ittybittygamingnews/video"
).strip()
ADILO_PUBLIC_HOME_PAGE = os.getenv(
    "ADILO_PUBLIC_HOME_PAGE",
    "https://adilo.bigcommand.com/c/ittybittygamingnews/home"
).strip()

# DO NOT set this unless you want to force a specific video
FEATURED_VIDEO_FORCE_ID = os.getenv("FEATURED_VIDEO_FORCE_ID", "").strip()

FEED_URLS_ENV = os.getenv("FEED_URLS", "").strip()

# =========================================================
# DATA
# =========================================================

@dataclass
class FeedItem:
    title: str
    url: str
    source: str
    published: Optional[datetime]
    summary: str


# =========================================================
# HELPERS
# =========================================================

def http_get(url: str, timeout: int = 20) -> requests.Response:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    return requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)

def load_cache() -> Dict[str, Any]:
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def save_cache(cache: Dict[str, Any]) -> None:
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)

def now_local() -> datetime:
    return datetime.now(ZoneInfo(DIGEST_GUARD_TZ))

def guard_should_post_now() -> bool:
    if DIGEST_FORCE_POST:
        print("[GUARD] DIGEST_FORCE_POST enabled — bypassing time guard.")
        return True

    tz = ZoneInfo(DIGEST_GUARD_TZ)
    n = datetime.now(tz)

    target_today = n.replace(
        hour=DIGEST_GUARD_LOCAL_HOUR,
        minute=DIGEST_GUARD_LOCAL_MINUTE,
        second=0,
        microsecond=0,
    )

    candidates = [target_today - timedelta(days=1), target_today, target_today + timedelta(days=1)]
    closest = min(candidates, key=lambda t: abs((n - t).total_seconds()))
    delta_min = abs((n - closest).total_seconds()) / 60.0

    if delta_min <= DIGEST_GUARD_WINDOW_MINUTES:
        print(
            f"[GUARD] OK. Local now: {n.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            f"Closest target: {closest.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            f"Delta={delta_min:.1f}min <= {DIGEST_GUARD_WINDOW_MINUTES}min"
        )
        return True

    print(
        f"[GUARD] Not within posting window. Local now: {n.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
        f"Closest target: {closest.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
        f"Delta={delta_min:.1f}min > {DIGEST_GUARD_WINDOW_MINUTES}min. Exiting without posting."
    )
    return False

def clean_text(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s

def shorten(s: str, max_len: int) -> str:
    s = clean_text(s)
    if len(s) <= max_len:
        return s
    return s[: max_len - 1].rstrip() + "…"

def parse_dt(entry: Any) -> Optional[datetime]:
    for key in ("published_parsed", "updated_parsed"):
        st = getattr(entry, key, None)
        if st:
            try:
                return datetime(*st[:6], tzinfo=ZoneInfo("UTC"))
            except Exception:
                pass
    return None

def normalize_source(url: str) -> str:
    try:
        m = re.search(r"https?://([^/]+)/", url)
        if not m:
            return url
        host = m.group(1).lower().replace("www.", "")
        return host
    except Exception:
        return url

def story_key(title: str, url: str) -> str:
    base = (title.strip().lower() + "|" + url.strip().lower()).encode("utf-8", errors="ignore")
    return hashlib.sha1(base).hexdigest()

# =========================================================
# RSS
# =========================================================

def get_feed_urls() -> List[str]:
    if FEED_URLS_ENV:
        parts = []
        for line in FEED_URLS_ENV.replace(",", "\n").splitlines():
            line = line.strip()
            if line:
                parts.append(line)
        return parts
    return DEFAULT_FEEDS

def fetch_feed_items() -> List[FeedItem]:
    feeds = get_feed_urls()
    window_start = datetime.now(ZoneInfo("UTC")) - timedelta(hours=DIGEST_WINDOW_HOURS)

    items: List[FeedItem] = []
    for url in feeds:
        try:
            print(f"[RSS] GET {url}")
            fp = feedparser.parse(url)
            if getattr(fp, "bozo", 0) == 1:
                print(f"[RSS] bozo=1 for {url}: {getattr(fp, 'bozo_exception', '')}")

            for e in fp.entries or []:
                link = getattr(e, "link", "") or ""
                title = clean_text(getattr(e, "title", "") or "")
                if not link or not title:
                    continue

                published = parse_dt(e)
                if published and published < window_start:
                    continue

                source = normalize_source(link)

                raw_sum = getattr(e, "summary", "") or getattr(e, "description", "") or ""
                raw_sum = BeautifulSoup(str(raw_sum), "html.parser").get_text(" ")
                summary = shorten(raw_sum, 260)

                items.append(FeedItem(title=title, url=link, source=source, published=published, summary=summary))
        except Exception as ex:
            print(f"[RSS] Feed failed: {url} ({ex})")

    # Dedup
    seen = set()
    out: List[FeedItem] = []
    for it in items:
        k = story_key(it.title, it.url)
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out

def select_top_items(items: List[FeedItem]) -> List[FeedItem]:
    def sort_key(it: FeedItem):
        t = it.published or datetime(1970, 1, 1, tzinfo=ZoneInfo("UTC"))
        return (t, it.title.lower())

    items = sorted(items, key=sort_key, reverse=True)

    per_source: Dict[str, int] = {}
    selected: List[FeedItem] = []
    for it in items:
        if per_source.get(it.source, 0) >= DIGEST_MAX_PER_SOURCE:
            continue
        selected.append(it)
        per_source[it.source] = per_source.get(it.source, 0) + 1
        if len(selected) >= DIGEST_TOP_N:
            break
    return selected

# =========================================================
# YOUTUBE LATEST
# =========================================================

def youtube_latest_url_title() -> Tuple[str, str]:
    rss = YOUTUBE_RSS_URL
    if not rss and YOUTUBE_CHANNEL_ID:
        rss = f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}"
    if not rss:
        return ("", "")

    try:
        print(f"[YT] Fetch RSS: {rss}")
        fp = feedparser.parse(rss)
        if not fp.entries:
            return ("", "")
        e = fp.entries[0]
        url = getattr(e, "link", "") or ""
        title = clean_text(getattr(e, "title", "") or "")
        return (url, title)
    except Exception as ex:
        print(f"[YT] RSS failed: {ex}")
        return ("", "")

# =========================================================
# ADILO LATEST (PUBLIC SCRAPE)
# =========================================================

ADILO_ID_RE = re.compile(r"(?:/watch/|video\?id=|/stage/videos/)([A-Za-z0-9_\-]{6,})")

def adilo_watch_url_from_id(vid: str) -> str:
    return f"https://adilo.bigcommand.com/watch/{vid}"

def adilo_scrape_candidates(html_text: str) -> List[str]:
    ids = []
    for m in ADILO_ID_RE.finditer(html_text or ""):
        ids.append(m.group(1))
    seen = set()
    out = []
    for x in ids:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out

def adilo_best_latest_url() -> str:
    # If forced, use it (you do NOT want this normally)
    if FEATURED_VIDEO_FORCE_ID:
        forced = adilo_watch_url_from_id(FEATURED_VIDEO_FORCE_ID)
        print(f"[ADILO] Using FEATURED_VIDEO_FORCE_ID: {forced}")
        return forced

    base = ADILO_PUBLIC_LATEST_PAGE.rstrip("/")
    cb = str(int(time.time() * 1000))

    # These are the variants that have historically produced IDs even when /video is JS-heavy
    probe_urls = [
        f"{base}?cb={cb}",
        base,
        f"{base}?id=&cb={cb}",
        f"{base}?id=&_={cb}",           # alt cache-buster
        f"{base}?id=latest&cb={cb}",    # sometimes works
        f"{base}?video=latest&cb={cb}", # sometimes works
    ]

    html_text = ""
    for u in probe_urls:
        try:
            print(f"[ADILO] SCRAPE attempt=1 timeout=25 url={u}")
            r = http_get(u, timeout=25)
            if r.status_code == 200 and r.text:
                html_text = r.text
                ids = adilo_scrape_candidates(html_text)
                if ids:
                    best = adilo_watch_url_from_id(ids[0])
                    print(f"[ADILO] Found candidate id={ids[0]} -> {best}")
                    return best
        except requests.exceptions.Timeout:
            print(f"[ADILO] Timeout url={u} (timeout=25)")
        except Exception as ex:
            print(f"[ADILO] SCRAPE error url={u}: {ex}")

    # If we got here, /video didn’t contain any IDs (often JS-rendered).
    # Try the slower-but-more-reliable "video?id=" pattern with longer timeout and retries.
    slow_urls = [
        f"{base}?id=&cb={cb}",
        f"{base}?id=&cb={cb}&n=1",
        f"{base}?id=&cb={cb}&n=2",
    ]

    for u in slow_urls:
        try:
            print(f"[ADILO] SCRAPE attempt=2 timeout=45 url={u}")
            r = http_get(u, timeout=45)
            if r.status_code == 200 and r.text:
                ids = adilo_scrape_candidates(r.text)
                if ids:
                    best = adilo_watch_url_from_id(ids[0])
                    print(f"[ADILO] Found candidate id={ids[0]} -> {best}")
                    return best
        except requests.exceptions.Timeout:
            print(f"[ADILO] Timeout url={u} (timeout=45)")
        except Exception as ex:
            print(f"[ADILO] SCRAPE error url={u}: {ex}")

    # Final fallback: link the latest page itself (better than dumping to home)
    print(f"[ADILO] No IDs found on latest page; falling back to latest page link: {base}")
    return base

# =========================================================
# DISCORD POST
# =========================================================

def discord_post(content: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL")

    payload: Dict[str, Any] = {"content": content}

    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, headers={"User-Agent": USER_AGENT}, timeout=25)
    r.raise_for_status()

# =========================================================
# MAIN
# =========================================================

def main() -> None:
    if not guard_should_post_now():
        return

    cache = load_cache()

    items_all = fetch_feed_items()
    if not items_all:
        print("[DIGEST] No feed items fetched. Exiting without posting.")
        return

    items = select_top_items(items_all)
    if not items:
        print("[DIGEST] No items selected. Exiting without posting.")
        return

    bullets = [it.title for it in items[:3]]
    date_str = now_local().strftime("%B %d, %Y")

    yt_url, yt_title = youtube_latest_url_title()
    adilo_url = adilo_best_latest_url()

    # Build message where:
    # - story link sits directly under its story
    # - YouTube comes before Adilo
    # - links are on their own lines for Discord to auto-unfurl
    lines = []
    lines.append(NEWSLETTER_TAGLINE)
    lines.append("")
    lines.append(date_str)
    lines.append("")
    lines.append(f"In Tonight’s Edition of {NEWSLETTER_NAME}…")
    for b in bullets[:3]:
        lines.append(f"► 🎮 {b}")
    lines.append("")
    lines.append("Tonight’s Top Stories")
    lines.append("")

    # Stories with links directly beneath
    for idx, it in enumerate(items, start=1):
        lines.append(f"{idx}) {it.title}")
        if it.summary:
            lines.append(shorten(it.summary, 260))
        lines.append(f"Source: {it.source}")
        lines.append(it.url)  # put URL alone on a line for unfurl
        lines.append("")

    # Featured video section
    if yt_url or adilo_url:
        if yt_url:
            lines.append("▶️ YouTube (latest)")
            # Put URL alone on a line for the playable preview
            lines.append(yt_url)
            if yt_title:
                lines.append(yt_title)
            lines.append("")
        if adilo_url:
            lines.append("📺 Adilo (latest)")
            # Put URL alone on a line for card/preview
            lines.append(adilo_url)
            lines.append("")

    content = "\n".join([x for x in lines if x is not None]).strip() + "\n"

    # Discord hard limit: 2000 chars
    if len(content) > 2000:
        # Keep the videos intact; trim earlier story summaries first by dropping summaries entirely if needed.
        # Fast approach: rebuild without summaries.
        lines2 = []
        lines2.append(NEWSLETTER_TAGLINE)
        lines2.append("")
        lines2.append(date_str)
        lines2.append("")
        lines2.append(f"In Tonight’s Edition of {NEWSLETTER_NAME}…")
        for b in bullets[:3]:
            lines2.append(f"► 🎮 {b}")
        lines2.append("")
        lines2.append("Tonight’s Top Stories")
        lines2.append("")
        for idx, it in enumerate(items, start=1):
            lines2.append(f"{idx}) {it.title}")
            lines2.append(f"Source: {it.source}")
            lines2.append(it.url)
            lines2.append("")
        if yt_url:
            lines2.append("▶️ YouTube (latest)")
            lines2.append(yt_url)
            lines2.append("")
        if adilo_url:
            lines2.append("📺 Adilo (latest)")
            lines2.append(adilo_url)
            lines2.append("")
        content = "\n".join(lines2).strip() + "\n"
        if len(content) > 2000:
            content = content[:1990] + "…\n"

    discord_post(content)

    cache["last_youtube_url"] = yt_url
    cache["last_adilo_url"] = adilo_url
    cache["last_run_local"] = now_local().isoformat()
    save_cache(cache)

    print("[DONE] Digest posted.")
    print(f"[DONE] YouTube: {yt_url}")
    print(f"[DONE] Featured Adilo video: {adilo_url}")


if __name__ == "__main__":
    main()
