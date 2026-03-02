import os
import sys
import json
import time
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Optional, Dict, Any, Tuple
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests
import feedparser
from dateutil import parser as dateparser
from bs4 import BeautifulSoup


# ----------------------------
# Config / Defaults
# ----------------------------

DEFAULT_FEEDS = [
    "https://www.bluesnews.com/news/news_1_0.rdf",
    "https://www.ign.com/rss/v2/articles/feed?categories=games",
    "https://www.gamespot.com/feeds/mashup/",
    "https://gamerant.com/feed",
    "https://www.polygon.com/rss/index.xml",
    "https://www.videogameschronicle.com/feed/",
    "https://www.gematsu.com/feed",
]

USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNews/Digest").strip()
HEADERS = {"User-Agent": USER_AGENT}

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

DIGEST_WINDOW_HOURS = int(os.getenv("DIGEST_WINDOW_HOURS", "24").strip())
DIGEST_TOP_N = int(os.getenv("DIGEST_TOP_N", "5").strip())
DIGEST_MAX_PER_SOURCE = int(os.getenv("DIGEST_MAX_PER_SOURCE", "1").strip())

NEWSLETTER_NAME = os.getenv("NEWSLETTER_NAME", "Itty Bitty Gaming News").strip()
NEWSLETTER_TAGLINE = os.getenv("NEWSLETTER_TAGLINE", "Snackable daily gaming news — five days a week.").strip()

DIGEST_CACHE_FILE = os.getenv("DIGEST_CACHE_FILE", ".digest_cache.json").strip()
DIGEST_POST_ONCE_PER_DAY = os.getenv("DIGEST_POST_ONCE_PER_DAY", "").strip().lower() in ("1", "true", "yes", "on")

# Guard time window
DIGEST_FORCE_POST = os.getenv("DIGEST_FORCE_POST", "").strip().lower() in ("1", "true", "yes", "on")
GUARD_TZ = os.getenv("DIGEST_GUARD_TZ", "America/Los_Angeles").strip()
GUARD_HOUR = int(os.getenv("DIGEST_GUARD_LOCAL_HOUR", "19").strip())      # 7pm PT
GUARD_MINUTE = int(os.getenv("DIGEST_GUARD_LOCAL_MINUTE", "0").strip())
GUARD_WINDOW_MINUTES = int(os.getenv("DIGEST_GUARD_WINDOW_MINUTES", "30").strip())

# YouTube RSS (latest upload)
YOUTUBE_RSS_URL = os.getenv("YOUTUBE_RSS_URL", "").strip()

# Adilo API + scrape fallback
ADILO_PUBLIC_KEY = os.getenv("ADILO_PUBLIC_KEY", "").strip()
ADILO_SECRET_KEY = os.getenv("ADILO_SECRET_KEY", "").strip()
ADILO_PROJECT_ID = os.getenv("ADILO_PROJECT_ID", "").strip()

ADILO_PUBLIC_LATEST_PAGE = os.getenv("ADILO_PUBLIC_LATEST_PAGE", "https://adilo.bigcommand.com/c/ittybittygamingnews/video").strip()
ADILO_PUBLIC_HOME_PAGE = os.getenv("ADILO_PUBLIC_HOME_PAGE", "https://adilo.bigcommand.com/c/ittybittygamingnews/home").strip()

ADILO_API_BASE = "https://adilo-api.bigcommand.com/v1"

# Skip Adilo promo videos by title keyword (tweak as needed)
ADILO_SKIP_TITLE_KEYWORDS = [
    "promo",
]


# ----------------------------
# Data model
# ----------------------------

@dataclass
class Story:
    title: str
    url: str
    source: str
    published: datetime
    summary: str


# ----------------------------
# Cache helpers
# ----------------------------

def load_cache(path: str) -> Dict[str, Any]:
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[CACHE] Failed to read cache {path}: {e}")
        return {}

def save_cache(path: str, data: Dict[str, Any]) -> None:
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
    except Exception as e:
        print(f"[CACHE] Failed to write cache {path}: {e}")

def today_key_local(tz_name: str) -> str:
    tz = ZoneInfo(tz_name)
    return datetime.now(tz).strftime("%Y-%m-%d")


# ----------------------------
# Guard
# ----------------------------

def guard_should_post_now() -> bool:
    if DIGEST_FORCE_POST:
        print("[GUARD] DIGEST_FORCE_POST enabled — bypassing time guard.")
        return True

    tz = ZoneInfo(GUARD_TZ)
    now_local = datetime.now(tz)
    target = now_local.replace(hour=GUARD_HOUR, minute=GUARD_MINUTE, second=0, microsecond=0)

    delta_min = abs((now_local - target).total_seconds()) / 60.0
    if delta_min <= GUARD_WINDOW_MINUTES:
        print(
            f"[GUARD] OK. Local now: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            f"Target: {target.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            f"Delta={delta_min:.1f}min <= {GUARD_WINDOW_MINUTES}min"
        )
        return True

    print(
        f"[GUARD] Not within posting window. Local now: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
        f"Target: {target.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
        f"Delta={delta_min:.1f}min > {GUARD_WINDOW_MINUTES}min. Exiting without posting."
    )
    return False


# ----------------------------
# RSS ingestion
# ----------------------------

def parse_datetime(dt_val) -> Optional[datetime]:
    if not dt_val:
        return None
    try:
        # feedparser gives struct_time sometimes
        if hasattr(dt_val, "tm_year"):
            return datetime(*dt_val[:6], tzinfo=ZoneInfo("UTC"))
        # string
        return dateparser.parse(str(dt_val))
    except Exception:
        return None

def fetch_feed_items(feed_url: str, timeout: int = 20) -> List[Story]:
    print(f"[RSS] GET {feed_url}")
    try:
        # feedparser can accept raw bytes; but it handles fetching poorly on some sites.
        # We'll fetch ourselves to control headers/timeouts.
        r = requests.get(feed_url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
        if getattr(parsed, "bozo", 0) == 1:
            # bozo isn't always fatal; keep going.
            bozo_exc = getattr(parsed, "bozo_exception", None)
            if bozo_exc:
                print(f"[RSS] bozo=1 for {feed_url}: {bozo_exc}")

        out: List[Story] = []
        for e in parsed.entries:
            title = (e.get("title") or "").strip()
            link = (e.get("link") or "").strip()
            if not title or not link:
                continue

            # Published
            published = (
                parse_datetime(e.get("published_parsed"))
                or parse_datetime(e.get("updated_parsed"))
                or parse_datetime(e.get("published"))
                or parse_datetime(e.get("updated"))
            )
            if not published:
                # If missing, treat as old
                continue

            # Ensure tz-aware
            if published.tzinfo is None:
                published = published.replace(tzinfo=ZoneInfo("UTC"))

            # Summary
            summary = (e.get("summary") or e.get("description") or "").strip()
            summary = re.sub(r"\s+", " ", BeautifulSoup(summary, "html.parser").get_text(" ", strip=True))

            src = urlparse(link).netloc.replace("www.", "")
            out.append(Story(title=title, url=link, source=src, published=published, summary=summary))

        return out

    except Exception as ex:
        print(f"[RSS] Feed failed: {feed_url} ({ex})")
        return []

def collect_stories() -> List[Story]:
    feed_urls_env = os.getenv("FEED_URLS", "").strip()
    feed_urls = [u.strip() for u in feed_urls_env.split(",") if u.strip()] if feed_urls_env else DEFAULT_FEEDS

    all_items: List[Story] = []
    for url in feed_urls:
        all_items.extend(fetch_feed_items(url))

    # Filter by window
    now_utc = datetime.now(ZoneInfo("UTC"))
    cutoff = now_utc - timedelta(hours=DIGEST_WINDOW_HOURS)
    in_window = [s for s in all_items if s.published.astimezone(ZoneInfo("UTC")) >= cutoff]

    # Deduplicate by URL
    seen = set()
    deduped: List[Story] = []
    for s in sorted(in_window, key=lambda x: x.published, reverse=True):
        key = s.url.split("#")[0].strip()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(s)

    # Limit per source
    per_source: Dict[str, int] = {}
    filtered: List[Story] = []
    for s in deduped:
        per_source.setdefault(s.source, 0)
        if per_source[s.source] >= DIGEST_MAX_PER_SOURCE:
            continue
        per_source[s.source] += 1
        filtered.append(s)

    # Final top N
    return filtered[:DIGEST_TOP_N]


# ----------------------------
# YouTube latest via RSS
# ----------------------------

def youtube_latest() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Returns (video_url, title, thumbnail_url)
    """
    if not YOUTUBE_RSS_URL:
        return (None, None, None)

    print(f"[YT] Fetch RSS: {YOUTUBE_RSS_URL}")
    try:
        r = requests.get(YOUTUBE_RSS_URL, headers=HEADERS, timeout=20)
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
        if not parsed.entries:
            return (None, None, None)

        entry = parsed.entries[0]
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()

        # Filter shorts by title heuristic
        if re.search(r"\bshorts?\b|#shorts", title, flags=re.IGNORECASE):
            # Try next entries until a non-shorts is found
            for e in parsed.entries[1:10]:
                t = (e.get("title") or "").strip()
                if not re.search(r"\bshorts?\b|#shorts", t, flags=re.IGNORECASE):
                    title = t
                    link = (e.get("link") or "").strip()
                    entry = e
                    break

        thumb = None
        # YouTube feeds often provide media_thumbnail
        mt = entry.get("media_thumbnail")
        if isinstance(mt, list) and mt:
            thumb = mt[0].get("url")
        return (link or None, title or None, thumb)

    except Exception as e:
        print(f"[YT] Failed to fetch/parse RSS: {e}")
        return (None, None, None)


# ----------------------------
# Adilo latest (API first, scrape fallback, cached fallback)
# ----------------------------

def adilo_api_headers() -> Dict[str, str]:
    # This mirrors what you've been doing: public/secret via headers.
    # If Adilo expects different auth, you already confirmed it worked before with these keys.
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "publicKey": ADILO_PUBLIC_KEY,
        "secretKey": ADILO_SECRET_KEY,
    }

def adilo_watch_url(video_id: str) -> str:
    # Discord preview is generally better on /watch/<id>
    return f"https://adilo.bigcommand.com/watch/{video_id}"

def adilo_latest_via_api(cache: Dict[str, Any]) -> Optional[str]:
    if not (ADILO_PUBLIC_KEY and ADILO_SECRET_KEY and ADILO_PROJECT_ID):
        print("[ADILO] API not attempted (missing ADILO_PROJECT_ID / ADILO_PUBLIC_KEY / ADILO_SECRET_KEY).")
        return None

    try:
        # Fetch first page to get total
        url = f"{ADILO_API_BASE}/projects/{ADILO_PROJECT_ID}/files?From=1&To=50"
        r = requests.get(url, headers=adilo_api_headers(), timeout=20)
        r.raise_for_status()
        data = r.json()

        payload = data.get("payload") or []
        meta = data.get("meta") or {}
        total = int(meta.get("total") or meta.get("Total") or len(payload) or 0)

        if total <= 0:
            return None

        # We don't trust API ordering; we sample the last few pages and pick max upload_date.
        # This keeps API calls low but still finds latest reliably.
        page_size = 50
        last_from = max(1, total - page_size + 1)
        ranges = []
        ranges.append((last_from, total))
        # include previous page too (covers edge cases where latest sits earlier)
        prev_from = max(1, last_from - page_size)
        prev_to = max(page_size, last_from - 1)
        if prev_to >= prev_from:
            ranges.append((prev_from, prev_to))

        best_dt = None
        best_id = None
        best_title = None

        def consider(file_id: str) -> None:
            nonlocal best_dt, best_id, best_title
            murl = f"{ADILO_API_BASE}/files/{file_id}/meta"
            mr = requests.get(murl, headers=adilo_api_headers(), timeout=20)
            mr.raise_for_status()
            mp = (mr.json() or {}).get("payload") or {}
            upload_date = (mp.get("upload_date") or "").strip()
            title = (mp.get("title") or "").strip()

            if any(k in title.lower() for k in ADILO_SKIP_TITLE_KEYWORDS):
                return

            if not upload_date:
                return
            dt = dateparser.parse(upload_date)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("UTC"))

            if best_dt is None or dt > best_dt:
                best_dt = dt
                best_id = file_id
                best_title = title

        for frm, to in ranges:
            purl = f"{ADILO_API_BASE}/projects/{ADILO_PROJECT_ID}/files?From={frm}&To={to}"
            pr = requests.get(purl, headers=adilo_api_headers(), timeout=20)
            pr.raise_for_status()
            items = (pr.json() or {}).get("payload") or []
            for it in items:
                fid = (it.get("id") or "").strip()
                if fid:
                    consider(fid)

        if best_id:
            w = adilo_watch_url(best_id)
            cache["last_adilo_watch_url"] = w
            cache["last_adilo_video_id"] = best_id
            cache["last_adilo_title"] = best_title or ""
            print(f"[ADILO] API latest id={best_id} -> {w}")
            return w

        return None

    except Exception as e:
        print(f"[ADILO] API failed: {e}")
        return None

def add_cachebuster(url: str) -> str:
    u = urlparse(url)
    q = parse_qs(u.query)
    q["cb"] = [str(int(time.time() * 1000))]
    new_q = urlencode(q, doseq=True)
    return urlunparse((u.scheme, u.netloc, u.path, u.params, new_q, u.fragment))

def extract_adilo_ids_from_html(html: str) -> List[str]:
    # Look for common patterns:
    # - /watch/<id>
    # - video?id=<id>
    ids = set()

    for m in re.findall(r"/watch/([A-Za-z0-9_\-]+)", html):
        ids.add(m)

    for m in re.findall(r"video\?id=([A-Za-z0-9_\-]+)", html):
        ids.add(m)

    # Sometimes raw IDs appear in JS-ish blobs; accept long-ish tokens
    for m in re.findall(r'"id"\s*:\s*"([A-Za-z0-9_\-]{6,})"', html):
        ids.add(m)

    return list(ids)

def adilo_latest_via_scrape(cache: Dict[str, Any]) -> Optional[str]:
    candidates = [
        ADILO_PUBLIC_LATEST_PAGE,
        add_cachebuster(ADILO_PUBLIC_LATEST_PAGE),
        add_cachebuster(ADILO_PUBLIC_LATEST_PAGE.rstrip("/") + "/"),
        add_cachebuster(ADILO_PUBLIC_LATEST_PAGE + "?video=latest"),
        add_cachebuster(ADILO_PUBLIC_LATEST_PAGE + "?id="),
        ADILO_PUBLIC_HOME_PAGE,
    ]

    for url in candidates:
        try:
            print(f"[ADILO] SCRAPE timeout=25 url={url}")
            r = requests.get(url, headers=HEADERS, timeout=25)
            r.raise_for_status()
            html = r.text or ""
            ids = extract_adilo_ids_from_html(html)
            if not ids:
                continue

            # Heuristic: try to avoid "promo" if it tends to be first on page
            # We can’t fetch meta without API, so we just pick the first ID that
            # isn’t obviously a short token (all IDs are similar) and trust page order.
            # If page order is unreliable, API path should fix it.
            chosen = ids[0]
            w = adilo_watch_url(chosen)
            cache["last_adilo_watch_url"] = w
            cache["last_adilo_video_id"] = chosen
            print(f"[ADILO] SCRAPE found id={chosen} -> {w}")
            return w

        except requests.exceptions.Timeout:
            print(f"[ADILO] Timeout url={url} (timeout=25)")
            continue
        except Exception as e:
            print(f"[ADILO] SCRAPE failed: {e}")
            continue

    # Last resort: cached last-good
    last_good = (cache.get("last_adilo_watch_url") or "").strip()
    if last_good:
        print(f"[ADILO] Using cached last-good: {last_good}")
        return last_good

    print(f"[ADILO] Falling back: {ADILO_PUBLIC_HOME_PAGE}")
    return ADILO_PUBLIC_HOME_PAGE

def adilo_latest(cache: Dict[str, Any]) -> str:
    # API first (best), then scrape, then cache/home
    w = adilo_latest_via_api(cache)
    if w:
        return w
    return adilo_latest_via_scrape(cache) or ADILO_PUBLIC_HOME_PAGE


# ----------------------------
# Discord posting (content + embeds)
# ----------------------------

def discord_post(content: str, embeds: List[Dict[str, Any]]) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL")

    payload: Dict[str, Any] = {
        "content": content,
        "embeds": embeds[:10],  # Discord limit: max 10 embeds per message
        "username": NEWSLETTER_NAME,
    }

    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, headers=HEADERS, timeout=20)
    # Helpful debug on failure
    if r.status_code >= 400:
        print(f"[DISCORD] Error {r.status_code}: {r.text[:500]}")
    r.raise_for_status()

def make_story_embed(idx: int, s: Story) -> Dict[str, Any]:
    # Keep embed descriptions short to avoid payload bloat
    desc = s.summary.strip()
    if len(desc) > 260:
        desc = desc[:257].rstrip() + "…"

    published_local = s.published.astimezone(ZoneInfo(GUARD_TZ))
    footer = f"Source: {s.source} • {published_local.strftime('%b %d, %Y %I:%M %p %Z')}"

    return {
        "title": f"{idx}) {s.title}",
        "url": s.url,
        "description": desc,
        "footer": {"text": footer},
    }

def make_youtube_embed(url: str, title: Optional[str], thumb: Optional[str]) -> Dict[str, Any]:
    emb = {
        "title": "▶️ YouTube (latest)",
        "url": url,
        "description": (title or "").strip(),
    }
    if thumb:
        emb["thumbnail"] = {"url": thumb}
    return emb

def make_adilo_embed(url: str) -> Dict[str, Any]:
    # Discord will usually render a rich preview for Adilo when url is the embed url.
    return {
        "title": "📺 Adilo (latest)",
        "url": url,
    }


# ----------------------------
# Main
# ----------------------------

def main() -> None:
    cache = load_cache(DIGEST_CACHE_FILE)

    if DIGEST_POST_ONCE_PER_DAY and not DIGEST_FORCE_POST:
        today = today_key_local(GUARD_TZ)
        if cache.get("posted_day") == today:
            print(f"[CACHE] Already posted for {today}. Exiting.")
            sys.exit(0)

    if not guard_should_post_now():
        sys.exit(0)

    stories = collect_stories()
    if not stories:
        print("[DIGEST] No items found in window. Exiting without posting.")
        sys.exit(0)

    # YouTube
    yt_url, yt_title, yt_thumb = youtube_latest()

    # Adilo
    adilo_url = adilo_latest(cache)

    # Build content (keep it clean; embeds carry the cards)
    now_local = datetime.now(ZoneInfo(GUARD_TZ))
    date_line = now_local.strftime("%B %d, %Y")

    # IMPORTANT: avoid the “cards below” phrasing you don’t want.
    content_lines = [
        NEWSLETTER_TAGLINE,
        "",
        date_line,
        "",
        "In Tonight’s Edition of Itty Bitty Gaming News…",
    ]
    for s in stories[:3]:
        content_lines.append(f"► 🎮 {s.title}")
    content_lines += [
        "",
        "Tonight’s Top Stories",
        "",
        # Keep these as plain section labels; embeds will appear right under the message
        # in the order we attach them.
    ]

    # Embeds:
    embeds: List[Dict[str, Any]] = []

    # Add story embeds first so they appear “under” the Top Stories section.
    for i, s in enumerate(stories, start=1):
        embeds.append(make_story_embed(i, s))

    # Then add YouTube and Adilo embeds at end (or swap if you prefer above)
    if yt_url:
        embeds.append(make_youtube_embed(yt_url, yt_title, yt_thumb))
    if adilo_url:
        embeds.append(make_adilo_embed(adilo_url))

    # Discord limit: 10 embeds. With 5 stories + YT + Adilo = 7 embeds (safe).
    content = "\n".join(content_lines).strip()

    discord_post(content, embeds)

    if DIGEST_POST_ONCE_PER_DAY and not DIGEST_FORCE_POST:
        cache["posted_day"] = today_key_local(GUARD_TZ)
        print(f"[CACHE] Marked posted for {cache['posted_day']}.")

    save_cache(DIGEST_CACHE_FILE, cache)

    print("[DONE] Digest posted.")
    if yt_url:
        print(f"[DONE] YouTube: {yt_url}")
    if adilo_url:
        print(f"[DONE] Featured Adilo video: {adilo_url}")


if __name__ == "__main__":
    main()
