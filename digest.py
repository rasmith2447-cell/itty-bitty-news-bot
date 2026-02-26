#!/usr/bin/env python3
import os
import re
import json
import time
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Dict, Any, Optional, Tuple

import requests
import feedparser
from bs4 import BeautifulSoup

# =========================================================
# ENV / SETTINGS
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

# Optional hard force (leave blank for normal operation)
FEATURED_VIDEO_FORCE_ID = os.getenv("FEATURED_VIDEO_FORCE_ID", "").strip()

FEED_URLS_ENV = os.getenv("FEED_URLS", "").strip()

# =========================================================
# TYPES
# =========================================================

@dataclass
class FeedItem:
    title: str
    url: str
    source: str
    published: Optional[datetime]
    summary: str

# =========================================================
# BASIC HELPERS
# =========================================================

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

def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def shorten(s: str, max_len: int) -> str:
    s = clean_text(s)
    if len(s) <= max_len:
        return s
    return s[: max_len - 1].rstrip() + "…"

def normalize_source(url: str) -> str:
    m = re.search(r"https?://([^/]+)/", url or "")
    if not m:
        return url
    host = m.group(1).lower().replace("www.", "")
    return host

def story_key(title: str, url: str) -> str:
    base = (title.strip().lower() + "|" + url.strip().lower()).encode("utf-8", errors="ignore")
    return hashlib.sha1(base).hexdigest()

def parse_dt(entry: Any) -> Optional[datetime]:
    for key in ("published_parsed", "updated_parsed"):
        st = getattr(entry, key, None)
        if st:
            try:
                return datetime(*st[:6], tzinfo=ZoneInfo("UTC"))
            except Exception:
                pass
    return None

def http_get(url: str, timeout: int = 25) -> requests.Response:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    return requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)

def now_local() -> datetime:
    return datetime.now(ZoneInfo(DIGEST_GUARD_TZ))

# =========================================================
# GUARD
# =========================================================

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
# YOUTUBE
# =========================================================

YOUTUBE_WATCH_RE = re.compile(r"(?:v=|youtu\.be/)([A-Za-z0-9_\-]{6,})")

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

        # Normalize to youtu.be (Discord tends to unfurl consistently)
        m = YOUTUBE_WATCH_RE.search(url)
        if m:
            vid = m.group(1)
            url = f"https://youtu.be/{vid}"

        return (url, title)
    except Exception as ex:
        print(f"[YT] RSS failed: {ex}")
        return ("", "")

# =========================================================
# ADILO (public scrape)
# =========================================================

ADILO_ID_RE = re.compile(r"(?:/watch/|video\?id=|/stage/videos/)([A-Za-z0-9_\-]{6,})")

def adilo_watch_url_from_id(vid: str) -> str:
    return f"https://adilo.bigcommand.com/watch/{vid}"

def _recursive_find_ids(obj: Any, out: List[str]) -> None:
    if obj is None:
        return
    if isinstance(obj, str):
        for m in ADILO_ID_RE.finditer(obj):
            out.append(m.group(1))
        return
    if isinstance(obj, dict):
        for _, v in obj.items():
            _recursive_find_ids(v, out)
        return
    if isinstance(obj, list):
        for v in obj:
            _recursive_find_ids(v, out)
        return

def adilo_scrape_ids_from_html(html_text: str) -> List[str]:
    ids: List[str] = []

    # 1) direct regex scan
    for m in ADILO_ID_RE.finditer(html_text or ""):
        ids.append(m.group(1))

    # 2) __NEXT_DATA__ JSON (common for React/Next apps)
    try:
        soup = BeautifulSoup(str(html_text), "html.parser")
        script = soup.find("script", id="__NEXT_DATA__")
        if script and script.string:
            data = json.loads(script.string)
            _recursive_find_ids(data, ids)
    except Exception:
        pass

    # de-dupe preserve order
    seen = set()
    out = []
    for x in ids:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out

def adilo_best_latest_url() -> str:
    if FEATURED_VIDEO_FORCE_ID:
        forced = adilo_watch_url_from_id(FEATURED_VIDEO_FORCE_ID)
        print(f"[ADILO] Using FEATURED_VIDEO_FORCE_ID: {forced}")
        return forced

    base = ADILO_PUBLIC_LATEST_PAGE.rstrip("/")
    cb = str(int(time.time() * 1000))

    probe_urls = [
        f"{base}?cb={cb}",
        base,
        f"{base}?id=&cb={cb}",
        f"{base}?id=&_={cb}",
        f"{base}?id=latest&cb={cb}",
        f"{base}?video=latest&cb={cb}",
    ]

    for u in probe_urls:
        try:
            print(f"[ADILO] SCRAPE attempt=1 timeout=25 url={u}")
            r = http_get(u, timeout=25)
            if r.status_code == 200 and r.text:
                ids = adilo_scrape_ids_from_html(r.text)
                if ids:
                    best = adilo_watch_url_from_id(ids[0])
                    print(f"[ADILO] Found candidate id={ids[0]} -> {best}")
                    return best
        except requests.exceptions.Timeout:
            print(f"[ADILO] Timeout url={u} (timeout=25)")
        except Exception as ex:
            print(f"[ADILO] SCRAPE error url={u}: {ex}")

    # slower retry
    slow_urls = [
        f"{base}?id=&cb={cb}&n=1",
        f"{base}?id=&cb={cb}&n=2",
        f"{base}?id=&cb={cb}&n=3",
    ]
    for u in slow_urls:
        try:
            print(f"[ADILO] SCRAPE attempt=2 timeout=45 url={u}")
            r = http_get(u, timeout=45)
            if r.status_code == 200 and r.text:
                ids = adilo_scrape_ids_from_html(r.text)
                if ids:
                    best = adilo_watch_url_from_id(ids[0])
                    print(f"[ADILO] Found candidate id={ids[0]} -> {best}")
                    return best
        except requests.exceptions.Timeout:
            print(f"[ADILO] Timeout url={u} (timeout=45)")
        except Exception as ex:
            print(f"[ADILO] SCRAPE error url={u}: {ex}")

    print(f"[ADILO] No IDs found; falling back to latest page link: {base}")
    return base

# =========================================================
# DISCORD (embeds)
# =========================================================

def discord_post(content: str, embeds: List[Dict[str, Any]]) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL")

    # Discord limits
    # - content: 2000 chars
    # - embeds: max 10
    # - embed.description: 4096
    # - embed.title: 256
    # We'll keep it safe.
    content = (content or "").strip()
    if len(content) > 2000:
        content = content[:1990] + "…"

    embeds = embeds[:10]

    payload: Dict[str, Any] = {"content": content, "embeds": embeds}

    r = requests.post(
        DISCORD_WEBHOOK_URL,
        json=payload,
        headers={"User-Agent": USER_AGENT},
        timeout=25
    )
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

    date_str = now_local().strftime("%B %d, %Y")

    yt_url, yt_title = youtube_latest_url_title()
    adilo_url = adilo_best_latest_url()

    # Content header ONLY (no story URLs in content)
    bullets = [it.title for it in items[:3]]
    header_lines = []
    header_lines.append(NEWSLETTER_TAGLINE)
    header_lines.append("")
    header_lines.append(date_str)
    header_lines.append("")
    header_lines.append(f"In Tonight’s Edition of {NEWSLETTER_NAME}…")
    for b in bullets[:3]:
        header_lines.append(f"► 🎮 {b}")
    header_lines.append("")
    header_lines.append("Tonight’s Top Stories")
    header = "\n".join(header_lines).strip()

    embeds: List[Dict[str, Any]] = []

    # Story embeds (cards appear under their story)
    for idx, it in enumerate(items, start=1):
        embeds.append({
            "title": f"{idx}) {shorten(it.title, 256)}",
            "url": it.url,
            "description": shorten(it.summary, 260) if it.summary else "",
            "footer": {"text": f"Source: {it.source}"},
        })

    # Video embeds (YouTube first)
    if yt_url:
        embeds.append({
            "title": "▶️ YouTube (latest)",
            "url": yt_url,
            "description": shorten(yt_title, 200) if yt_title else "",
        })

    if adilo_url:
        embeds.append({
            "title": "📺 Adilo (latest)",
            "url": adilo_url,
            "description": "Watch the latest episode on Adilo.",
        })

    discord_post(header, embeds)

    cache["last_youtube_url"] = yt_url
    cache["last_adilo_url"] = adilo_url
    cache["last_run_local"] = now_local().isoformat()
    save_cache(cache)

    print("[DONE] Digest posted.")
    print(f"[DONE] YouTube: {yt_url}")
    print(f"[DONE] Featured Adilo video: {adilo_url}")

if __name__ == "__main__":
    main()
