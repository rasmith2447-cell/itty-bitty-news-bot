#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

import requests
import feedparser
from bs4 import BeautifulSoup
from email.utils import parsedate_to_datetime

# =========================
# ENV + CONSTANTS
# =========================

USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNews/Digest").strip()

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

NEWSLETTER_NAME = os.getenv("NEWSLETTER_NAME", "Itty Bitty Gaming News").strip()
NEWSLETTER_TAGLINE = os.getenv("NEWSLETTER_TAGLINE", "Snackable daily gaming news — five days a week.").strip()

DIGEST_WINDOW_HOURS = int(os.getenv("DIGEST_WINDOW_HOURS", "24").strip())
DIGEST_TOP_N = int(os.getenv("DIGEST_TOP_N", "5").strip())
DIGEST_MAX_PER_SOURCE = int(os.getenv("DIGEST_MAX_PER_SOURCE", "1").strip())

# Guard settings (7pm PT default)
DIGEST_GUARD_TZ = os.getenv("DIGEST_GUARD_TZ", "America/Los_Angeles").strip()
DIGEST_GUARD_LOCAL_HOUR = int(os.getenv("DIGEST_GUARD_LOCAL_HOUR", "19").strip())
DIGEST_GUARD_LOCAL_MINUTE = int(os.getenv("DIGEST_GUARD_LOCAL_MINUTE", "0").strip())
DIGEST_GUARD_WINDOW_MINUTES = int(os.getenv("DIGEST_GUARD_WINDOW_MINUTES", "120").strip())

DIGEST_FORCE_POST = os.getenv("DIGEST_FORCE_POST", "").strip().lower() in ("1", "true", "yes", "y")

# Feeds: newline or comma-separated
FEED_URLS_RAW = os.getenv("FEED_URLS", "").strip()
DEFAULT_FEEDS = [
    "https://www.bluesnews.com/news/news_1_0.rdf",
    "https://www.ign.com/rss/v2/articles/feed?categories=games",
    "https://www.gamespot.com/feeds/mashup/",
    "https://gamerant.com/feed",
    "https://www.polygon.com/rss/index.xml",
    "https://www.videogameschronicle.com/feed/",
    "https://www.gematsu.com/feed",
]

# YouTube auto
YOUTUBE_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "").strip()  # you can set this in workflow
YOUTUBE_FEATURED_URL = os.getenv("YOUTUBE_FEATURED_URL", "").strip()  # optional override
YOUTUBE_FEATURED_TITLE = os.getenv("YOUTUBE_FEATURED_TITLE", "").strip()  # optional override

# Adilo public pages
ADILO_PUBLIC_LATEST_PAGE = os.getenv(
    "ADILO_PUBLIC_LATEST_PAGE", "https://adilo.bigcommand.com/c/ittybittygamingnews/video"
).strip()
ADILO_PUBLIC_HOME_PAGE = os.getenv(
    "ADILO_PUBLIC_HOME_PAGE", "https://adilo.bigcommand.com/c/ittybittygamingnews/home"
).strip()

# If you set FEATURED_VIDEO_FORCE_ID, it will force Adilo to that ID (ONLY use when debugging)
FEATURED_VIDEO_FORCE_ID = os.getenv("FEATURED_VIDEO_FORCE_ID", "").strip()

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})

# =========================
# SMALL UTILITIES
# =========================

def clamp(s: str, max_len: int) -> str:
    s = (s or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1].rstrip() + "…"


def strip_html(html: str) -> str:
    if not html:
        return ""
    try:
        soup = BeautifulSoup(str(html), "html.parser")
        text = soup.get_text(" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()
        return text
    except Exception:
        # fallback: remove tags crudely
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return text


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        host = host.replace("www.", "")
        return host
    except Exception:
        return ""


def parse_entry_datetime(entry) -> datetime | None:
    # Prefer feedparser's structured time
    if getattr(entry, "published_parsed", None):
        try:
            dt = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
            return dt
        except Exception:
            pass
    # Fall back to RFC822 parsing
    for k in ("published", "updated", "created"):
        if getattr(entry, k, None):
            try:
                dt = parsedate_to_datetime(getattr(entry, k))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                continue
    return None


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
        microsecond=0,
    )

    candidates = [target_today - timedelta(days=1), target_today, target_today + timedelta(days=1)]
    closest_target = min(candidates, key=lambda t: abs((now_local - t).total_seconds()))
    delta_min = abs((now_local - closest_target).total_seconds()) / 60.0

    if delta_min <= DIGEST_GUARD_WINDOW_MINUTES:
        print(
            f"[GUARD] OK. Local now: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            f"Closest target: {closest_target.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            f"Delta={delta_min:.1f}min <= {DIGEST_GUARD_WINDOW_MINUTES}min"
        )
        return True

    print(
        f"[GUARD] Not within posting window. Local now: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
        f"Closest target: {closest_target.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
        f"Delta={delta_min:.1f}min > {DIGEST_GUARD_WINDOW_MINUTES}min. Exiting without posting."
    )
    return False


# =========================
# FILTERS (NO DEALS / NO OPINION / NO RUMORS)
# =========================

BAD_TITLE_PATTERNS = [
    r"\bbest\b",
    r"\btop\s+\d+\b",
    r"\branking\b",
    r"\branked\b",
    r"\bopinion\b",
    r"\bdebate\b",
    r"\bpoll\b",
    r"\bletters\b",
    r"\breview\b",
    r"\bpreview\b",
    r"\bdeals?\b",
    r"\bdiscount\b",
    r"\bcheapest\b",
    r"\bprice drop\b",
    r"\bnow \d+% off\b",
    r"\bguide\b",
    r"\bhow to\b",
    r"\bexplained\b",
    r"\bhistory of\b",
    r"\bupdate\)\b",
    r"\bupdate\b.*\bhistory\b",
    r"\brumou?r\b",
    r"\bspeculation\b",
    r"\bleak\b",
    r"\bleaked\b",
    r"\binsider\b",
    r"\breportedly\b",
    r"\bmaybe\b",
    r"\bmight\b",
]

def is_bad_story(title: str, summary: str) -> bool:
    t = (title or "").lower()
    s = (summary or "").lower()
    blob = t + " " + s
    for pat in BAD_TITLE_PATTERNS:
        if re.search(pat, blob, flags=re.IGNORECASE):
            return True
    return False


# =========================
# TAGGING
# =========================

def make_tags(title: str, source_domain: str) -> list[str]:
    t = (title or "").lower()
    tags = []

    # Platforms / ecosystems
    if "playstation" in t or "ps5" in t or "ps4" in t or "sony" in t:
        tags.append("#PlayStation")
    if "xbox" in t or "microsoft" in t:
        tags.append("#Xbox")
    if "nintendo" in t or "switch" in t:
        tags.append("#Nintendo")
    if "steam" in t or "pc" in t:
        tags.append("#PC")
    if "mobile" in t or "ios" in t or "android" in t:
        tags.append("#Mobile")
    if "vr" in t:
        tags.append("#VR")

    # News types
    if any(k in t for k in ["launch", "releases", "release", "drops", "out now"]):
        tags.append("#Release")
    if any(k in t for k in ["update", "patch", "season", "hotfix"]):
        tags.append("#Update")
    if any(k in t for k in ["announced", "reveal", "revealed", "trailer"]):
        tags.append("#Announcement")
    if any(k in t for k in ["layoff", "laid off", "cuts", "shutdown", "closed", "closure"]):
        tags.append("#Industry")
    if any(k in t for k in ["lawsuit", "court", "legal"]):
        tags.append("#Legal")
    if any(k in t for k in ["hack", "breach", "leak"]):
        tags.append("#Security")

    # Keep it short
    uniq = []
    for tag in tags:
        if tag not in uniq:
            uniq.append(tag)
    return uniq[:4]


# =========================
# RSS FETCH
# =========================

def get_feed_urls() -> list[str]:
    if not FEED_URLS_RAW:
        return DEFAULT_FEEDS
    parts = []
    for line in FEED_URLS_RAW.replace(",", "\n").splitlines():
        u = line.strip()
        if u:
            parts.append(u)
    return parts if parts else DEFAULT_FEEDS


def fetch_feed_items() -> list[dict]:
    feeds = get_feed_urls()
    now_utc = datetime.now(timezone.utc)
    window_start = now_utc - timedelta(hours=DIGEST_WINDOW_HOURS)

    items = []
    seen_links = set()

    for url in feeds:
        print(f"[RSS] GET {url}")
        try:
            resp = SESSION.get(url, timeout=25)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
            if getattr(parsed, "bozo", 0):
                # Don't hard-fail; many feeds set bozo for encoding warnings.
                print(f"[RSS] bozo=1 for {url}: {getattr(parsed, 'bozo_exception', '')}")

            for e in parsed.entries:
                title = (getattr(e, "title", "") or "").strip()
                link = (getattr(e, "link", "") or "").strip()
                summary = strip_html(getattr(e, "summary", "") or getattr(e, "description", "") or "")

                if not title or not link:
                    continue

                if link in seen_links:
                    continue

                dt = parse_entry_datetime(e)
                if not dt:
                    continue

                if dt < window_start:
                    continue

                if is_bad_story(title, summary):
                    continue

                src = domain_of(link) or domain_of(url)
                item = {
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "dt": dt,
                    "source": src,
                }
                items.append(item)
                seen_links.add(link)

        except Exception as ex:
            print(f"[RSS] Feed failed: {url} ({ex})")
            continue

    # Sort newest first
    items.sort(key=lambda x: x["dt"], reverse=True)
    return items


def select_top_stories(items: list[dict]) -> list[dict]:
    # Cluster by canonical-ish key to reduce duplicates across sources
    # Simple approach: normalize title
    def norm_title(t: str) -> str:
        t = (t or "").lower()
        t = re.sub(r"[\W_]+", " ", t).strip()
        t = re.sub(r"\b(the|a|an|and|or|to|for|of|in|on|with)\b", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        return t

    chosen = []
    per_source = {}
    seen_title_norm = set()

    for it in items:
        src = it["source"]
        per_source.setdefault(src, 0)

        if per_source[src] >= DIGEST_MAX_PER_SOURCE:
            continue

        nt = norm_title(it["title"])
        if nt in seen_title_norm:
            continue

        chosen.append(it)
        per_source[src] += 1
        seen_title_norm.add(nt)

        if len(chosen) >= DIGEST_TOP_N:
            break

    return chosen


# =========================
# DISCORD POSTING (ONE MESSAGE PER STORY FOR "CARD UNDER STORY")
# =========================

def discord_post(content: str, embeds: list[dict] | None = None):
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL")

    payload = {"content": content}
    if embeds:
        payload["embeds"] = embeds[:10]

    r = SESSION.post(DISCORD_WEBHOOK_URL, json=payload, timeout=25)
    if r.status_code >= 400:
        # Print response body to help debug 400s without exposing secrets
        print(f"[DISCORD] HTTP {r.status_code}: {r.text[:500]}")
    r.raise_for_status()


def build_story_embed(item: dict) -> dict:
    title = clamp(item["title"], 256)
    url = item["link"]
    summary = clamp(item["summary"], 350)  # keep safe for Discord embed
    src = item["source"]

    tags = make_tags(item["title"], src)
    tag_line = " ".join(tags) if tags else ""

    desc_parts = []
    if summary:
        desc_parts.append(summary)
    if tag_line:
        desc_parts.append(f"\n{tag_line}")

    embed = {
        "title": title,
        "url": url,
        "description": clamp("".join(desc_parts), 4096),
        "footer": {"text": f"Source: {src}"},
    }

    # Try to enrich with og:image (best-effort; don't fail the digest)
    try:
        og = fetch_og(url, timeout=10)
        if og.get("image"):
            embed["image"] = {"url": og["image"]}
        if og.get("site_name") and not embed.get("author"):
            embed["author"] = {"name": og["site_name"]}
    except Exception:
        pass

    return embed


# =========================
# OG / OEMBED HELPERS (THUMBNAILS)
# =========================

def fetch_og(url: str, timeout: int = 10) -> dict:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*"}
    r = SESSION.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    html = r.text

    soup = BeautifulSoup(html, "html.parser")

    def meta(prop: str) -> str:
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        return (tag.get("content") if tag else "") or ""

    og_title = meta("og:title")
    og_image = meta("og:image")
    og_site = meta("og:site_name")

    return {"title": og_title.strip(), "image": og_image.strip(), "site_name": og_site.strip()}


def fetch_youtube_oembed(url: str, timeout: int = 10) -> dict:
    # No API key needed
    oembed = f"https://www.youtube.com/oembed?url={url}&format=json"
    r = SESSION.get(oembed, timeout=timeout)
    r.raise_for_status()
    return r.json()


# =========================
# YOUTUBE LATEST (RSS)
# =========================

def get_latest_youtube_video() -> tuple[str, str, str]:
    """
    Returns (url, title, thumbnail_url). Best-effort.
    If YOUTUBE_FEATURED_URL is set, uses it as override.
    """
    if YOUTUBE_FEATURED_URL:
        title = YOUTUBE_FEATURED_TITLE or "YouTube (latest)"
        thumb = ""
        try:
            data = fetch_youtube_oembed(YOUTUBE_FEATURED_URL, timeout=10)
            title = data.get("title") or title
            thumb = data.get("thumbnail_url") or ""
        except Exception:
            pass
        return (YOUTUBE_FEATURED_URL, title, thumb)

    channel_id = YOUTUBE_CHANNEL_ID.strip()
    if not channel_id:
        # If you didn't set it in workflow, just return nothing
        return ("", "", "")

    rss_candidates = [
        f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
        # uploads playlist id trick: UCxxxx -> UUxxxx
        f"https://www.youtube.com/feeds/videos.xml?playlist_id=UU{channel_id[2:]}" if channel_id.startswith("UC") else "",
    ]
    rss_candidates = [u for u in rss_candidates if u]

    for rss in rss_candidates:
        try:
            print(f"[YT] Fetch RSS: {rss}")
            r = SESSION.get(rss, timeout=20)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
            if not parsed.entries:
                continue
            e = parsed.entries[0]
            url = (getattr(e, "link", "") or "").strip()
            title = (getattr(e, "title", "") or "").strip()
            thumb = ""
            try:
                data = fetch_youtube_oembed(url, timeout=10)
                title = data.get("title") or title
                thumb = data.get("thumbnail_url") or ""
            except Exception:
                pass
            return (url, title, thumb)
        except Exception as ex:
            print(f"[YT] RSS failed: {rss} ({ex})")
            continue

    return ("", "", "")


def build_youtube_embed(url: str, title: str, thumb: str) -> dict:
    emb = {
        "title": clamp(title or "YouTube (latest)", 256),
        "url": url,
        "description": "",
    }
    if thumb:
        emb["image"] = {"url": thumb}
    emb["footer"] = {"text": "YouTube (latest)"}
    return emb


# =============================
# ADILO (ROBUST PUBLIC SCRAPE)
# =============================

def _adilo_http_get(url: str, timeout: int = 20) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    r = SESSION.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text


def _adilo_extract_ids_from_html(html: str) -> list[str]:
    if not html:
        return []

    text = html

    patterns = [
        r"https?://adilo\.bigcommand\.com/watch/([A-Za-z0-9_-]{6,})",
        r"/watch/([A-Za-z0-9_-]{6,})",
        r"video\?id=([A-Za-z0-9_-]{6,})",
        r"/stage/videos/([A-Za-z0-9_-]{6,})",
        r"https?://adilo\.bigcommand\.com/stage/videos/([A-Za-z0-9_-]{6,})",
    ]

    found: list[str] = []
    seen = set()

    for pat in patterns:
        for m in re.findall(pat, text):
            vid = m.strip()
            if not vid or vid in seen:
                continue
            seen.add(vid)
            found.append(vid)

    # Parse DOM attributes too
    try:
        soup = BeautifulSoup(str(html), "html.parser")
        attrs = []

        for iframe in soup.find_all("iframe"):
            attrs.append(iframe.get("src") or "")

        for a in soup.find_all("a"):
            attrs.append(a.get("href") or "")

        for meta in soup.find_all("meta"):
            attrs.append(meta.get("content") or "")

        blob = "\n".join(attrs)
        for pat in patterns:
            for m in re.findall(pat, blob):
                vid = m.strip()
                if not vid or vid in seen:
                    continue
                seen.add(vid)
                found.append(vid)

    except Exception:
        pass

    return found


def scrape_latest_adilo_watch_url() -> str:
    # Debug / emergency override (DO NOT leave enabled long-term)
    if FEATURED_VIDEO_FORCE_ID:
        forced = f"https://adilo.bigcommand.com/watch/{FEATURED_VIDEO_FORCE_ID}"
        print(f"[ADILO] Using FEATURED_VIDEO_FORCE_ID: {forced}")
        return forced

    base = ADILO_PUBLIC_LATEST_PAGE.rstrip("/")

    cb = f"cb={int(time.time())}{random.randint(100,999)}"
    candidates = [
        base,
        f"{base}?{cb}",
        f"{base}/?{cb}",
        f"{base}?id=&{cb}",
        f"{base}?video=latest&{cb}",
        ADILO_PUBLIC_HOME_PAGE.rstrip("/"),
        f"{ADILO_PUBLIC_HOME_PAGE.rstrip('/')}?{cb}",
    ]

    attempts = [
        {"timeout": 25, "sleep": 0.0},
        {"timeout": 18, "sleep": 1.0},
        {"timeout": 12, "sleep": 1.5},
    ]

    for i, att in enumerate(attempts, start=1):
        timeout = att["timeout"]
        sleep_s = att["sleep"]

        if sleep_s:
            time.sleep(sleep_s)

        for url in candidates:
            try:
                print(f"[ADILO] SCRAPE attempt={i} timeout={timeout} url={url}")
                html = _adilo_http_get(url, timeout=timeout)
                ids = _adilo_extract_ids_from_html(html)

                if ids:
                    picked = ids[0]
                    watch_url = f"https://adilo.bigcommand.com/watch/{picked}"
                    print(f"[ADILO] Found candidate id={picked} -> {watch_url}")
                    return watch_url

            except requests.exceptions.ReadTimeout:
                print(f"[ADILO] Timeout url={url} (timeout={timeout})")
                continue
            except Exception as ex:
                print(f"[ADILO] Error url={url}: {ex}")
                continue

    print(f"[ADILO] Falling back: {ADILO_PUBLIC_HOME_PAGE}")
    return ADILO_PUBLIC_HOME_PAGE


def build_adilo_embed(url: str) -> dict:
    # Try to pull a thumbnail via og:image from the watch page
    title = "Adilo (latest)"
    thumb = ""
    try:
        og = fetch_og(url, timeout=12)
        if og.get("title"):
            title = og["title"]
        if og.get("image"):
            thumb = og["image"]
    except Exception:
        pass

    emb = {"title": clamp(title, 256), "url": url, "description": "", "footer": {"text": "Adilo (latest)"}}
    if thumb:
        emb["image"] = {"url": thumb}
    return emb


# =========================
# MESSAGE BUILDERS
# =========================

def make_bullets(stories: list[dict], count: int = 3) -> str:
    bullets = []
    for it in stories[:count]:
        bullets.append(f"► 🎮 {clamp(it['title'], 80)}")
    return "\n".join(bullets)


def format_story_text(idx: int, item: dict) -> str:
    title = item["title"].strip()
    summary = clamp(item["summary"], 340)
    src = item["source"]
    link = item["link"]
    tags = make_tags(title, src)
    tag_line = (" ".join(tags)).strip()

    lines = []
    lines.append(f"{idx}) {title}")
    if summary:
        lines.append(summary)
    lines.append(f"Source: {src}")
    lines.append(link)
    if tag_line:
        lines.append(tag_line)
    return "\n".join(lines).strip()


# =========================
# MAIN
# =========================

def main():
    if not guard_should_post_now():
        # Exit cleanly so Actions doesn't show a failure
        return

    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL")

    items = fetch_feed_items()
    if not items:
        print("[DIGEST] No feed items fetched. Exiting without posting.")
        return

    stories = select_top_stories(items)
    if not stories:
        print("[DIGEST] No stories after filters. Exiting without posting.")
        return

    # Header / intro message
    tz = ZoneInfo(DIGEST_GUARD_TZ)
    now_local = datetime.now(tz)
    date_str = now_local.strftime("%B %d, %Y")

    intro_lines = []
    if NEWSLETTER_TAGLINE:
        intro_lines.append(NEWSLETTER_TAGLINE)
        intro_lines.append("")
    intro_lines.append(date_str)
    intro_lines.append("")
    intro_lines.append(f"In Tonight’s Edition of {NEWSLETTER_NAME}…")
    intro_lines.append(make_bullets(stories, count=3))
    intro_lines.append("")
    intro_lines.append("Tonight’s Top Stories")
    intro_msg = "\n".join(intro_lines).strip()

    discord_post(intro_msg)
    time.sleep(0.4)

    # One message per story so the "card" appears directly underneath
    for i, it in enumerate(stories, start=1):
        text = format_story_text(i, it)
        embed = build_story_embed(it)

        # Keep content safe-ish (Discord content max 2000 chars)
        text = clamp(text, 1800)

        discord_post(text, embeds=[embed])
        time.sleep(0.6)

    # Featured videos section (YouTube above Adilo, as requested)
    yt_url, yt_title, yt_thumb = get_latest_youtube_video()
    adilo_url = scrape_latest_adilo_watch_url()

    # Post YouTube (if available)
    if yt_url:
        yt_text = "▶️ YouTube (latest)\n" + yt_url
        yt_embed = build_youtube_embed(yt_url, yt_title, yt_thumb)
        discord_post(yt_text, embeds=[yt_embed])
        time.sleep(0.6)
    else:
        print("[DONE] YouTube not available (no channel id / RSS failed).")

    # Post Adilo
    adilo_text = "📺 Adilo (latest)\n" + adilo_url
    adilo_embed = build_adilo_embed(adilo_url)
    discord_post(adilo_text, embeds=[adilo_embed])

    print("[DONE] Digest posted.")
    if yt_url:
        print(f"[DONE] YouTube: {yt_url}")
    print(f"[DONE] Featured Adilo video: {adilo_url}")


if __name__ == "__main__":
    main()
