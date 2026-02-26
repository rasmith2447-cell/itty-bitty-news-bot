import os
import re
import json
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

# Guard settings
DIGEST_FORCE_POST = os.getenv("DIGEST_FORCE_POST", "").strip().lower() in ("1", "true", "yes", "y")
DIGEST_GUARD_TZ = os.getenv("DIGEST_GUARD_TZ", "America/Los_Angeles").strip()
DIGEST_GUARD_LOCAL_HOUR = int(os.getenv("DIGEST_GUARD_LOCAL_HOUR", "19").strip())
DIGEST_GUARD_LOCAL_MINUTE = int(os.getenv("DIGEST_GUARD_LOCAL_MINUTE", "0").strip())
DIGEST_GUARD_WINDOW_MINUTES = int(os.getenv("DIGEST_GUARD_WINDOW_MINUTES", "120").strip())

# YouTube RSS
YOUTUBE_RSS_URL = os.getenv(
    "YOUTUBE_RSS_URL",
    "https://www.youtube.com/feeds/videos.xml?channel_id=UC0SJd4h7GQqoYTVjlDnSzqQ"
).strip()
YOUTUBE_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "").strip()

# Adilo scrape pages
ADILO_PUBLIC_LATEST_PAGE = os.getenv(
    "ADILO_PUBLIC_LATEST_PAGE",
    "https://adilo.bigcommand.com/c/ittybittygamingnews/video"
).strip()
ADILO_PUBLIC_HOME_PAGE = os.getenv(
    "ADILO_PUBLIC_HOME_PAGE",
    "https://adilo.bigcommand.com/c/ittybittygamingnews/home"
).strip()


HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# =========================
# HELPERS
# =========================

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
        microsecond=0
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


def parse_feed_urls() -> List[str]:
    raw = os.getenv("FEED_URLS", "").strip()
    if raw:
        # Supports comma or newline separated
        parts = [p.strip() for p in re.split(r"[\n,]+", raw) if p.strip()]
        return parts
    return DEFAULT_FEED_URLS


def safe_text(s: str) -> str:
    return (s or "").replace("\r", " ").replace("\n", " ").strip()


def domain_from_url(url: str) -> str:
    try:
        return re.sub(r"^www\.", "", re.findall(r"https?://([^/]+)", url)[0].lower())
    except Exception:
        return "source"


def is_probably_not_news(title: str, summary: str) -> bool:
    t = (title or "").lower()
    s = (summary or "").lower()

    # Kill listicles, deals, evergreen guides, opinion-y “debate/poll/ranking”
    bad_markers = [
        "best ", "top ", "ranking", "ranked", "guide", "review", "deals", "deal",
        "drops to", "% off", "sale", "discount", "coupon",
        "poll:", "poll ", "debate:", "debate ", "opinion", "editorial",
        "history of", "2026 update",
        "cosplay", "letters",
        "power bank", "controller", "controllers",
    ]
    if any(m in t for m in bad_markers):
        return True
    if any(m in s for m in bad_markers):
        return True

    # Rumor/speculation filter
    rumor_markers = ["rumor", "rumour", "leak", "leaked", "reportedly", "could be", "might be", "speculation"]
    if any(m in t for m in rumor_markers):
        return True
    if any(m in s for m in rumor_markers):
        return True

    return False


def fetch_feed_items(feed_url: str) -> List[Dict[str, Any]]:
    print(f"[RSS] GET {feed_url}")
    # feedparser can fetch itself, but we want consistent headers/timeouts
    try:
        r = requests.get(feed_url, headers=HEADERS, timeout=25)
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
        if getattr(parsed, "bozo", 0) == 1:
            # log but don’t fail
            print(f"[RSS] bozo=1 for {feed_url}: {getattr(parsed, 'bozo_exception', '')}")
        items = []
        for e in parsed.entries:
            link = e.get("link") or ""
            title = safe_text(e.get("title") or "")
            summary = safe_text(e.get("summary") or e.get("description") or "")
            published = e.get("published") or e.get("updated") or ""
            items.append({
                "title": title,
                "summary": summary,
                "url": link,
                "source": domain_from_url(link) if link else domain_from_url(feed_url),
                "published_raw": published,
            })
        return items
    except Exception as ex:
        print(f"[RSS] Feed failed: {feed_url} ({ex})")
        return []


def parse_published_dt(raw: str) -> Optional[datetime]:
    # lightweight parsing: let feedparser handle most; if missing, return None
    # feedparser sometimes includes parsed struct_time fields, but we stored raw string
    # We’ll fallback to “now” in sorting if needed.
    if not raw:
        return None
    try:
        # feedparser has date parsing utility
        t = feedparser._parse_date(raw)
        if t:
            return datetime(*t[:6], tzinfo=ZoneInfo("UTC"))
    except Exception:
        pass
    return None


def dedupe_and_filter(all_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Basic “no repeats across sources” clustering by normalized title
    seen_keys = set()
    out = []

    for it in all_items:
        title = it["title"]
        summary = it["summary"]
        url = it["url"]

        if not url or not title:
            continue
        if is_probably_not_news(title, summary):
            continue

        key = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
        key = re.sub(r"\s+", " ", key)

        if key in seen_keys:
            continue
        seen_keys.add(key)

        it["published_dt"] = parse_published_dt(it.get("published_raw", ""))
        out.append(it)

    # Sort newest first (published_dt desc; None treated as old)
    def sort_key(x):
        dt = x.get("published_dt")
        return dt.timestamp() if dt else 0

    out.sort(key=sort_key, reverse=True)

    # limit per source
    per_source = {}
    final = []
    for it in out:
        src = it["source"]
        per_source[src] = per_source.get(src, 0) + 1
        if per_source[src] > DIGEST_MAX_PER_SOURCE:
            continue
        final.append(it)
        if len(final) >= DIGEST_TOP_N:
            break

    return final


# =========================
# YOUTUBE (embed-friendly)
# =========================

def fetch_latest_youtube() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Returns (video_url, title, thumbnail_url)
    Filters out Shorts.
    """
    try:
        print(f"[YT] Fetch RSS: {YOUTUBE_RSS_URL}")
        r = requests.get(YOUTUBE_RSS_URL, headers=HEADERS, timeout=25)
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
        for e in parsed.entries:
            link = e.get("link") or ""
            title = safe_text(e.get("title") or "")
            if not link:
                continue
            # Filter Shorts
            if "/shorts/" in link:
                continue

            m = re.search(r"v=([A-Za-z0-9_-]{6,})", link)
            vid = m.group(1) if m else None
            if not vid:
                # sometimes link is already https://youtu.be/<id>
                m2 = re.search(r"youtu\.be/([A-Za-z0-9_-]{6,})", link)
                vid = m2.group(1) if m2 else None

            thumb = None
            if vid:
                # maxres sometimes 404; Discord will still show if it can fetch
                thumb = f"https://img.youtube.com/vi/{vid}/maxresdefault.jpg"

            return link, title, thumb
    except Exception as ex:
        print(f"[YT] Failed to fetch RSS: {ex}")

    return None, None, None


# =========================
# ADILO (more reliable scrape)
# =========================

def scrape_adilo_latest_watch_url() -> str:
    """
    Scrape the public latest page and extract candidates.
    Strategy:
      - Fetch HTML
      - Find ALL occurrences of video?id=<ID> and watch/<ID>
      - Choose the best candidate:
          Prefer last video?id=<ID> found (often the newest)
          else last watch/<ID> found
      - Return https://adilo.bigcommand.com/watch/<ID>
      - If we can’t find an ID, fallback to ADILO_PUBLIC_HOME_PAGE
    """
    urls_to_try = [
        ADILO_PUBLIC_LATEST_PAGE,
        f"{ADILO_PUBLIC_LATEST_PAGE}?cb={int(time.time()*1000)}",
        f"{ADILO_PUBLIC_LATEST_PAGE}/?cb={int(time.time()*1000)}",
        ADILO_PUBLIC_HOME_PAGE,
    ]

    for u in urls_to_try:
        try:
            print(f"[ADILO] SCRAPE attempt=1 timeout=25 url={u}")
            r = requests.get(u, headers=HEADERS, timeout=25)
            r.raise_for_status()
            html = r.text or ""
            # Search both patterns
            ids_video = re.findall(r"video\?id=([A-Za-z0-9_-]+)", html)
            ids_watch = re.findall(r"/watch/([A-Za-z0-9_-]+)", html)

            # Some pages might embed JSON with "id":"XYZ"
            # But don’t overmatch everything—only accept plausible IDs:
            ids_generic = re.findall(r'"id"\s*:\s*"([A-Za-z0-9_-]{6,})"', html)

            candidate_id = None

            if ids_video:
                candidate_id = ids_video[-1]
            elif ids_watch:
                candidate_id = ids_watch[-1]
            else:
                # If we have only generic IDs, pick the last one (least bad fallback)
                if ids_generic:
                    candidate_id = ids_generic[-1]

            if candidate_id:
                watch_url = f"https://adilo.bigcommand.com/watch/{candidate_id}"
                print(f"[ADILO] Found candidate id={candidate_id} -> {watch_url}")
                return watch_url
        except Exception as ex:
            print(f"[ADILO] SCRAPE failed: {ex}")

    print(f"[ADILO] Falling back: {ADILO_PUBLIC_HOME_PAGE}")
    return ADILO_PUBLIC_HOME_PAGE


def fetch_og_image(url: str) -> Optional[str]:
    """Try to grab an og:image for nicer Discord cards."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=25)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        tag = soup.find("meta", property="og:image")
        if tag and tag.get("content"):
            return tag["content"].strip()
    except Exception:
        pass
    return None


# =========================
# DISCORD POST (embeds)
# =========================

def discord_post(content: str, embeds: List[Dict[str, Any]]) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL")

    payload: Dict[str, Any] = {
        "content": content,
        "embeds": embeds[:10],  # Discord max per message
        "allowed_mentions": {"parse": []},
    }

    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, headers={"User-Agent": USER_AGENT}, timeout=25)
    if r.status_code >= 400:
        # Print response body to help debug “400 Bad Request”
        raise RuntimeError(f"Discord webhook error {r.status_code}: {r.text}")


def build_story_embed(i: int, it: Dict[str, Any]) -> Dict[str, Any]:
    title = f"{i}) {it['title']}"
    url = it["url"]
    summary = it["summary"]
    src = it["source"]

    # Keep summaries tight for Discord
    if len(summary) > 320:
        summary = summary[:317] + "..."

    emb: Dict[str, Any] = {
        "title": title,
        "url": url,
        "description": f"{summary}\n\n**Source:** {src}",
    }
    return emb


def build_video_embed(label: str, url: str, title: Optional[str], thumb: Optional[str]) -> Dict[str, Any]:
    emb: Dict[str, Any] = {
        "title": title or label,
        "url": url,
        "description": label,
    }
    if thumb:
        emb["thumbnail"] = {"url": thumb}
    return emb


# =========================
# MAIN
# =========================

def main() -> None:
    # Guard
    if not guard_should_post_now():
        import sys
        sys.exit(0)

    # Pull feeds
    feed_urls = parse_feed_urls()
    all_items: List[Dict[str, Any]] = []
    for fu in feed_urls:
        all_items.extend(fetch_feed_items(fu))

    if not all_items:
        print("[DIGEST] No feed items fetched. Exiting without posting.")
        return

    # Filter to window
    cutoff = datetime.now(ZoneInfo("UTC")) - timedelta(hours=DIGEST_WINDOW_HOURS)
    # Keep items with publish date in window; if missing dt, keep (some feeds omit) but they’ll sort older
    windowed = []
    for it in all_items:
        dt = it.get("published_dt") or parse_published_dt(it.get("published_raw", ""))
        if dt is None:
            windowed.append(it)
        else:
            if dt >= cutoff:
                windowed.append(it)

    stories = dedupe_and_filter(windowed)
    if not stories:
        print("[DIGEST] No items found in window. Exiting without posting.")
        return

    # Fetch YouTube + Adilo
    yt_url, yt_title, yt_thumb = fetch_latest_youtube()
    adilo_watch = scrape_adilo_latest_watch_url()
    adilo_thumb = fetch_og_image(adilo_watch) if adilo_watch.startswith("https://adilo.bigcommand.com/watch/") else None

    # Build header content (NO “cards below”, no instructions)
    date_line = now_local().strftime("%B %d, %Y")
    bullets = "\n".join([f"► 🎮 {s['title']}" for s in stories[:3]])

    content = (
        f"{NEWSLETTER_TAGLINE}\n\n"
        f"{date_line}\n\n"
        f"In Tonight’s Edition of {NEWSLETTER_NAME}…\n"
        f"{bullets}\n\n"
        f"Tonight’s Top Stories"
    )

    # Build embeds: videos first, then stories (so they appear “with” the post)
    embeds: List[Dict[str, Any]] = []

    if yt_url:
        # Use an embed object so you always get a rich card even when Discord doesn’t unfurl text links.
        embeds.append(build_video_embed("▶️ YouTube (latest)", yt_url, yt_title or "YouTube (latest)", yt_thumb))

    if adilo_watch:
        embeds.append(build_video_embed("📺 Adilo (latest)", adilo_watch, "Watch today’s Itty Bitty Gaming News (Adilo)", adilo_thumb))

    # Story cards, in order
    for idx, it in enumerate(stories, start=1):
        embeds.append(build_story_embed(idx, it))

    discord_post(content, embeds)

    print("[DONE] Digest posted.")
    print(f"[DONE] YouTube: {yt_url}")
    print(f"[DONE] Featured Adilo video: {adilo_watch}")


if __name__ == "__main__":
    main()
