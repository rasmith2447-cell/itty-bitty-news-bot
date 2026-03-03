#!/usr/bin/env python3
"""
digest.py — Itty Bitty Gaming News Digest

What this script does (in order):
1) Guard: only post within time window unless DIGEST_FORCE_POST=true
2) Optional: post-once-per-day cache
3) Fetch RSS items (top N in last X hours)
4) Fetch latest YouTube video (RSS), filter Shorts
5) Fetch latest Adilo video (scrape public latest page; no forced ID)
6) Post to Discord via webhook:
   a) Standalone YouTube URL (for playable card unfurl)
   b) Standalone Adilo URL (for card unfurl)
   c) Digest text + story embeds (cards under each story)
7) Export digest text to DIGEST_EXPORT_FILE (default: digest_latest.txt)
"""

from __future__ import annotations

import os
import re
import json
import time
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Optional, Tuple, Dict

import requests
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dateparser


# -------------------------
# ENV HELPERS
# -------------------------
def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    v = env(name, "")
    if v == "":
        return default
    return v.lower() in ("1", "true", "yes", "y", "on")


def env_int(name: str, default: int) -> int:
    v = env(name, "")
    if v == "":
        return default
    try:
        return int(v)
    except ValueError:
        return default


# -------------------------
# DATA MODEL
# -------------------------
@dataclass
class Story:
    title: str
    url: str
    source: str
    published_dt_utc: datetime
    summary: str
    tags: List[str]


# -------------------------
# TIME GUARD
# -------------------------
def guard_should_post_now() -> bool:
    if env_bool("DIGEST_FORCE_POST", False):
        print("[GUARD] DIGEST_FORCE_POST enabled — bypassing time guard.")
        return True

    tz_name = env("DIGEST_GUARD_TZ", "America/Los_Angeles")
    target_hour = env_int("DIGEST_GUARD_LOCAL_HOUR", 19)
    target_minute = env_int("DIGEST_GUARD_LOCAL_MINUTE", 0)
    window_minutes = env_int("DIGEST_GUARD_WINDOW_MINUTES", 30)

    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)

    target_today = now_local.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
    candidates = [
        target_today - timedelta(days=1),
        target_today,
        target_today + timedelta(days=1),
    ]
    closest_target = min(candidates, key=lambda t: abs((now_local - t).total_seconds()))
    delta_min = abs((now_local - closest_target).total_seconds()) / 60.0

    if delta_min <= window_minutes:
        print(
            f"[GUARD] OK. Local now: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            f"Closest target: {closest_target.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            f"Delta={delta_min:.1f}min <= {window_minutes}min"
        )
        return True

    print(
        f"[GUARD] Not within posting window. Local now: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
        f"Closest target: {closest_target.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
        f"Delta={delta_min:.1f}min > {window_minutes}min. Exiting without posting."
    )
    return False


# -------------------------
# ONCE-PER-DAY CACHE
# -------------------------
def cache_load(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) if f.read().strip() else {}
    except Exception:
        return {}


def cache_save(path: str, data: dict) -> None:
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
    except Exception:
        pass


def cache_today_key(tz_name: str) -> str:
    tz = ZoneInfo(tz_name)
    return datetime.now(tz).strftime("%Y-%m-%d")


def should_skip_due_to_once_per_day() -> bool:
    if env_bool("DIGEST_FORCE_POST", False):
        return False

    if not env_bool("DIGEST_POST_ONCE_PER_DAY", False):
        return False

    cache_file = env("DIGEST_CACHE_FILE", ".digest_cache.json")
    tz_name = env("DIGEST_GUARD_TZ", "America/Los_Angeles")
    today = cache_today_key(tz_name)

    data = cache_load(cache_file)
    last_posted = data.get("last_posted_date", "")
    if last_posted == today:
        print(f"[CACHE] Already posted for {today}. Exiting without posting.")
        return True

    return False


def mark_posted_today() -> None:
    if not env_bool("DIGEST_POST_ONCE_PER_DAY", False):
        return

    cache_file = env("DIGEST_CACHE_FILE", ".digest_cache.json")
    tz_name = env("DIGEST_GUARD_TZ", "America/Los_Angeles")
    today = cache_today_key(tz_name)

    data = cache_load(cache_file)
    data["last_posted_date"] = today
    cache_save(cache_file, data)
    print(f"[CACHE] Marked posted for {today}.")


# -------------------------
# HTTP HELPERS
# -------------------------
def http_get(url: str, timeout: int = 25) -> requests.Response:
    headers = {
        "User-Agent": env("USER_AGENT", "IttyBittyGamingNews/Digest"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    return requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)


def clean_text(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s


def truncate(s: str, n: int) -> str:
    s = s or ""
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)].rstrip() + "…"


# -------------------------
# RSS FETCH
# -------------------------
DEFAULT_FEEDS = [
    "https://www.bluesnews.com/news/news_1_0.rdf",
    "https://www.ign.com/rss/v2/articles/feed?categories=games",
    "https://www.gamespot.com/feeds/mashup/",
    "https://gamerant.com/feed",
    "https://www.polygon.com/rss/index.xml",
    "https://www.videogameschronicle.com/feed/",
    "https://www.gematsu.com/feed",
]


def parse_published_dt(entry) -> Optional[datetime]:
    # feedparser sometimes gives published_parsed
    if getattr(entry, "published_parsed", None):
        try:
            return datetime.fromtimestamp(time.mktime(entry.published_parsed), tz=ZoneInfo("UTC"))
        except Exception:
            pass

    # try published / updated strings
    for k in ("published", "updated", "created"):
        v = getattr(entry, k, None)
        if v:
            try:
                dt = dateparser.parse(v)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=ZoneInfo("UTC"))
                return dt.astimezone(ZoneInfo("UTC"))
            except Exception:
                continue

    return None


def extract_tags(entry) -> List[str]:
    tags = []
    if getattr(entry, "tags", None):
        for t in entry.tags:
            term = getattr(t, "term", None)
            if term:
                tags.append(clean_text(term))
    # Dedup, keep order
    seen = set()
    out = []
    for t in tags:
        tl = t.lower()
        if tl not in seen:
            seen.add(tl)
            out.append(t)
    return out[:6]


def fetch_feed_items(feed_url: str) -> List[Story]:
    try:
        print(f"[RSS] GET {feed_url}")
        d = feedparser.parse(feed_url)
        if getattr(d, "bozo", 0):
            # bozo=1 is often “encoding mismatch”, not fatal
            exc = getattr(d, "bozo_exception", None)
            if exc:
                print(f"[RSS] bozo=1 for {feed_url}: {exc}")
        items: List[Story] = []

        for e in getattr(d, "entries", []):
            title = clean_text(getattr(e, "title", ""))
            link = clean_text(getattr(e, "link", ""))
            if not title or not link:
                continue

            dt = parse_published_dt(e)
            if not dt:
                continue

            # Best-effort summary
            summary = ""
            for k in ("summary", "description"):
                v = getattr(e, k, None)
                if v:
                    # strip HTML
                    soup = BeautifulSoup(v, "html.parser")
                    summary = clean_text(soup.get_text(" ", strip=True))
                    break

            source = ""
            # Try feed title first, else domain
            feed_title = getattr(d.feed, "title", "") if getattr(d, "feed", None) else ""
            source = clean_text(feed_title) if feed_title else ""
            if not source:
                m = re.search(r"https?://([^/]+)/", link)
                source = m.group(1) if m else "source"

            tags = extract_tags(e)

            items.append(
                Story(
                    title=title,
                    url=link,
                    source=source,
                    published_dt_utc=dt,
                    summary=summary,
                    tags=tags,
                )
            )

        return items
    except Exception as ex:
        print(f"[RSS] Feed failed: {feed_url} ({ex})")
        return []


def build_story_key(story: Story) -> str:
    # Stable dedupe key
    raw = (story.url or "") + "|" + (story.title or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def select_top_stories(all_items: List[Story], window_hours: int, top_n: int, max_per_source: int) -> List[Story]:
    now_utc = datetime.now(ZoneInfo("UTC"))
    cutoff = now_utc - timedelta(hours=window_hours)

    # filter by time
    in_window = [s for s in all_items if s.published_dt_utc >= cutoff]
    print(f"[DIGEST] After {window_hours}h window filter: {len(in_window)} item(s)")

    # sort newest first
    in_window.sort(key=lambda s: s.published_dt_utc, reverse=True)

    # dedupe by key + enforce max per source
    out: List[Story] = []
    seen_keys = set()
    per_source: Dict[str, int] = {}

    for s in in_window:
        k = build_story_key(s)
        if k in seen_keys:
            continue
        seen_keys.add(k)

        src = (s.source or "").lower()
        per_source[src] = per_source.get(src, 0) + 1
        if per_source[src] > max_per_source:
            continue

        out.append(s)
        if len(out) >= top_n:
            break

    return out


# -------------------------
# YOUTUBE LATEST (RSS) + FILTER SHORTS
# -------------------------
def is_youtube_short(entry) -> bool:
    title = clean_text(getattr(entry, "title", "")).lower()
    link = clean_text(getattr(entry, "link", "")).lower()

    if "#shorts" in title or "shorts" == title.strip():
        return True
    if "/shorts/" in link:
        return True
    # Sometimes Shorts mention "short" in categories, but that’s noisy—avoid false positives.
    return False


def fetch_latest_youtube_url() -> Optional[str]:
    rss_url = env("YOUTUBE_RSS_URL", "")
    if not rss_url:
        return None

    print(f"[YT] Fetch RSS: {rss_url}")
    try:
        d = feedparser.parse(rss_url)
        entries = getattr(d, "entries", []) or []
        for e in entries:
            if is_youtube_short(e):
                continue
            link = clean_text(getattr(e, "link", ""))
            if link:
                return link
        return None
    except Exception as ex:
        print(f"[YT] Failed to fetch RSS: {ex}")
        return None


# -------------------------
# ADILO LATEST (SCRAPE PUBLIC PAGE) — NO FORCED ID
# -------------------------
ADILO_ID_PATTERNS = [
    # /c/.../video?id=XXXX
    re.compile(r"video\?id=([A-Za-z0-9_\-]+)"),
    # /watch/XXXX
    re.compile(r"/watch/([A-Za-z0-9_\-]+)"),
    # stage/videos/XXXX
    re.compile(r"/stage/videos/([A-Za-z0-9_\-]+)"),
]


def adilo_extract_first_id(html: str) -> Optional[str]:
    if not html:
        return None

    # Search in order of appearance by scanning left-to-right
    # We'll find the earliest match among the known patterns
    earliest = None  # tuple(idx, id)
    for pat in ADILO_ID_PATTERNS:
        for m in pat.finditer(html):
            idx = m.start()
            vid = m.group(1)
            if not vid:
                continue
            if earliest is None or idx < earliest[0]:
                earliest = (idx, vid)
    return earliest[1] if earliest else None


def adilo_build_watch_url(video_id: str) -> str:
    # Prefer /watch/ID for a clean unfurl.
    return f"https://adilo.bigcommand.com/watch/{video_id}"


def fetch_latest_adilo_url() -> str:
    latest_page = env("ADILO_PUBLIC_LATEST_PAGE", "https://adilo.bigcommand.com/c/ittybittygamingnews/video")
    home_page = env("ADILO_PUBLIC_HOME_PAGE", "https://adilo.bigcommand.com/c/ittybittygamingnews/home")

    # Try several variants to beat caching / partial renders
    cb = str(int(time.time() * 1000))
    candidates = [
        latest_page,
        f"{latest_page}?cb={cb}",
        latest_page.rstrip("/") + f"/?cb={cb}",
        f"{latest_page}?video=latest&cb={cb}",
        f"{latest_page}?id=&cb={cb}",  # sometimes triggers server-side resolution
    ]

    timeout = env_int("ADILO_SCRAPE_TIMEOUT", 25)

    for url in candidates:
        try:
            print(f"[ADILO] SCRAPE attempt=1 timeout={timeout} url={url}")
            r = http_get(url, timeout=timeout)

            # If the final URL includes ?id=..., trust it immediately
            if "video?id=" in (r.url or ""):
                m = re.search(r"video\?id=([A-Za-z0-9_\-]+)", r.url)
                if m:
                    vid = m.group(1)
                    if vid:
                        final = adilo_build_watch_url(vid)
                        print(f"[ADILO] Redirect resolved id={vid} -> {final}")
                        return final

            html = r.text or ""
            # Some pages are JS-heavy; but the id often appears in the HTML anyway.
            vid = adilo_extract_first_id(html)
            if vid:
                final = adilo_build_watch_url(vid)
                print(f"[ADILO] Found candidate id={vid} -> {final}")
                return final

            # Try meta og:url / canonical
            soup = BeautifulSoup(html, "html.parser")
            og = soup.find("meta", attrs={"property": "og:url"})
            if og and og.get("content"):
                og_url = clean_text(og.get("content"))
                m = re.search(r"video\?id=([A-Za-z0-9_\-]+)", og_url) or re.search(r"/watch/([A-Za-z0-9_\-]+)", og_url)
                if m:
                    vid2 = m.group(1)
                    final = adilo_build_watch_url(vid2)
                    print(f"[ADILO] og:url resolved id={vid2} -> {final}")
                    return final

        except requests.exceptions.Timeout:
            print(f"[ADILO] Timeout url={url} (timeout={timeout})")
        except Exception as ex:
            print(f"[ADILO] SCRAPE error url={url}: {ex}")

    print(f"[ADILO] No IDs found on latest page; falling back: {home_page}")
    return home_page


# -------------------------
# DISCORD POSTING
# -------------------------
def discord_webhook_url() -> str:
    # The ONLY webhook digest posts to:
    # You are already mapping this in content_board.yml via DISCORD_WEBHOOK_URL env.
    return env("DISCORD_WEBHOOK_URL", "")


def discord_post_content_only(content: str) -> None:
    url = discord_webhook_url()
    if not url:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL")

    payload = {"content": content}
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()


def discord_post_digest_with_embeds(content: str, embeds: list) -> None:
    url = discord_webhook_url()
    if not url:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL")

    payload = {"content": content, "embeds": embeds}
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()


def story_to_embed(i: int, s: Story) -> dict:
    # Discord embed limits are tight; keep it lean and consistent.
    # Put the link INSIDE the embed (url field) so it’s a “card” under the story.
    published_local = s.published_dt_utc.astimezone(ZoneInfo(env("DIGEST_GUARD_TZ", "America/Los_Angeles")))
    footer = f"{clean_text(s.source)} • {published_local.strftime('%b %d, %Y %I:%M %p %Z')}"

    # Add tags (if present) in a subtle way
    tag_line = ""
    if s.tags:
        tag_line = "Tags: " + ", ".join(s.tags[:5])

    desc_parts = []
    if s.summary:
        desc_parts.append(truncate(s.summary, 280))
    if tag_line:
        desc_parts.append(tag_line)
    desc_parts.append(f"[Read the full story]({s.url})")

    return {
        "title": f"{i}) {truncate(s.title, 220)}",
        "url": s.url,
        "description": "\n".join(desc_parts),
        "footer": {"text": footer},
    }


# -------------------------
# DIGEST CONTENT (TEXT)
# -------------------------
def build_digest_text(stories: List[Story]) -> str:
    tz_name = env("DIGEST_GUARD_TZ", "America/Los_Angeles")
    today_local = datetime.now(ZoneInfo(tz_name)).strftime("%B %d, %Y")

    name = env("NEWSLETTER_NAME", "Itty Bitty Gaming News")
    tagline = env("NEWSLETTER_TAGLINE", "Snackable daily gaming news — five days a week.")

    lines = []
    lines.append(tagline)
    lines.append("")
    lines.append(today_local)
    lines.append("")
    lines.append(f"In Tonight’s Edition of {name}…")
    for s in stories[:3]:
        lines.append(f"► 🎮 {truncate(s.title, 90)}")
    lines.append("")
    lines.append("Tonight’s Top Stories")
    # IMPORTANT: Do NOT include raw URLs here (keeps unfurls clean + embeds do the linking)
    for idx, s in enumerate(stories, start=1):
        lines.append(f"{idx}) {truncate(s.title, 140)}")
    return "\n".join(lines).strip()


def export_digest_text(text: str) -> None:
    out_path = env("DIGEST_EXPORT_FILE", "digest_latest.txt")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text.strip() + "\n")
        print(f"[EXPORT] Wrote {out_path}")
    except Exception as ex:
        print(f"[EXPORT] Failed to write {out_path}: {ex}")


# -------------------------
# MAIN
# -------------------------
def main() -> None:
    # Guard
    if not guard_should_post_now():
        # exit 0 so Actions doesn’t mark failure
        return

    # Once-per-day cache
    if should_skip_due_to_once_per_day():
        return

    # Config
    window_hours = env_int("DIGEST_WINDOW_HOURS", 24)
    top_n = env_int("DIGEST_TOP_N", 5)
    max_per_source = env_int("DIGEST_MAX_PER_SOURCE", 1)

    # Feed list
    feed_urls_raw = env("FEED_URLS", "")
    if feed_urls_raw:
        feed_urls = [u.strip() for u in feed_urls_raw.split(",") if u.strip()]
    else:
        feed_urls = DEFAULT_FEEDS

    # Fetch stories
    all_items: List[Story] = []
    for u in feed_urls:
        all_items.extend(fetch_feed_items(u))

    if not all_items:
        print("[DIGEST] No feed items fetched. Exiting without posting.")
        return

    stories = select_top_stories(all_items, window_hours=window_hours, top_n=top_n, max_per_source=max_per_source)
    if not stories:
        print("[DIGEST] No items found in window. Exiting without posting.")
        return

    # Build digest text + embeds (cards under story)
    digest_text = build_digest_text(stories)
    embeds = [story_to_embed(i, s) for i, s in enumerate(stories, start=1)]

    # Export for OnlySocial / website etc.
    export_digest_text(digest_text)

    # Fetch featured URLs
    yt_url = fetch_latest_youtube_url()
    adilo_url = fetch_latest_adilo_url()

    # Post to Discord
    # Key behavior: standalone messages for playable cards (YouTube/Adilo),
    # then the digest + embeds.
    try:
        # 1) YouTube unfurl card (playable) — MUST be stand-alone message
        if yt_url:
            discord_post_content_only(yt_url)
        else:
            print("[YT] No usable YouTube URL found (or only Shorts). Skipping YouTube post.")

        # 2) Adilo unfurl card — MUST be stand-alone message
        if adilo_url and adilo_url != env("ADILO_PUBLIC_HOME_PAGE", "https://adilo.bigcommand.com/c/ittybittygamingnews/home"):
            discord_post_content_only(adilo_url)
        else:
            # Still post it if you want visibility; but it will be “home”
            # Uncomment next line if you prefer always posting even when fallback:
            # discord_post_content_only(adilo_url)
            print(f"[ADILO] Using fallback/no-video URL. Not posting standalone: {adilo_url}")

        # 3) Digest + story embeds
        discord_post_digest_with_embeds(digest_text, embeds)

    except requests.exceptions.HTTPError as ex:
        # Print helpful response body for Discord 400s
        resp = getattr(ex, "response", None)
        if resp is not None:
            print("[DISCORD] HTTP error body:", resp.text[:2000])
        raise

    # Mark cache
    mark_posted_today()

    print("[DONE] Digest posted.")
    if yt_url:
        print(f"[DONE] YouTube: {yt_url}")
    print(f"[DONE] Featured Adilo video: {adilo_url}")


if __name__ == "__main__":
    main()
