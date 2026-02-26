#!/usr/bin/env python3
import os
import re
import time
import html
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse, parse_qs

import requests
import feedparser
from bs4 import BeautifulSoup

# =========================
# Config
# =========================

DEFAULT_FEEDS = [
    "https://www.bluesnews.com/news/news_1_0.rdf",
    "https://www.ign.com/rss/v2/articles/feed?categories=games",
    "https://www.gamespot.com/feeds/mashup/",
    "https://gamerant.com/feed",
    "https://www.polygon.com/rss/index.xml",
    "https://www.videogameschronicle.com/feed/",
    "https://www.gematsu.com/feed",
]

USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNews/Digest").strip() or "IttyBittyGamingNews/Digest"
DISCORD_WEBHOOK_URL = (os.getenv("DISCORD_WEBHOOK_URL") or "").strip()

NEWSLETTER_NAME = os.getenv("NEWSLETTER_NAME", "Itty Bitty Gaming News").strip()
NEWSLETTER_TAGLINE = os.getenv("NEWSLETTER_TAGLINE", "Snackable daily gaming news — five days a week.").strip()

DIGEST_WINDOW_HOURS = int(os.getenv("DIGEST_WINDOW_HOURS", "24").strip())
DIGEST_TOP_N = int(os.getenv("DIGEST_TOP_N", "5").strip())
DIGEST_MAX_PER_SOURCE = int(os.getenv("DIGEST_MAX_PER_SOURCE", "1").strip())

DIGEST_FORCE_POST = (os.getenv("DIGEST_FORCE_POST", "").strip().lower() in ("1", "true", "yes", "y", "on"))

DIGEST_GUARD_TZ = os.getenv("DIGEST_GUARD_TZ", "America/Los_Angeles").strip()
DIGEST_GUARD_LOCAL_HOUR = int(os.getenv("DIGEST_GUARD_LOCAL_HOUR", "19").strip())
DIGEST_GUARD_LOCAL_MINUTE = int(os.getenv("DIGEST_GUARD_LOCAL_MINUTE", "0").strip())
DIGEST_GUARD_WINDOW_MINUTES = int(os.getenv("DIGEST_GUARD_WINDOW_MINUTES", "120").strip())

# YouTube latest via RSS
YOUTUBE_RSS_URL = (os.getenv("YOUTUBE_RSS_URL") or "").strip()
YOUTUBE_CHANNEL_ID = (os.getenv("YOUTUBE_CHANNEL_ID") or "").strip()

# Adilo scrape pages
ADILO_PUBLIC_LATEST_PAGE = os.getenv("ADILO_PUBLIC_LATEST_PAGE", "https://adilo.bigcommand.com/c/ittybittygamingnews/video").strip()
ADILO_PUBLIC_HOME_PAGE = os.getenv("ADILO_PUBLIC_HOME_PAGE", "https://adilo.bigcommand.com/c/ittybittygamingnews/home").strip()

# Adilo API (preferred for reliability)
ADILO_API_BASE = "https://adilo-api.bigcommand.com/v1"
ADILO_PUBLIC_KEY = (os.getenv("ADILO_PUBLIC_KEY") or "").strip()
ADILO_SECRET_KEY = (os.getenv("ADILO_SECRET_KEY") or "").strip()
ADILO_PROJECT_ID = (os.getenv("ADILO_PROJECT_ID") or "").strip()

# optional: force id ONLY if you explicitly set it
FEATURED_VIDEO_FORCE_ID = (os.getenv("FEATURED_VIDEO_FORCE_ID") or "").strip()

# =========================
# Helpers
# =========================

def now_local() -> datetime:
    return datetime.now(ZoneInfo(DIGEST_GUARD_TZ))


def guard_should_post_now() -> bool:
    if DIGEST_FORCE_POST:
        print("[GUARD] DIGEST_FORCE_POST enabled — bypassing time guard.")
        return True

    tz = ZoneInfo(DIGEST_GUARD_TZ)
    now = datetime.now(tz)
    target_today = now.replace(
        hour=DIGEST_GUARD_LOCAL_HOUR,
        minute=DIGEST_GUARD_LOCAL_MINUTE,
        second=0,
        microsecond=0,
    )

    candidates = [target_today - timedelta(days=1), target_today, target_today + timedelta(days=1)]
    closest = min(candidates, key=lambda t: abs((now - t).total_seconds()))
    delta_min = abs((now - closest).total_seconds()) / 60.0

    if delta_min <= DIGEST_GUARD_WINDOW_MINUTES:
        print(
            f"[GUARD] OK. Local now: {now:%Y-%m-%d %H:%M:%S %Z}. "
            f"Closest target: {closest:%Y-%m-%d %H:%M:%S %Z}. "
            f"Delta={delta_min:.1f}min <= {DIGEST_GUARD_WINDOW_MINUTES}min"
        )
        return True

    print(
        f"[GUARD] Not within posting window. Local now: {now:%Y-%m-%d %H:%M:%S %Z}. "
        f"Closest target: {closest:%Y-%m-%d %H:%M:%S %Z}. "
        f"Delta={delta_min:.1f}min > {DIGEST_GUARD_WINDOW_MINUTES}min. Exiting without posting."
    )
    return False


def safe_text(s: str) -> str:
    s = s or ""
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def domain_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""


def parse_published(entry) -> Optional[datetime]:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = getattr(entry, key, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=ZoneInfo("UTC"))
            except Exception:
                pass
    return None


def is_probably_short(title: str, link: str) -> bool:
    t = (title or "").lower()
    l = (link or "").lower()
    if "#shorts" in t:
        return True
    if "/shorts/" in l:
        return True
    return False


def get_feed_urls() -> List[str]:
    raw = (os.getenv("FEED_URLS") or "").strip()
    if not raw:
        return DEFAULT_FEEDS
    parts = []
    for line in raw.replace(",", "\n").splitlines():
        u = line.strip()
        if u:
            parts.append(u)
    return parts or DEFAULT_FEEDS


# =========================
# Fetch RSS items
# =========================

def fetch_all_items() -> List[Dict]:
    feeds = get_feed_urls()
    window_start = datetime.now(ZoneInfo("UTC")) - timedelta(hours=DIGEST_WINDOW_HOURS)

    items: List[Dict] = []
    per_source_counts: Dict[str, int] = {}

    for url in feeds:
        print(f"[RSS] GET {url}")
        try:
            d = feedparser.parse(url)
            if not getattr(d, "entries", None):
                print(f"[RSS] No entries for {url}")
                continue

            for e in d.entries:
                title = safe_text(getattr(e, "title", ""))
                link = safe_text(getattr(e, "link", ""))
                if not title or not link:
                    continue

                if is_probably_short(title, link):
                    continue

                published = parse_published(e) or datetime.now(ZoneInfo("UTC"))
                if published < window_start:
                    continue

                src = domain_of(link) or domain_of(url) or "unknown"
                if per_source_counts.get(src, 0) >= DIGEST_MAX_PER_SOURCE:
                    continue

                summary = safe_text(getattr(e, "summary", "") or getattr(e, "description", ""))
                summary = re.sub(r"<[^>]+>", "", summary).strip()
                summary = safe_text(summary)

                items.append(
                    {
                        "title": title,
                        "url": link,
                        "source": src,
                        "published": published,
                        "summary": summary,
                    }
                )
                per_source_counts[src] = per_source_counts.get(src, 0) + 1

        except Exception as ex:
            print(f"[RSS] Feed failed: {url} ({ex})")

    items.sort(key=lambda x: x["published"], reverse=True)

    seen_urls = set()
    seen_titles = set()
    deduped = []
    for it in items:
        if it["url"] in seen_urls:
            continue
        tkey = re.sub(r"[^a-z0-9]+", "", it["title"].lower())
        if tkey in seen_titles:
            continue
        seen_urls.add(it["url"])
        seen_titles.add(tkey)
        deduped.append(it)

    return deduped[: max(DIGEST_TOP_N, 1)]


# =========================
# YouTube latest (RSS)
# =========================

def youtube_latest() -> Tuple[Optional[str], Optional[str]]:
    rss = YOUTUBE_RSS_URL
    if not rss and YOUTUBE_CHANNEL_ID:
        rss = f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}"
    if not rss:
        return None, None

    try:
        print(f"[YT] Fetch RSS: {rss}")
        d = feedparser.parse(rss)
        if not getattr(d, "entries", None):
            print("[YT] No entries in RSS.")
            return None, None

        # Find first non-short
        for e in d.entries[:15]:
            title = safe_text(getattr(e, "title", ""))
            link = safe_text(getattr(e, "link", ""))
            if not link:
                continue
            if is_probably_short(title, link):
                continue
            return link, title

        return None, None
    except Exception as ex:
        print(f"[YT] Failed: {ex}")
        return None, None


# =========================
# Adilo latest (API-first, then scrape)
# =========================

ADILO_ID_RE = re.compile(r"(?:video\?id=|/watch/)([A-Za-z0-9_\-]{6,})")

def _adilo_api_headers() -> Dict[str, str]:
    # This is intentionally conservative; if your API expects different header names,
    # keep them here so we only change one place.
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "X-Public-Key": ADILO_PUBLIC_KEY,
        "X-Secret-Key": ADILO_SECRET_KEY,
    }


def _adilo_api_get_json(url: str, timeout: int = 25) -> Optional[Dict]:
    try:
        r = requests.get(url, headers=_adilo_api_headers(), timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def _parse_upload_date(s: str) -> Optional[datetime]:
    # Example seen: "2023-12-15 21:24:04"
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            # Adilo dates are typically UTC-ish; treat as UTC for ordering
            return datetime.strptime(s, fmt).replace(tzinfo=ZoneInfo("UTC"))
        except Exception:
            pass
    return None


def adilo_latest_via_api() -> Optional[str]:
    """
    More reliable than scraping when keys are present.
    Strategy:
      - get total count
      - fetch 3 pages (first, last, and middle)
      - sample up to 12 items per page and fetch /files/{id}/meta for upload_date
      - pick best dt; then expand to check +/- 2 pages around the best page
    """
    if not (ADILO_PUBLIC_KEY and ADILO_SECRET_KEY and ADILO_PROJECT_ID):
        return None

    # Fetch first page to learn total
    first_url = f"{ADILO_API_BASE}/projects/{ADILO_PROJECT_ID}/files?From=1&To=50"
    j = _adilo_api_get_json(first_url, timeout=25)
    if not j or "meta" not in j:
        return None

    meta = j.get("meta") or {}
    total = int(meta.get("total") or 0)
    if total <= 0:
        return None

    def page_bounds(page_index: int, page_size: int = 50) -> Tuple[int, int]:
        start = page_index * page_size + 1
        end = min(total, (page_index + 1) * page_size)
        return start, end

    page_size = 50
    last_page_index = max(0, (total - 1) // page_size)
    mid_page_index = last_page_index // 2

    pages_to_probe = sorted(set([0, mid_page_index, last_page_index]))

    best = {"dt": None, "file_id": None, "page": None}

    def probe_page(page_index: int) -> None:
        nonlocal best
        frm, to = page_bounds(page_index, page_size)
        url = f"{ADILO_API_BASE}/projects/{ADILO_PROJECT_ID}/files?From={frm}&To={to}"
        j2 = _adilo_api_get_json(url, timeout=25)
        if not j2:
            return
        payload = j2.get("payload")
        if not isinstance(payload, list) or not payload:
            return

        # sample a chunk from start of page (often newer or older depending on ordering)
        sample = payload[: min(12, len(payload))]
        for it in sample:
            fid = (it.get("id") or "").strip()
            if not fid:
                continue
            meta_url = f"{ADILO_API_BASE}/files/{fid}/meta"
            jm = _adilo_api_get_json(meta_url, timeout=25)
            if not jm:
                continue
            p = jm.get("payload") or {}
            dt = _parse_upload_date(p.get("upload_date") or "")
            if not dt:
                continue
            if best["dt"] is None or dt > best["dt"]:
                best = {"dt": dt, "file_id": fid, "page": page_index}

    for pi in pages_to_probe:
        probe_page(pi)

    if not best["file_id"]:
        return None

    # Expand around winning page to avoid missing newer items due to ordering quirks
    for pi in range(max(0, best["page"] - 2), min(last_page_index, best["page"] + 2) + 1):
        probe_page(pi)

    if not best["file_id"]:
        return None

    watch_url = f"https://adilo.bigcommand.com/watch/{best['file_id']}"
    print(f"[ADILO] API newest candidate: {watch_url} dt={best['dt'].isoformat()}")
    return watch_url


def adilo_latest_via_scrape() -> Optional[str]:
    sess = requests.Session()
    sess.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )

    cb = str(int(time.time() * 1000))
    candidates = [
        ADILO_PUBLIC_LATEST_PAGE,
        f"{ADILO_PUBLIC_LATEST_PAGE}?cb={cb}",
        f"{ADILO_PUBLIC_LATEST_PAGE}/?cb={cb}",
        f"{ADILO_PUBLIC_LATEST_PAGE}?video=latest&cb={cb}",
        f"{ADILO_PUBLIC_LATEST_PAGE}?id=&cb={cb}",
        ADILO_PUBLIC_HOME_PAGE,
    ]

    for url in candidates:
        try:
            print(f"[ADILO] SCRAPE attempt=1 timeout=25 url={url}")
            r = sess.get(url, timeout=25, allow_redirects=True)
            text = r.text or ""

            ids_vid = re.findall(r"video\?id=([A-Za-z0-9_\-]{6,})", text)
            ids_watch = re.findall(r"/watch/([A-Za-z0-9_\-]{6,})", text)

            all_ids = []
            for x in ids_vid:
                if x and x not in all_ids:
                    all_ids.append(x)
            for x in ids_watch:
                if x and x not in all_ids:
                    all_ids.append(x)

            if not all_ids:
                continue

            chosen = all_ids[0]
            watch_url = f"https://adilo.bigcommand.com/watch/{chosen}"
            print(f"[ADILO] Found candidate id={chosen} -> {watch_url}")
            return watch_url

        except requests.exceptions.Timeout:
            print(f"[ADILO] Timeout url={url} (timeout=25)")
        except Exception as ex:
            print(f"[ADILO] SCRAPE failed: {ex}")

    print(f"[ADILO] Falling back: {ADILO_PUBLIC_HOME_PAGE}")
    return ADILO_PUBLIC_HOME_PAGE


def scrape_adilo_latest() -> Optional[str]:
    # Only honor forced ID if YOU explicitly set it (we're not hard-locking automatically)
    if FEATURED_VIDEO_FORCE_ID:
        forced = FEATURED_VIDEO_FORCE_ID.strip()
        if forced.startswith("http"):
            print(f"[ADILO] Using FEATURED_VIDEO_FORCE_ID as URL: {forced}")
            return forced
        url = f"https://adilo.bigcommand.com/watch/{forced}"
        print(f"[ADILO] Using FEATURED_VIDEO_FORCE_ID: {url}")
        return url

    # 1) API-first
    api_url = adilo_latest_via_api()
    if api_url:
        return api_url

    # 2) Public scrape fallback
    return adilo_latest_via_scrape()


def adilo_thumbnail_from_watch_url(watch_url: str) -> Optional[str]:
    try:
        r = requests.get(watch_url, timeout=20, headers={"User-Agent": USER_AGENT})
        soup = BeautifulSoup(str(r.text), "html.parser")
        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            return og["content"].strip()
    except Exception:
        return None
    return None


# =========================
# Discord posting
# =========================

def discord_post(content: str, embeds: Optional[List[Dict]] = None) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL")

    payload = {"content": content}
    if embeds:
        payload["embeds"] = embeds

    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
    if r.status_code >= 400:
        raise requests.HTTPError(f"{r.status_code} {r.text}", response=r)


def build_story_embed(idx: int, item: Dict) -> Dict:
    title = f"{idx}) {item['title']}"
    url = item["url"]
    src = item["source"]
    summary = item["summary"] or ""
    if len(summary) > 320:
        summary = summary[:317].rstrip() + "…"

    desc_parts = []
    if summary:
        desc_parts.append(summary)
    desc_parts.append(f"Source: {src}")
    desc_parts.append(url)

    return {
        "title": title[:256],
        "url": url,
        "description": "\n".join(desc_parts)[:4096],
    }


def main() -> None:
    if not guard_should_post_now():
        return

    items = fetch_all_items()
    if not items:
        print("[DIGEST] No items found in window. Exiting without posting.")
        return

    yt_url, yt_title = youtube_latest()
    adilo_url = scrape_adilo_latest()

    today_str = now_local().strftime("%B %d, %Y")

    blerbs = items[:3]
    blerb_lines = [f"► 🎮 {it['title'][:80]}" for it in blerbs]

    header = []
    if NEWSLETTER_TAGLINE:
        header.append(NEWSLETTER_TAGLINE)
        header.append("")
    header.append(today_str)
    header.append("")
    header.append(f"In Tonight’s Edition of {NEWSLETTER_NAME}…")
    header.extend(blerb_lines)
    header.append("")
    header.append("Tonight’s Top Stories")

    content = "\n".join(header)

    embeds: List[Dict] = []
    for i, it in enumerate(items, start=1):
        embeds.append(build_story_embed(i, it))

    # Adilo embed card
    if adilo_url:
        adilo_embed = {
            "title": "📺 Adilo (latest)",
            "url": adilo_url,
            "description": adilo_url,
        }
        thumb = None
        if adilo_url.startswith("https://adilo.bigcommand.com/watch/"):
            thumb = adilo_thumbnail_from_watch_url(adilo_url)
        if thumb:
            adilo_embed["image"] = {"url": thumb}
        embeds.append(adilo_embed)

    embeds = embeds[:10]

    # Post digest (stories + adilo card)
    discord_post(content, embeds)

    # Post YouTube as its own raw-url message to force playable card
    if yt_url:
        discord_post(yt_url, None)

    print("[DONE] Digest posted.")
    if yt_url:
        print(f"[DONE] YouTube: {yt_url}")
    if adilo_url:
        print(f"[DONE] Featured Adilo video: {adilo_url}")


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        print("[ERROR] Digest crashed:", ex)
        traceback.print_exc()
        raise
