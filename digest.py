#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
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
YOUTUBE_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "").strip()
YOUTUBE_FEATURED_URL = os.getenv("YOUTUBE_FEATURED_URL", "").strip()   # optional override
YOUTUBE_FEATURED_TITLE = os.getenv("YOUTUBE_FEATURED_TITLE", "").strip()  # optional override

# Adilo public pages
ADILO_PUBLIC_LATEST_PAGE = os.getenv(
    "ADILO_PUBLIC_LATEST_PAGE", "https://adilo.bigcommand.com/c/ittybittygamingnews/video"
).strip()
ADILO_PUBLIC_HOME_PAGE = os.getenv(
    "ADILO_PUBLIC_HOME_PAGE", "https://adilo.bigcommand.com/c/ittybittygamingnews/home"
).strip()

# Debug override (ONLY use if you're forcing)
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
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return text


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower().replace("www.", "")
        return host
    except Exception:
        return ""


def parse_entry_datetime(entry) -> datetime | None:
    if getattr(entry, "published_parsed", None):
        try:
            return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        except Exception:
            pass

    for k in ("published", "updated", "created"):
        v = getattr(entry, k, None)
        if v:
            try:
                dt = parsedate_to_datetime(v)
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
# FILTERS
# =========================

BAD_TITLE_PATTERNS = [
    r"\bbest\b",
    r"\btop\s+\d+\b",
    r"\branking\b",
    r"\branked\b",
    r"\breview\b",
    r"\bpreview\b",
    r"\bdeals?\b",
    r"\bdiscount\b",
    r"\bcheapest\b",
    r"\bguide\b",
    r"\bhow to\b",
    r"\bexplained\b",
    r"\brumou?r\b",
    r"\bspeculation\b",
    r"\breportedly\b",
]

def is_bad_story(title: str, summary: str) -> bool:
    blob = ((title or "") + " " + (summary or "")).lower()
    for pat in BAD_TITLE_PATTERNS:
        if re.search(pat, blob, flags=re.IGNORECASE):
            return True
    return False


# =========================
# TAGGING
# =========================

def make_tags(title: str) -> list[str]:
    t = (title or "").lower()
    tags = []

    if "playstation" in t or "ps5" in t or "sony" in t:
        tags.append("#PlayStation")
    if "xbox" in t or "microsoft" in t:
        tags.append("#Xbox")
    if "nintendo" in t or "switch" in t:
        tags.append("#Nintendo")
    if "steam" in t or "pc" in t:
        tags.append("#PC")
    if "mobile" in t or "ios" in t or "android" in t:
        tags.append("#Mobile")

    if any(k in t for k in ["launch", "releases", "release", "out now"]):
        tags.append("#Release")
    if any(k in t for k in ["update", "patch", "hotfix", "season"]):
        tags.append("#Update")
    if any(k in t for k in ["announced", "reveal", "trailer"]):
        tags.append("#Announcement")
    if any(k in t for k in ["layoff", "cuts", "shutdown", "closure"]):
        tags.append("#Industry")
    if any(k in t for k in ["lawsuit", "court", "legal"]):
        tags.append("#Legal")
    if any(k in t for k in ["hack", "breach"]):
        tags.append("#Security")

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
                print(f"[RSS] bozo=1 for {url}: {getattr(parsed, 'bozo_exception', '')}")

            for e in parsed.entries:
                title = (getattr(e, "title", "") or "").strip()
                link = (getattr(e, "link", "") or "").strip()
                summary = strip_html(getattr(e, "summary", "") or getattr(e, "description", "") or "")

                if not title or not link or link in seen_links:
                    continue

                dt = parse_entry_datetime(e)
                if not dt or dt < window_start:
                    continue

                if is_bad_story(title, summary):
                    continue

                src = domain_of(link) or domain_of(url)
                items.append({"title": title, "link": link, "summary": summary, "dt": dt, "source": src})
                seen_links.add(link)

        except Exception as ex:
            print(f"[RSS] Feed failed: {url} ({ex})")

    items.sort(key=lambda x: x["dt"], reverse=True)
    return items


def select_top_stories(items: list[dict]) -> list[dict]:
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
# DISCORD
# =========================

def discord_post(content: str, embeds: list[dict] | None = None):
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL")

    payload = {"content": content}
    if embeds:
        payload["embeds"] = embeds[:10]

    r = SESSION.post(DISCORD_WEBHOOK_URL, json=payload, timeout=25)
    if r.status_code >= 400:
        print(f"[DISCORD] HTTP {r.status_code}: {r.text[:500]}")
    r.raise_for_status()


def fetch_og(url: str, timeout: int = 10) -> dict:
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*"}
    r = SESSION.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    def meta(prop: str) -> str:
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        return (tag.get("content") if tag else "") or ""

    return {
        "title": (meta("og:title") or "").strip(),
        "image": (meta("og:image") or "").strip(),
        "site_name": (meta("og:site_name") or "").strip(),
        "url": (meta("og:url") or "").strip(),
    }


def build_story_embed(item: dict) -> dict:
    title = clamp(item["title"], 256)
    url = item["link"]
    summary = clamp(item["summary"], 350)
    src = item["source"]

    tags = make_tags(item["title"])
    tag_line = " ".join(tags) if tags else ""

    desc = summary
    if tag_line:
        desc = (desc + "\n\n" + tag_line).strip()

    emb = {
        "title": title,
        "url": url,
        "description": clamp(desc, 4096),
        "footer": {"text": f"Source: {src}"},
    }

    try:
        og = fetch_og(url, timeout=10)
        if og.get("image"):
            emb["image"] = {"url": og["image"]}
        if og.get("site_name"):
            emb["author"] = {"name": og["site_name"]}
    except Exception:
        pass

    return emb


# =========================
# YOUTUBE (PLAYER IN DISCORD)
# =========================

def get_latest_youtube_url() -> str:
    # If you manually override it, use that.
    if YOUTUBE_FEATURED_URL:
        return YOUTUBE_FEATURED_URL

    if not YOUTUBE_CHANNEL_ID:
        return ""

    rss_candidates = [
        f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}",
    ]
    if YOUTUBE_CHANNEL_ID.startswith("UC") and len(YOUTUBE_CHANNEL_ID) > 2:
        rss_candidates.append(f"https://www.youtube.com/feeds/videos.xml?playlist_id=UU{YOUTUBE_CHANNEL_ID[2:]}")

    for rss in rss_candidates:
        try:
            print(f"[YT] Fetch RSS: {rss}")
            r = SESSION.get(rss, timeout=20)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
            if parsed.entries:
                return (getattr(parsed.entries[0], "link", "") or "").strip()
        except Exception as ex:
            print(f"[YT] RSS failed: {rss} ({ex})")

    return ""


# =============================
# ADILO (MORE RELIABLE "LATEST")
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


def _adilo_pick_best_watch_url(html: str) -> str:
    """
    Priority:
      1) og:url pointing to /watch/<id>
      2) canonical /watch/<id>
      3) last /watch/<id> found in DOM/text (often newest on listing pages)
    """
    soup = BeautifulSoup(str(html), "html.parser")

    # 1) og:url
    og_url = ""
    og_tag = soup.find("meta", attrs={"property": "og:url"})
    if og_tag and og_tag.get("content"):
        og_url = og_tag.get("content", "").strip()
        m = re.search(r"adilo\.bigcommand\.com/watch/([A-Za-z0-9_-]{6,})", og_url)
        if m:
            return f"https://adilo.bigcommand.com/watch/{m.group(1)}"

    # 2) canonical
    can = soup.find("link", attrs={"rel": "canonical"})
    if can and can.get("href"):
        href = can.get("href", "").strip()
        m = re.search(r"adilo\.bigcommand\.com/watch/([A-Za-z0-9_-]{6,})", href)
        if m:
            return f"https://adilo.bigcommand.com/watch/{m.group(1)}"

    # 3) collect ALL ids in DOM order
    ids = []

    # first: anchors and iframes and meta contents in DOM order
    for tag in soup.find_all(["a", "iframe", "meta"]):
        val = ""
        if tag.name == "a":
            val = tag.get("href") or ""
        elif tag.name == "iframe":
            val = tag.get("src") or ""
        elif tag.name == "meta":
            val = tag.get("content") or ""

        if not val:
            continue

        # watch
        for m in re.finditer(r"/watch/([A-Za-z0-9_-]{6,})", val):
            ids.append(m.group(1))

        # full watch
        for m in re.finditer(r"adilo\.bigcommand\.com/watch/([A-Za-z0-9_-]{6,})", val):
            ids.append(m.group(1))

        # video id param
        for m in re.finditer(r"video\?id=([A-Za-z0-9_-]{6,})", val):
            ids.append(m.group(1))

        # stage/videos
        for m in re.finditer(r"/stage/videos/([A-Za-z0-9_-]{6,})", val):
            ids.append(m.group(1))

    # also scan raw html as fallback
    raw = str(html)
    for m in re.finditer(r"adilo\.bigcommand\.com/watch/([A-Za-z0-9_-]{6,})", raw):
        ids.append(m.group(1))
    for m in re.finditer(r"/watch/([A-Za-z0-9_-]{6,})", raw):
        ids.append(m.group(1))

    # dedupe while preserving order
    seen = set()
    ordered = []
    for vid in ids:
        if vid not in seen:
            seen.add(vid)
            ordered.append(vid)

    if ordered:
        # KEY CHANGE: pick LAST id (often newest), not first
        picked = ordered[-1]
        return f"https://adilo.bigcommand.com/watch/{picked}"

    return ""


def scrape_latest_adilo_watch_url() -> str:
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
        f"{base}?video=latest&{cb}",
        # home sometimes loads more reliably (but contains many IDs)
        ADILO_PUBLIC_HOME_PAGE.rstrip("/"),
        f"{ADILO_PUBLIC_HOME_PAGE.rstrip('/')}?{cb}",
    ]

    attempts = [
        {"timeout": 25, "sleep": 0.0},
        {"timeout": 18, "sleep": 1.2},
        {"timeout": 12, "sleep": 1.8},
    ]

    for i, att in enumerate(attempts, start=1):
        timeout = att["timeout"]
        if att["sleep"]:
            time.sleep(att["sleep"])

        for url in candidates:
            try:
                print(f"[ADILO] SCRAPE attempt={i} timeout={timeout} url={url}")
                html = _adilo_http_get(url, timeout=timeout)
                watch = _adilo_pick_best_watch_url(html)
                if watch:
                    m = re.search(r"/watch/([A-Za-z0-9_-]{6,})", watch)
                    vid = m.group(1) if m else "UNKNOWN"
                    print(f"[ADILO] Found candidate id={vid} -> {watch}")
                    return watch
            except requests.exceptions.ReadTimeout:
                print(f"[ADILO] Timeout url={url} (timeout={timeout})")
            except Exception as ex:
                print(f"[ADILO] Error url={url}: {ex}")

    print(f"[ADILO] Falling back: {ADILO_PUBLIC_HOME_PAGE}")
    return ADILO_PUBLIC_HOME_PAGE


def build_adilo_embed(url: str) -> dict:
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
    return "\n".join([f"► 🎮 {clamp(it['title'], 80)}" for it in stories[:count]])


def format_story_text(idx: int, item: dict) -> str:
    title = item["title"].strip()
    summary = clamp(item["summary"], 340)
    src = item["source"]
    link = item["link"]
    tags = make_tags(title)
    tag_line = (" ".join(tags)).strip()

    lines = [f"{idx}) {title}"]
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

    discord_post("\n".join(intro_lines).strip())
    time.sleep(1.2)

    # One story at a time (keeps "card under story" behavior)
    for i, it in enumerate(stories, start=1):
        text = clamp(format_story_text(i, it), 1800)
        embed = build_story_embed(it)
        discord_post(text, embeds=[embed])
        time.sleep(1.2)

    # Featured videos: YouTube should be playable => DO NOT attach custom embed.
    yt_url = get_latest_youtube_url()
    if yt_url:
        # Put URL on its own line so Discord unfurls with player
        discord_post("▶️ YouTube (latest)")
        time.sleep(0.8)
        discord_post(yt_url)  # <-- this is what makes the inline player appear
        time.sleep(1.2)

    # Adilo: we post an embed with thumbnail + link
    adilo_url = scrape_latest_adilo_watch_url()
    adilo_embed = build_adilo_embed(adilo_url)
    discord_post("📺 Adilo (latest)\n" + adilo_url, embeds=[adilo_embed])

    print("[DONE] Digest posted.")
    if yt_url:
        print(f"[DONE] YouTube: {yt_url}")
    print(f"[DONE] Featured Adilo video: {adilo_url}")


if __name__ == "__main__":
    main()
