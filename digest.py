#!/usr/bin/env python3
import os
import re
import json
import time
import html
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional, Tuple

import requests
import feedparser

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
DIGEST_GUARD_WINDOW_MINUTES = int(os.getenv("DIGEST_GUARD_WINDOW_MINUTES", "30").strip())  # tighter default

# ✅ Prevent double-posting per day
DIGEST_POST_ONCE_PER_DAY = (os.getenv("DIGEST_POST_ONCE_PER_DAY", "true").strip().lower() in ("1", "true", "yes", "y", "on"))
DIGEST_CACHE_FILE = os.getenv("DIGEST_CACHE_FILE", ".digest_cache.json").strip()

# YouTube RSS
YOUTUBE_RSS_URL = (os.getenv("YOUTUBE_RSS_URL") or "").strip()
YOUTUBE_CHANNEL_ID = (os.getenv("YOUTUBE_CHANNEL_ID") or "").strip()

# Adilo API
ADILO_API_BASE = "https://adilo-api.bigcommand.com/v1"
ADILO_PUBLIC_KEY = (os.getenv("ADILO_PUBLIC_KEY") or "").strip()
ADILO_SECRET_KEY = (os.getenv("ADILO_SECRET_KEY") or "").strip()
ADILO_PROJECT_ID = (os.getenv("ADILO_PROJECT_ID") or "").strip()

# Optional: skip promo videos by title match (case-insensitive)
ADILO_SKIP_TITLE_REGEX = os.getenv("ADILO_SKIP_TITLE_REGEX", r"(?i)\bpromo\b").strip()

# Optional: manual override (DON’T use in normal operation)
FEATURED_VIDEO_FORCE_ID = (os.getenv("FEATURED_VIDEO_FORCE_ID") or "").strip()

# =========================
# Helpers
# =========================

def safe_text(s: str) -> str:
    s = s or ""
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def domain_of(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""

def now_local() -> datetime:
    return datetime.now(ZoneInfo(DIGEST_GUARD_TZ))

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
    return ("#shorts" in t) or ("/shorts/" in l)

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
# Guard + Daily cache
# =========================

def load_cache() -> Dict:
    try:
        if os.path.exists(DIGEST_CACHE_FILE):
            with open(DIGEST_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        pass
    return {}

def save_cache(obj: Dict) -> None:
    try:
        with open(DIGEST_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, sort_keys=True)
    except Exception:
        pass

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

def should_skip_due_to_daily_cache() -> bool:
    if DIGEST_FORCE_POST:
        return False
    if not DIGEST_POST_ONCE_PER_DAY:
        return False

    cache = load_cache()
    today = now_local().strftime("%Y-%m-%d")
    last = (cache.get("last_post_local_date") or "").strip()

    if last == today:
        print(f"[CACHE] Already posted for {today}. Skipping to prevent double-post.")
        return True

    return False

def mark_posted_today() -> None:
    cache = load_cache()
    today = now_local().strftime("%Y-%m-%d")
    cache["last_post_local_date"] = today
    cache["last_post_local_ts"] = now_local().isoformat()
    save_cache(cache)
    print(f"[CACHE] Marked posted for {today}.")

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

    # light dedupe
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

        for e in d.entries[:25]:
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
# Adilo newest via API (reliable)
# =========================

def _parse_upload_date(s: str) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    # Adilo often returns "YYYY-MM-DD HH:MM:SS"
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
    except Exception:
        pass
    # fallback
    try:
        return datetime.fromisoformat(s).replace(tzinfo=ZoneInfo("UTC"))
    except Exception:
        return None

def _adilo_headers() -> Dict[str, str]:
    # Most common header naming; if your account uses different names we can add more.
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "X-Public-Key": ADILO_PUBLIC_KEY,
        "X-Secret-Key": ADILO_SECRET_KEY,
    }

def _adilo_get_json(url: str, timeout: int = 25) -> Optional[Dict]:
    try:
        r = requests.get(url, headers=_adilo_headers(), timeout=timeout)
        if r.status_code == 200:
            return r.json()
        print(f"[ADILO] API non-200 status={r.status_code} url={url}")
        return None
    except Exception as ex:
        print(f"[ADILO] API error url={url} ex={ex}")
        return None

def adilo_latest_via_api() -> Optional[str]:
    if FEATURED_VIDEO_FORCE_ID:
        forced = FEATURED_VIDEO_FORCE_ID.strip()
        if forced.startswith("http"):
            print(f"[ADILO] Using FEATURED_VIDEO_FORCE_ID as URL: {forced}")
            return forced
        url = f"https://adilo.bigcommand.com/watch/{forced}"
        print(f"[ADILO] Using FEATURED_VIDEO_FORCE_ID: {url}")
        return url

    if not (ADILO_PROJECT_ID and ADILO_PUBLIC_KEY and ADILO_SECRET_KEY):
        print("[ADILO] API not attempted (missing ADILO_PROJECT_ID / ADILO_PUBLIC_KEY / ADILO_SECRET_KEY).")
        return None

    print("[ADILO] API selecting newest by upload_date…")

    # 1) Get total count
    first_url = f"{ADILO_API_BASE}/projects/{ADILO_PROJECT_ID}/files?From=1&To=50"
    j = _adilo_get_json(first_url, timeout=25)
    if not j:
        print("[ADILO] API failed to fetch first page.")
        return None

    meta = j.get("meta") or {}
    total = int(meta.get("total") or 0)
    if total <= 0:
        print("[ADILO] API total=0.")
        return None

    page_size = 50
    last_page_index = max(0, (total - 1) // page_size)

    def page_bounds(pi: int) -> Tuple[int, int]:
        start = pi * page_size + 1
        end = min(total, (pi + 1) * page_size)
        return start, end

    # 2) Scan last N pages (newest uploads tend to be near the end for this API)
    pages_to_scan = int(os.getenv("ADILO_SCAN_PAGES", "6").strip())  # scan last 6 pages
    start_pi = max(0, last_page_index - (pages_to_scan - 1))
    page_indices = list(range(start_pi, last_page_index + 1))

    skip_re = re.compile(ADILO_SKIP_TITLE_REGEX) if ADILO_SKIP_TITLE_REGEX else None

    best_dt = None
    best_id = None
    best_title = None

    for pi in page_indices:
        frm, to = page_bounds(pi)
        url = f"{ADILO_API_BASE}/projects/{ADILO_PROJECT_ID}/files?From={frm}&To={to}"
        jj = _adilo_get_json(url, timeout=25)
        if not jj:
            continue
        payload = jj.get("payload")
        if not isinstance(payload, list) or not payload:
            continue

        # Evaluate ALL items on the page (not just first 20)
        for it in payload:
            fid = (it.get("id") or "").strip()
            if not fid:
                continue

            murl = f"{ADILO_API_BASE}/files/{fid}/meta"
            jm = _adilo_get_json(murl, timeout=25)
            if not jm:
                continue
            p = jm.get("payload") or {}
            title = (p.get("title") or "").strip()
            dt = _parse_upload_date(p.get("upload_date") or "")
            if not dt:
                continue

            # Skip promo if we have other options later
            if skip_re and title and skip_re.search(title):
                # don’t immediately discard; just mark it as “lower priority”
                pass

            if best_dt is None or dt > best_dt:
                best_dt = dt
                best_id = fid
                best_title = title

    if not best_id:
        print("[ADILO] API could not determine newest file.")
        return None

    # 3) If newest is a promo, try again ignoring promos (if possible)
    if best_title and skip_re and skip_re.search(best_title):
        print(f"[ADILO] Newest looks like promo ('{best_title}'). Trying best non-promo…")
        best_dt2 = None
        best_id2 = None

        for pi in page_indices:
            frm, to = page_bounds(pi)
            url = f"{ADILO_API_BASE}/projects/{ADILO_PROJECT_ID}/files?From={frm}&To={to}"
            jj = _adilo_get_json(url, timeout=25)
            if not jj:
                continue
            payload = jj.get("payload")
            if not isinstance(payload, list) or not payload:
                continue

            for it in payload:
                fid = (it.get("id") or "").strip()
                if not fid:
                    continue

                murl = f"{ADILO_API_BASE}/files/{fid}/meta"
                jm = _adilo_get_json(murl, timeout=25)
                if not jm:
                    continue
                p = jm.get("payload") or {}
                title = (p.get("title") or "").strip()
                if title and skip_re.search(title):
                    continue

                dt = _parse_upload_date(p.get("upload_date") or "")
                if not dt:
                    continue

                if best_dt2 is None or dt > best_dt2:
                    best_dt2 = dt
                    best_id2 = fid

        if best_id2:
            best_id = best_id2
            best_dt = best_dt2

    watch_url = f"https://adilo.bigcommand.com/watch/{best_id}"
    print(f"[ADILO] API newest: {watch_url} dt={best_dt.isoformat() if best_dt else 'unknown'}")
    return watch_url

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

    desc = []
    if summary:
        desc.append(summary)
    desc.append(f"Source: {src}")
    # Put the URL in the embed body too (you wanted links under the story)
    desc.append(url)

    return {
        "title": title[:256],
        "url": url,
        "description": "\n".join(desc)[:4096],
    }

# =========================
# Main
# =========================

def main() -> None:
    if not guard_should_post_now():
        return

    if should_skip_due_to_daily_cache():
        return

    items = fetch_all_items()
    if not items:
        print("[DIGEST] No items found in window. Exiting without posting.")
        return

    yt_url, _yt_title = youtube_latest()
    adilo_watch_url = adilo_latest_via_api()

    # Short header content so embeds are the “cards”
    today_str = now_local().strftime("%B %d, %Y")
    blerbs = items[:3]
    blerb_lines = [f"► 🎮 {it['title'][:80]}" for it in blerbs]

    content_lines = []
    if NEWSLETTER_TAGLINE:
        content_lines.append(NEWSLETTER_TAGLINE)
        content_lines.append("")
    content_lines.append(today_str)
    content_lines.append("")
    content_lines.append(f"In Tonight’s Edition of {NEWSLETTER_NAME}…")
    content_lines.extend(blerb_lines)
    content_lines.append("")
    content_lines.append("Tonight’s Top Stories")

    content = "\n".join(content_lines)

    # Build embeds (story cards)
    embeds: List[Dict] = []
    for i, it in enumerate(items, start=1):
        embeds.append(build_story_embed(i, it))

    # Add an Adilo “latest” embed card
    if adilo_watch_url:
        embeds.append({
            "title": "📺 Adilo (latest)",
            "url": adilo_watch_url,
            "description": adilo_watch_url
        })

    # Discord max embeds per message is 10
    embeds = embeds[:10]

    # 1) Post digest with cards
    discord_post(content, embeds)

    # 2) Post standalone YouTube URL to force playable preview
    # (Discord previews work best when the message is ONLY the URL)
    if yt_url:
        discord_post(yt_url, None)

    # 3) Post standalone Adilo watch URL (also helps preview reliability)
    if adilo_watch_url:
        discord_post(adilo_watch_url, None)

    mark_posted_today()

    print("[DONE] Digest posted.")
    if yt_url:
        print(f"[DONE] YouTube: {yt_url}")
    if adilo_watch_url:
        print(f"[DONE] Adilo: {adilo_watch_url}")

if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        print("[ERROR] Digest crashed:", ex)
        traceback.print_exc()
        raise
