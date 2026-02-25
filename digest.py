#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import time
import json
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
YOUTUBE_FEATURED_URL = os.getenv("YOUTUBE_FEATURED_URL", "").strip()

# Adilo public pages
ADILO_PUBLIC_LATEST_PAGE = os.getenv(
    "ADILO_PUBLIC_LATEST_PAGE", "https://adilo.bigcommand.com/c/ittybittygamingnews/video"
).strip()
ADILO_PUBLIC_HOME_PAGE = os.getenv(
    "ADILO_PUBLIC_HOME_PAGE", "https://adilo.bigcommand.com/c/ittybittygamingnews/home"
).strip()

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})


# =========================
# UTILITIES
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
                # don't fail hard on bozo; many feeds are still usable
                pass

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
# YOUTUBE
# =========================

def get_latest_youtube_url() -> str:
    # If you hardcode/override, respect it.
    if YOUTUBE_FEATURED_URL:
        return YOUTUBE_FEATURED_URL

    if not YOUTUBE_CHANNEL_ID:
        return ""

    rss_candidates = [
        f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}",
    ]

    # Uploads playlist fallback (UU + channel id without UC)
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
# ADILO (RELIABLE MOST RECENT)
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


def _dedup_keep_order(xs: list[str]) -> list[str]:
    seen = set()
    out = []
    for x in xs:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _extract_adilo_ids_from_html(html: str) -> tuple[list[str], list[str]]:
    """
    Returns:
      - ids_any: ids found anywhere (watch/, stage/, video?id=)
      - ids_from_video_id_links: ids found specifically from video?id=... links (these are high-signal)
    """
    text = str(html)
    soup = BeautifulSoup(text, "html.parser")

    ids_any = []
    ids_video_id = []

    # 1) Strongest signal: explicit video?id=XXXX in href/src/text
    for m in re.finditer(r"video\?id=([A-Za-z0-9_-]{6,})", text):
        ids_any.append(m.group(1))
        ids_video_id.append(m.group(1))

    # 2) Watch links
    for m in re.finditer(r"/watch/([A-Za-z0-9_-]{6,})", text):
        ids_any.append(m.group(1))

    # 3) Stage links
    for m in re.finditer(r"/stage/videos/([A-Za-z0-9_-]{6,})", text):
        ids_any.append(m.group(1))

    # Also check actual attributes where React apps sometimes stash URLs
    for tag in soup.find_all(["a", "iframe"]):
        val = (tag.get("href") or tag.get("src") or "").strip()
        if not val:
            continue
        for m in re.finditer(r"video\?id=([A-Za-z0-9_-]{6,})", val):
            ids_any.append(m.group(1))
            ids_video_id.append(m.group(1))
        for m in re.finditer(r"/watch/([A-Za-z0-9_-]{6,})", val):
            ids_any.append(m.group(1))
        for m in re.finditer(r"/stage/videos/([A-Za-z0-9_-]{6,})", val):
            ids_any.append(m.group(1))

    return _dedup_keep_order(ids_any), _dedup_keep_order(ids_video_id)


def _parse_best_datetime_from_watch_html(html: str) -> datetime | None:
    """
    Try to detect a date from a watch page.
    Works across a bunch of common patterns; returns newest detected.
    """
    text = str(html)
    candidates: list[datetime] = []

    # ISO timestamps like 2026-02-25T03:18:22Z
    for m in re.finditer(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)", text):
        try:
            candidates.append(datetime.fromisoformat(m.group(1).replace("Z", "+00:00")))
        except Exception:
            pass

    # upload_date / createdAt etc: 2026-02-25 03:18:22 or 2026-02-25
    for m in re.finditer(
        r"(?:upload_date|uploadDate|created_at|createdAt|published_at|publishedAt)[^0-9]{0,25}"
        r"(\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}:\d{2})?)",
        text,
        flags=re.I,
    ):
        s = m.group(1)
        try:
            if " " in s or "T" in s:
                s2 = s.replace(" ", "T")
                dt = datetime.fromisoformat(s2)
                candidates.append(dt.replace(tzinfo=timezone.utc))
            else:
                candidates.append(datetime.fromisoformat(s).replace(tzinfo=timezone.utc))
        except Exception:
            pass

    # Meta tags sometimes help
    soup = BeautifulSoup(text, "html.parser")
    for prop in ["article:published_time", "og:updated_time", "og:published_time"]:
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            c = tag.get("content", "").strip()
            try:
                candidates.append(datetime.fromisoformat(c.replace("Z", "+00:00")))
            except Exception:
                pass

    if not candidates:
        return None

    norm = []
    for dt in candidates:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        norm.append(dt.astimezone(timezone.utc))

    return max(norm)


def _adilo_watch_url(vid: str) -> str:
    return f"https://adilo.bigcommand.com/watch/{vid}"


def _adilo_public_video_url(vid: str) -> str:
    return f"{ADILO_PUBLIC_LATEST_PAGE}?id={vid}"


def scrape_latest_adilo_url() -> str:
    """
    Reliable strategy:
      1) Fetch the public latest page with cache-busters.
      2) Extract all candidate IDs.
      3) Prefer candidates found in video?id=... links.
      4) Verify candidates by requesting watch pages and picking the newest detected datetime.
      5) If datetime detection fails, fall back to the last *video?id* candidate, else last any-candidate.
    """
    base_latest = ADILO_PUBLIC_LATEST_PAGE.rstrip("/")
    base_home = ADILO_PUBLIC_HOME_PAGE.rstrip("/")
    cb = f"cb={int(time.time())}{random.randint(100,999)}"

    pages = [
        base_latest,
        f"{base_latest}?{cb}",
        f"{base_latest}/?{cb}",
        f"{base_latest}?video=latest&{cb}",
        base_home,
        f"{base_home}?{cb}",
    ]

    attempts = [
        {"timeout": 25, "sleep": 0.0},
        {"timeout": 18, "sleep": 1.0},
        {"timeout": 12, "sleep": 1.6},
    ]

    ids_any: list[str] = []
    ids_video_id: list[str] = []

    for i, att in enumerate(attempts, start=1):
        timeout = att["timeout"]
        if att["sleep"]:
            time.sleep(att["sleep"])

        for url in pages:
            try:
                print(f"[ADILO] SCRAPE attempt={i} timeout={timeout} url={url}")
                html = _adilo_http_get(url, timeout=timeout)
                any_ids, video_ids = _extract_adilo_ids_from_html(html)
                if any_ids:
                    ids_any.extend(any_ids)
                if video_ids:
                    ids_video_id.extend(video_ids)
            except requests.exceptions.ReadTimeout:
                print(f"[ADILO] Timeout url={url} (timeout={timeout})")
            except Exception as ex:
                print(f"[ADILO] Error url={url}: {ex}")

        ids_any = _dedup_keep_order(ids_any)
        ids_video_id = _dedup_keep_order(ids_video_id)

        if ids_any or ids_video_id:
            break

    if not ids_any and not ids_video_id:
        print(f"[ADILO] No IDs found. Falling back: {ADILO_PUBLIC_HOME_PAGE}")
        return ADILO_PUBLIC_HOME_PAGE

    # Probe: verify newest by watch-page datetime.
    # Priority: video?id candidates first, then any candidates.
    probe_pool = _dedup_keep_order(list(reversed(ids_video_id)) + list(reversed(ids_any)))
    probe_pool = probe_pool[:12]  # don’t hammer Adilo

    best: tuple[datetime, str] | None = None

    for vid in probe_pool:
        watch_url = _adilo_watch_url(vid)
        try:
            html = _adilo_http_get(watch_url, timeout=12)
            dt = _parse_best_datetime_from_watch_html(html)
            if dt:
                if (best is None) or (dt > best[0]):
                    best = (dt, watch_url)
            time.sleep(0.25)
        except Exception:
            continue

    if best:
        print(f"[ADILO] Picked newest by watch-page datetime: {best[1]} dt={best[0].isoformat()}")
        return best[1]

    # No dates detected: fall back to strongest heuristic:
    # prefer the last-seen video?id candidate (usually “current/latest selection”)
    if ids_video_id:
        fallback = _adilo_public_video_url(ids_video_id[-1])
        print(f"[ADILO] No datetime found. Falling back to video?id heuristic: {fallback}")
        return fallback

    fallback = _adilo_watch_url(ids_any[-1]) if ids_any else ADILO_PUBLIC_HOME_PAGE
    print(f"[ADILO] No datetime found. Falling back to newest-id heuristic: {fallback}")
    return fallback


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
    time.sleep(1.1)

    for i, it in enumerate(stories, start=1):
        text = clamp(format_story_text(i, it), 1800)
        embed = build_story_embed(it)
        discord_post(text, embeds=[embed])
        time.sleep(1.1)

    # YouTube: post URL alone for Discord inline player
    yt_url = get_latest_youtube_url()
    if yt_url:
        discord_post("▶️ YouTube (latest)")
        time.sleep(0.7)
        discord_post(yt_url)
        time.sleep(1.0)

    # Adilo: scrape + verify newest
    adilo_url = scrape_latest_adilo_url()
    adilo_embed = build_adilo_embed(adilo_url)
    discord_post("📺 Adilo (latest)\n" + adilo_url, embeds=[adilo_embed])

    print("[DONE] Digest posted.")
    if yt_url:
        print(f"[DONE] YouTube: {yt_url}")
    print(f"[DONE] Featured Adilo video: {adilo_url}")


if __name__ == "__main__":
    main()
