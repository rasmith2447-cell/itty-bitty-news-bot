import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Dict, Any, Optional, Tuple

import requests
import feedparser
from bs4 import BeautifulSoup


# =========================
# CONFIG
# =========================

DEFAULT_FEED_URLS = [
    "https://www.bluesnews.com/news/news_1_0.rdf",
    "https://www.ign.com/rss/v2/articles/feed?categories=games",
    "https://www.gamespot.com/feeds/mashup/",
    "https://gamerant.com/feed",
    "https://www.polygon.com/rss/index.xml",
    "https://www.videogameschronicle.com/feed/",
    "https://www.gematsu.com/feed",
]

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

DIGEST_WINDOW_HOURS = int(os.getenv("DIGEST_WINDOW_HOURS", "24").strip())
DIGEST_TOP_N = int(os.getenv("DIGEST_TOP_N", "5").strip())
DIGEST_MAX_PER_SOURCE = int(os.getenv("DIGEST_MAX_PER_SOURCE", "1").strip())

NEWSLETTER_NAME = os.getenv("NEWSLETTER_NAME", "Itty Bitty Gaming News").strip()
NEWSLETTER_TAGLINE = os.getenv("NEWSLETTER_TAGLINE", "Snackable daily gaming news — five days a week.").strip()
USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNews/Digest").strip()

# Guard
DIGEST_FORCE_POST = os.getenv("DIGEST_FORCE_POST", "").strip().lower() in ("1", "true", "yes", "y")
DIGEST_GUARD_TZ = os.getenv("DIGEST_GUARD_TZ", "America/Los_Angeles").strip()
DIGEST_GUARD_LOCAL_HOUR = int(os.getenv("DIGEST_GUARD_LOCAL_HOUR", "19").strip())
DIGEST_GUARD_LOCAL_MINUTE = int(os.getenv("DIGEST_GUARD_LOCAL_MINUTE", "0").strip())
DIGEST_GUARD_WINDOW_MINUTES = int(os.getenv("DIGEST_GUARD_WINDOW_MINUTES", "120").strip())

# YouTube
YOUTUBE_RSS_URL = os.getenv(
    "YOUTUBE_RSS_URL",
    "https://www.youtube.com/feeds/videos.xml?channel_id=UC0SJd4h7GQqoYTVjlDnSzqQ",
).strip()

# Adilo public pages
ADILO_PUBLIC_LATEST_PAGE = os.getenv(
    "ADILO_PUBLIC_LATEST_PAGE",
    "https://adilo.bigcommand.com/c/ittybittygamingnews/video",
).strip()
ADILO_PUBLIC_HOME_PAGE = os.getenv(
    "ADILO_PUBLIC_HOME_PAGE",
    "https://adilo.bigcommand.com/c/ittybittygamingnews/home",
).strip()

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


# =========================
# TIME GUARD
# =========================

def guard_should_post_now() -> bool:
    if DIGEST_FORCE_POST:
        print("[GUARD] DIGEST_FORCE_POST enabled — bypassing time guard.")
        return True

    tz = ZoneInfo(DIGEST_GUARD_TZ)
    now_local = datetime.now(tz)

    target_today = now_local.replace(
        hour=DIGEST_GUARD_LOCAL_HOUR,
        minute=DIGEST_GUARD_LOCAL_MINUTE,
        second=0,
        microsecond=0
    )

    candidates = [target_today - timedelta(days=1), target_today, target_today + timedelta(days=1)]
    closest = min(candidates, key=lambda t: abs((now_local - t).total_seconds()))
    delta_min = abs((now_local - closest).total_seconds()) / 60.0

    if delta_min <= DIGEST_GUARD_WINDOW_MINUTES:
        print(
            f"[GUARD] OK. Local now: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            f"Closest target: {closest.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            f"Delta={delta_min:.1f}min <= {DIGEST_GUARD_WINDOW_MINUTES}min"
        )
        return True

    print(
        f"[GUARD] Not within posting window. Local now: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
        f"Closest target: {closest.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
        f"Delta={delta_min:.1f}min > {DIGEST_GUARD_WINDOW_MINUTES}min. Exiting without posting."
    )
    return False


# =========================
# FEEDS
# =========================

def parse_feed_urls() -> List[str]:
    raw = os.getenv("FEED_URLS", "").strip()
    if raw:
        parts = [p.strip() for p in re.split(r"[\n,]+", raw) if p.strip()]
        return parts
    return DEFAULT_FEED_URLS


def safe_text(s: str) -> str:
    return (s or "").replace("\r", " ").replace("\n", " ").strip()


def domain_from_url(url: str) -> str:
    try:
        host = re.findall(r"https?://([^/]+)", url)[0].lower()
        return re.sub(r"^www\.", "", host)
    except Exception:
        return "source"


def parse_published_dt(raw: str) -> Optional[datetime]:
    if not raw:
        return None
    try:
        t = feedparser._parse_date(raw)
        if t:
            return datetime(*t[:6], tzinfo=ZoneInfo("UTC"))
    except Exception:
        pass
    return None


def fetch_feed_items(feed_url: str) -> List[Dict[str, Any]]:
    print(f"[RSS] GET {feed_url}")
    try:
        r = requests.get(feed_url, headers=HEADERS, timeout=25)
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
        if getattr(parsed, "bozo", 0) == 1:
            print(f"[RSS] bozo=1 for {feed_url}: {getattr(parsed, 'bozo_exception', '')}")

        out = []
        for e in parsed.entries:
            link = e.get("link") or ""
            title = safe_text(e.get("title") or "")
            summary = safe_text(e.get("summary") or e.get("description") or "")
            published = e.get("published") or e.get("updated") or ""

            if not link or not title:
                continue

            out.append({
                "title": title,
                "summary": summary,
                "url": link,
                "source": domain_from_url(link),
                "published_raw": published,
                "published_dt": parse_published_dt(published),
            })
        return out
    except Exception as ex:
        print(f"[RSS] Feed failed: {feed_url} ({ex})")
        return []


def dedupe_and_select(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # de-dupe by normalized title
    seen = set()
    deduped = []
    for it in items:
        key = re.sub(r"[^a-z0-9]+", " ", it["title"].lower()).strip()
        key = re.sub(r"\s+", " ", key)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)

    # sort newest first (missing dt -> old)
    def sk(it):
        dt = it.get("published_dt")
        return dt.timestamp() if dt else 0

    deduped.sort(key=sk, reverse=True)

    # limit per source
    per_source: Dict[str, int] = {}
    out: List[Dict[str, Any]] = []
    for it in deduped:
        src = it["source"]
        per_source[src] = per_source.get(src, 0) + 1
        if per_source[src] > DIGEST_MAX_PER_SOURCE:
            continue
        out.append(it)
        if len(out) >= DIGEST_TOP_N:
            break
    return out


# =========================
# YOUTUBE (standalone URL for “playable”)
# =========================

def fetch_latest_youtube_url() -> Optional[str]:
    try:
        print(f"[YT] Fetch RSS: {YOUTUBE_RSS_URL}")
        r = requests.get(YOUTUBE_RSS_URL, headers=HEADERS, timeout=25)
        r.raise_for_status()
        parsed = feedparser.parse(r.content)

        for e in parsed.entries:
            link = (e.get("link") or "").strip()
            if not link:
                continue
            # filter shorts
            if "/shorts/" in link:
                continue
            return link
    except Exception as ex:
        print(f"[YT] Failed to fetch RSS: {ex}")
    return None


# =========================
# ADILO (more reliable scrape + validate)
# =========================

def _adilo_extract_ids(html: str) -> List[str]:
    """
    Extract IDs in the order they appear.
    We accept IDs from:
      - video?id=<ID>
      - /watch/<ID>
      - /stage/videos/<ID>
    """
    ids: List[str] = []

    # Preserve order by scanning with a single regex that matches all three patterns.
    pattern = re.compile(
        r"(?:video\?id=|/watch/|/stage/videos/)([A-Za-z0-9_-]{6,})"
    )
    for m in pattern.finditer(html or ""):
        ids.append(m.group(1))

    return ids


def _adilo_watch_url(video_id: str) -> str:
    return f"https://adilo.bigcommand.com/watch/{video_id}"


def _adilo_is_valid_watch(url: str) -> bool:
    # Quick validation so we don’t post junk.
    try:
        # Some CDNs block HEAD; do GET with tiny timeout.
        r = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        if r.status_code >= 400:
            return False
        # Ensure we didn’t land back on home hub
        if "/c/ittybittygamingnews/home" in (r.url or ""):
            return False
        return True
    except Exception:
        return False


def scrape_adilo_latest_watch_url() -> str:
    """
    Tries multiple public URLs and cache-busters.
    Picks the BEST candidate by:
      - taking the LAST id found (usually newest)
      - validating /watch/<id> returns a real page
    """
    cb = int(time.time() * 1000)

    urls_to_try = [
        ADILO_PUBLIC_LATEST_PAGE,
        f"{ADILO_PUBLIC_LATEST_PAGE}?cb={cb}",
        f"{ADILO_PUBLIC_LATEST_PAGE}/?cb={cb}",
        f"{ADILO_PUBLIC_LATEST_PAGE}?video=latest&cb={cb}",
        # IMPORTANT: this sometimes appears in page source links
        f"{ADILO_PUBLIC_LATEST_PAGE}?id=&cb={cb}",
        # last resort: home
        ADILO_PUBLIC_HOME_PAGE,
    ]

    for u in urls_to_try:
        try:
            print(f"[ADILO] SCRAPE attempt=1 timeout=25 url={u}")
            r = requests.get(u, headers=HEADERS, timeout=25, allow_redirects=True)
            r.raise_for_status()
            html = r.text or ""

            ids = _adilo_extract_ids(html)
            if not ids:
                continue

            # Try candidates from newest-ish to oldest-ish
            # (we prefer later occurrences)
            for vid in reversed(ids):
                watch = _adilo_watch_url(vid)
                if _adilo_is_valid_watch(watch):
                    print(f"[ADILO] Found candidate id={vid} -> {watch}")
                    return watch

        except Exception as ex:
            print(f"[ADILO] SCRAPE failed: {ex}")

    print(f"[ADILO] Falling back: {ADILO_PUBLIC_HOME_PAGE}")
    return ADILO_PUBLIC_HOME_PAGE


# =========================
# DISCORD (multiple posts to get the “separated” layout)
# =========================

def discord_post(content: str = "", embeds: Optional[List[Dict[str, Any]]] = None) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL")

    payload: Dict[str, Any] = {
        "content": content or "",
        "allowed_mentions": {"parse": []},
    }
    if embeds:
        payload["embeds"] = embeds[:10]

    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, headers={"User-Agent": USER_AGENT}, timeout=25)
    if r.status_code >= 400:
        raise RuntimeError(f"Discord webhook error {r.status_code}: {r.text}")


def story_embed(i: int, it: Dict[str, Any]) -> Dict[str, Any]:
    summary = it["summary"] or ""
    if len(summary) > 340:
        summary = summary[:337] + "..."

    return {
        "title": f"{i}) {it['title']}",
        "url": it["url"],
        "description": f"{summary}\n\n**Source:** {it['source']}",
    }


# =========================
# MAIN
# =========================

def main() -> None:
    if not guard_should_post_now():
        import sys
        sys.exit(0)

    # Fetch feeds
    feed_urls = parse_feed_urls()
    all_items: List[Dict[str, Any]] = []
    for fu in feed_urls:
        all_items.extend(fetch_feed_items(fu))

    if not all_items:
        print("[DIGEST] No feed items fetched. Exiting without posting.")
        return

    # Window filter
    cutoff = datetime.now(ZoneInfo("UTC")) - timedelta(hours=DIGEST_WINDOW_HOURS)
    windowed = []
    for it in all_items:
        dt = it.get("published_dt")
        if dt is None or dt >= cutoff:
            windowed.append(it)

    stories = dedupe_and_select(windowed)
    if not stories:
        print("[DIGEST] No items found in window. Exiting without posting.")
        return

    # Build header (no video links here)
    local_date = datetime.now(ZoneInfo(DIGEST_GUARD_TZ)).strftime("%B %d, %Y")
    bullets = "\n".join([f"► 🎮 {s['title']}" for s in stories[:3]])

    header = (
        f"{NEWSLETTER_TAGLINE}\n\n"
        f"{local_date}\n\n"
        f"In Tonight’s Edition of {NEWSLETTER_NAME}…\n"
        f"{bullets}\n\n"
        f"Tonight’s Top Stories"
    )

    # Post header as its own message
    discord_post(header)

    # Post YouTube as its own message (best chance of “playable”)
    yt_url = fetch_latest_youtube_url()
    if yt_url:
        # Standalone URL only
        discord_post(yt_url)
    else:
        print("[YT] No YouTube URL found.")

    # Post Adilo as its own message (standalone URL)
    adilo_watch = scrape_adilo_latest_watch_url()
    if adilo_watch and adilo_watch != ADILO_PUBLIC_HOME_PAGE:
        discord_post(adilo_watch)
    else:
        # Still post *something*, but make it clear
        discord_post(f"{ADILO_PUBLIC_HOME_PAGE}")

    # Post each story as its own message so cards are separated per story
    for idx, it in enumerate(stories, start=1):
        discord_post("", [story_embed(idx, it)])
        # tiny pause to avoid webhook rate-limit spikes
        time.sleep(0.4)

    print("[DONE] Digest posted.")
    print(f"[DONE] YouTube: {yt_url}")
    print(f"[DONE] Featured Adilo video: {adilo_watch}")


if __name__ == "__main__":
    main()
