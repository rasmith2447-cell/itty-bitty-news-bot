#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import time
import html
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Optional, Dict, Tuple
from urllib.parse import urlparse, parse_qs

import requests
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dateparser


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

NEWSLETTER_NAME = os.getenv("NEWSLETTER_NAME", "Itty Bitty Gaming News").strip()
NEWSLETTER_TAGLINE = os.getenv("NEWSLETTER_TAGLINE", "Snackable daily gaming news — five days a week.").strip()

DIGEST_WINDOW_HOURS = int(os.getenv("DIGEST_WINDOW_HOURS", "24").strip())
DIGEST_TOP_N = int(os.getenv("DIGEST_TOP_N", "5").strip())
DIGEST_MAX_PER_SOURCE = int(os.getenv("DIGEST_MAX_PER_SOURCE", "1").strip())

USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNews/Digest").strip()

# Discord
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

# Guard (posting window)
DIGEST_FORCE_POST = os.getenv("DIGEST_FORCE_POST", "").strip().lower() in ("1", "true", "yes", "y", "on")

GUARD_TZ = os.getenv("DIGEST_GUARD_TZ", "America/Los_Angeles").strip()
GUARD_HOUR = int(os.getenv("DIGEST_GUARD_LOCAL_HOUR", "19").strip())         # 7pm
GUARD_MINUTE = int(os.getenv("DIGEST_GUARD_LOCAL_MINUTE", "0").strip())
GUARD_WINDOW_MINUTES = int(os.getenv("DIGEST_GUARD_WINDOW_MINUTES", "120").strip())

# YouTube
YOUTUBE_RSS_URL = os.getenv("YOUTUBE_RSS_URL", "").strip()
YOUTUBE_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "").strip()

# Adilo (public scrape)
ADILO_PUBLIC_LATEST_PAGE = os.getenv(
    "ADILO_PUBLIC_LATEST_PAGE",
    "https://adilo.bigcommand.com/c/ittybittygamingnews/video"
).strip()

ADILO_PUBLIC_HOME_PAGE = os.getenv(
    "ADILO_PUBLIC_HOME_PAGE",
    "https://adilo.bigcommand.com/c/ittybittygamingnews/home"
).strip()

# Optional: soft hint (NOT a hard lock)
# Example: https://adilo.bigcommand.com/c/ittybittygamingnews/video?id=lXgdGPp6
ADILO_LATEST_HINT_URL = os.getenv("ADILO_LATEST_HINT_URL", "").strip()

# State file (for seen URLs)
STATE_PATH = os.getenv("STATE_PATH", "state.json").strip()


# =========================
# Models
# =========================

@dataclass
class Story:
    title: str
    url: str
    summary: str
    source: str
    published: datetime
    tags: List[str]


# =========================
# Utilities
# =========================

def now_utc() -> datetime:
    return datetime.now(tz=ZoneInfo("UTC"))

def safe_trunc(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)].rstrip() + "…"

def load_state() -> Dict:
    if not os.path.exists(STATE_PATH):
        return {"seen_urls": [], "seen_titles": [], "seen_story_keys": []}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"seen_urls": [], "seen_titles": [], "seen_story_keys": []}
        data.setdefault("seen_urls", [])
        data.setdefault("seen_titles", [])
        data.setdefault("seen_story_keys", [])
        return data
    except Exception:
        return {"seen_urls": [], "seen_titles": [], "seen_story_keys": []}

def save_state(state: Dict) -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[WARN] Could not save state: {e}")

def story_key(title: str, url: str) -> str:
    # Stable-ish key for dedupe
    return (re.sub(r"\s+", " ", (title or "").strip().lower())[:160] + "|" + (url or "").strip())[:512]

def is_opinionish(title: str, summary: str) -> bool:
    t = (title or "").lower()
    s = (summary or "").lower()

    bad_markers = [
        "opinion", "review", "impressions", "hands-on", "preview", "ranked", "ranking",
        "what we think", "we think", "our take", "debate", "poll:", "poll ", "letters",
        "feature:", "features:", "editorial", "best ", "top ", "worst ", "favorite",
        "favourite", "here's why", "explained", "analysis"
    ]
    # Allow "announced", "launches", "release date" etc.
    allow_markers = [
        "announced", "launch", "launches", "released", "release", "date", "trailer",
        "revealed", "reveal", "update", "patch", "delayed", "drops", "coming to", "preorder"
    ]

    if any(m in t for m in allow_markers):
        return False

    if any(m in t for m in bad_markers):
        return True

    # Summary heuristic
    if "opinion" in s or "editorial" in s or "we think" in s:
        return True

    return False

def extract_tags(title: str) -> List[str]:
    # Simple, safe tags (Discord-friendly)
    title = (title or "").strip()
    tags = []
    if re.search(r"\b(ps5|playstation)\b", title, re.I):
        tags.append("#PlayStation")
    if re.search(r"\b(xbox)\b", title, re.I):
        tags.append("#Xbox")
    if re.search(r"\b(nintendo|switch)\b", title, re.I):
        tags.append("#Nintendo")
    if re.search(r"\b(pc|steam)\b", title, re.I):
        tags.append("#PC")
    if re.search(r"\b(diablo|blizzard|wow|overwatch)\b", title, re.I):
        tags.append("#Blizzard")
    if re.search(r"\b(bungie|marathon|destiny)\b", title, re.I):
        tags.append("#Bungie")

    # Generic for announcements/drops
    if re.search(r"\b(announc|reveal|trailer|launch|release|drops|update|patch)\w*\b", title, re.I):
        tags.append("#GamingNews")

    # Dedup, keep order
    out = []
    for t in tags:
        if t not in out:
            out.append(t)
    return out[:4]


# =========================
# Posting guard
# =========================

def guard_should_post_now() -> bool:
    if DIGEST_FORCE_POST:
        print("[GUARD] DIGEST_FORCE_POST enabled — bypassing time guard.")
        return True

    tz = ZoneInfo(GUARD_TZ)
    now_local = datetime.now(tz)

    target_today = now_local.replace(hour=GUARD_HOUR, minute=GUARD_MINUTE, second=0, microsecond=0)

    candidates = [
        target_today - timedelta(days=1),
        target_today,
        target_today + timedelta(days=1),
    ]
    closest_target = min(candidates, key=lambda t: abs((now_local - t).total_seconds()))
    delta_min = abs((now_local - closest_target).total_seconds()) / 60.0

    if delta_min <= GUARD_WINDOW_MINUTES:
        print(
            f"[GUARD] OK. Local now: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            f"Closest target: {closest_target.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            f"Delta={delta_min:.1f}min <= {GUARD_WINDOW_MINUTES}min"
        )
        return True

    print(
        f"[GUARD] Not within posting window. Local now: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
        f"Closest target: {closest_target.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
        f"Delta={delta_min:.1f}min > {GUARD_WINDOW_MINUTES}min. Exiting without posting."
    )
    return False


# =========================
# RSS fetch
# =========================

def get_feed_urls() -> List[str]:
    raw = os.getenv("FEED_URLS", "").strip()
    if raw:
        # supports newline, comma, or space separated
        parts = re.split(r"[\n,]+", raw)
        out = [p.strip() for p in parts if p.strip()]
        return out or DEFAULT_FEEDS
    return DEFAULT_FEEDS

def parse_entry_datetime(entry) -> Optional[datetime]:
    # Try the common feedparser fields
    for key in ("published", "updated", "created"):
        if getattr(entry, key, None):
            try:
                dt = dateparser.parse(getattr(entry, key))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=ZoneInfo("UTC"))
                return dt.astimezone(ZoneInfo("UTC"))
            except Exception:
                pass
    # structured time tuples
    for key in ("published_parsed", "updated_parsed"):
        t = getattr(entry, key, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=ZoneInfo("UTC"))
            except Exception:
                pass
    return None

def fetch_stories() -> List[Story]:
    urls = get_feed_urls()
    window_start = now_utc() - timedelta(hours=DIGEST_WINDOW_HOURS)

    stories: List[Story] = []
    per_source_count: Dict[str, int] = {}

    for feed_url in urls:
        print(f"[RSS] GET {feed_url}")
        try:
            fp = feedparser.parse(feed_url)
        except Exception as e:
            print(f"[RSS] Feed failed: {feed_url} ({e})")
            continue

        # feedparser sometimes flags encoding issues
        if getattr(fp, "bozo", 0) == 1:
            bozo_ex = getattr(fp, "bozo_exception", None)
            if bozo_ex:
                print(f"[RSS] bozo=1 for {feed_url}: {bozo_ex}")

        feed_title = ""
        try:
            feed_title = (fp.feed.get("title") or "").strip()
        except Exception:
            feed_title = ""

        for entry in getattr(fp, "entries", []) or []:
            title = (getattr(entry, "title", "") or "").strip()
            link = (getattr(entry, "link", "") or "").strip()
            summary = (getattr(entry, "summary", "") or getattr(entry, "description", "") or "").strip()
            summary = BeautifulSoup(summary, "html.parser").get_text(" ", strip=True)

            if not title or not link:
                continue

            dt = parse_entry_datetime(entry) or now_utc()
            if dt < window_start:
                continue

            source_host = urlparse(link).netloc.replace("www.", "").strip().lower()
            source = source_host or (feed_title.lower() if feed_title else "source")

            # Opinion filter
            if is_opinionish(title, summary):
                continue

            # Per-source cap
            per_source_count.setdefault(source, 0)
            if per_source_count[source] >= DIGEST_MAX_PER_SOURCE:
                continue

            tags = extract_tags(title)

            stories.append(
                Story(
                    title=title,
                    url=link,
                    summary=summary,
                    source=source,
                    published=dt,
                    tags=tags,
                )
            )
            per_source_count[source] += 1

    # Sort: newest first
    stories.sort(key=lambda s: s.published, reverse=True)

    # Take top N
    return stories[:DIGEST_TOP_N]


# =========================
# YouTube (RSS)
# =========================

def youtube_rss_url() -> Optional[str]:
    if YOUTUBE_RSS_URL:
        return YOUTUBE_RSS_URL

    if YOUTUBE_CHANNEL_ID:
        return f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}"

    return None

def fetch_latest_youtube_video(session: requests.Session) -> Optional[Tuple[str, str]]:
    """
    Returns (url, title) or None.
    """
    rss = youtube_rss_url()
    if not rss:
        return None

    print(f"[YT] Fetch RSS: {rss}")
    try:
        r = session.get(rss, timeout=20)
        r.raise_for_status()
    except Exception as e:
        print(f"[YT] Failed to fetch RSS: {e}")
        return None

    try:
        soup = BeautifulSoup(r.text, "xml")
        entry = soup.find("entry")
        if not entry:
            return None
        title = (entry.find("title").get_text(strip=True) if entry.find("title") else "").strip()
        link = entry.find("link")
        href = (link.get("href") if link else "").strip()
        if href and title:
            return (href, title)
        if href:
            return (href, "Latest YouTube video")
        return None
    except Exception as e:
        print(f"[YT] Parse RSS failed: {e}")
        return None


# =========================
# Adilo (public scrape, newest-by-date)
# =========================

ADILO_ID_RE = re.compile(r"(?:video\?id=|/watch/)([A-Za-z0-9_-]{6,})")

def _adilo_candidate_ids_from_html(text: str) -> List[str]:
    """
    Pull candidate IDs from any occurrence of:
      - video?id=ID
      - /watch/ID
    """
    if not text:
        return []
    ids = ADILO_ID_RE.findall(text)
    out = []
    for _id in ids:
        if _id not in out:
            out.append(_id)
    return out

def _adilo_parse_published_dt(html_text: str) -> Optional[datetime]:
    """
    Try to extract publish/upload datetime from Adilo public video page HTML.
    We look for:
      - JSON-LD datePublished
      - meta property article:published_time
      - any recognizable date string near "Published" labels
    """
    if not html_text:
        return None

    # JSON-LD
    try:
        soup = BeautifulSoup(html_text, "html.parser")
        for script in soup.find_all("script", {"type": "application/ld+json"}):
            raw = script.get_text(strip=True)
            if not raw:
                continue
            # could be dict or list
            try:
                data = json.loads(raw)
            except Exception:
                continue

            candidates = []
            if isinstance(data, dict):
                candidates.append(data)
            elif isinstance(data, list):
                candidates.extend([x for x in data if isinstance(x, dict)])

            for obj in candidates:
                dp = obj.get("datePublished") or obj.get("uploadDate") or obj.get("dateCreated")
                if dp:
                    try:
                        dt = dateparser.parse(dp)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
                        return dt.astimezone(ZoneInfo("UTC"))
                    except Exception:
                        pass
    except Exception:
        pass

    # meta article:published_time
    m = re.search(r'property=["\']article:published_time["\']\s+content=["\']([^"\']+)["\']', html_text, re.I)
    if m:
        try:
            dt = dateparser.parse(m.group(1))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("UTC"))
            return dt.astimezone(ZoneInfo("UTC"))
        except Exception:
            pass

    # last resort: scan for an ISO-ish timestamp
    m2 = re.search(r"\b(20\d{2}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)\b", html_text)
    if m2:
        try:
            dt = dateparser.parse(m2.group(1))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ZoneInfo("UTC"))
            return dt.astimezone(ZoneInfo("UTC"))
        except Exception:
            pass

    return None

def _adilo_public_video_url_from_id(_id: str) -> str:
    # Using /c/.../video?id=ID is consistently valid + fast
    return f"{ADILO_PUBLIC_LATEST_PAGE}?id={_id}"

def _adilo_watch_url_from_id(_id: str) -> str:
    return f"https://adilo.bigcommand.com/watch/{_id}"

def fetch_latest_adilo_video(session: requests.Session) -> Optional[str]:
    """
    Returns best public URL for newest Adilo upload.
    Strategy:
      1) If ADILO_LATEST_HINT_URL is provided, treat it as a candidate (NOT forced).
      2) Scrape multiple public pages (latest page + cachebusters + home) for candidate IDs.
      3) For up to first 12 candidates, fetch their public /video?id=ID page and parse published date.
      4) Pick the newest date.
      5) Return a WATCH url for clean embedding.
    """
    headers = {"User-Agent": USER_AGENT}

    candidate_ids: List[str] = []
    candidate_hint_id: Optional[str] = None

    # Hint URL (soft)
    if ADILO_LATEST_HINT_URL:
        m = ADILO_ID_RE.search(ADILO_LATEST_HINT_URL)
        if m:
            candidate_hint_id = m.group(1)
            candidate_ids.append(candidate_hint_id)

    # Pages to scrape (try “latest” variants + cache busters)
    cb = str(int(time.time() * 1000))
    pages = [
        ADILO_PUBLIC_LATEST_PAGE,
        f"{ADILO_PUBLIC_LATEST_PAGE}?cb={cb}",
        f"{ADILO_PUBLIC_LATEST_PAGE}/?cb={cb}",
        f"{ADILO_PUBLIC_LATEST_PAGE}?video=latest&cb={cb}",
        # sometimes the site builds links only after loading; still worth attempting:
        f"{ADILO_PUBLIC_LATEST_PAGE}?id=&cb={cb}",
        ADILO_PUBLIC_HOME_PAGE,
        f"{ADILO_PUBLIC_HOME_PAGE}?cb={cb}",
    ]

    scraped_any = False
    for url in pages:
        try:
            print(f"[ADILO] SCRAPE attempt=1 timeout=25 url={url}")
            r = session.get(url, headers=headers, timeout=25)
            text = r.text or ""
            scraped_any = True
            ids = _adilo_candidate_ids_from_html(text)
            for _id in ids:
                if _id not in candidate_ids:
                    candidate_ids.append(_id)
        except requests.exceptions.Timeout:
            print(f"[ADILO] Timeout url={url} (timeout=25)")
        except Exception as e:
            print(f"[ADILO] SCRAPE failed url={url} ({e})")

    # If we got nothing, bail to home (caller will handle fallback)
    if not candidate_ids:
        if scraped_any:
            print("[ADILO] No IDs found on public pages; falling back to home.")
        return None

    # Validate + date-rank candidates
    # Keep it bounded so we don't hammer the site.
    to_check = candidate_ids[:12]

    best_dt: Optional[datetime] = None
    best_id: Optional[str] = None

    for _id in to_check:
        try:
            # Fetch the "video?id=" page for reliable metadata extraction
            video_url = _adilo_public_video_url_from_id(_id)
            r = session.get(video_url, headers=headers, timeout=20)
            if r.status_code != 200:
                continue

            dt = _adilo_parse_published_dt(r.text)
            # If we can't parse a date, we still accept as "very low confidence"
            if dt is None:
                # if nothing else wins, keep the first parseable candidate
                if best_id is None:
                    best_id = _id
                continue

            if (best_dt is None) or (dt > best_dt):
                best_dt = dt
                best_id = _id

        except requests.exceptions.Timeout:
            continue
        except Exception:
            continue

    # If hint exists and has a date, it should naturally win (since it’s new)
    # If hint exists but no date parsing, it might still win only if nothing else parses.
    if best_id:
        watch = _adilo_watch_url_from_id(best_id)
        print(f"[ADILO] Picked newest id={best_id} dt={best_dt.isoformat() if best_dt else 'unknown'} -> {watch}")
        return watch

    return None


# =========================
# Discord posting
# =========================

def discord_post(content: str, embeds: Optional[List[Dict]] = None) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL")

    payload = {"content": content}
    if embeds:
        # Discord: max 10 embeds
        payload["embeds"] = embeds[:10]

    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
    r.raise_for_status()

def build_digest_message(stories: List[Story], yt: Optional[Tuple[str, str]], adilo_url: Optional[str]) -> Tuple[str, List[Dict]]:
    """
    Returns (content, embeds) where embeds includes per-story cards + Adilo card.
    YouTube is posted as a raw link in content so Discord can auto-embed/play.
    """
    local_tz = ZoneInfo(GUARD_TZ)
    today_str = datetime.now(local_tz).strftime("%B %d, %Y")

    # Header lines
    lines = []
    if NEWSLETTER_TAGLINE:
        lines.append(NEWSLETTER_TAGLINE)
        lines.append("")

    lines.append(today_str)
    lines.append("")
    lines.append(f"In Tonight’s Edition of {NEWSLETTER_NAME}…")

    # "teaser bullets" top 3
    for s in stories[:3]:
        lines.append(f"► 🎮 {safe_trunc(s.title, 85)}")
    lines.append("")
    lines.append("Tonight’s Top Stories")
    lines.append("")

    embeds: List[Dict] = []

    # Each story: numbered line + then embed card under it
    for idx, s in enumerate(stories, start=1):
        tag_str = (" " + " ".join(s.tags)) if s.tags else ""
        lines.append(f"{idx}) {s.title}{tag_str}")

        # Story embed (card)
        desc = safe_trunc(s.summary, 280)
        if not desc:
            desc = "Read the full story at the source."

        embed = {
            "title": safe_trunc(s.title, 256),
            "url": s.url,
            "description": desc,
            "footer": {"text": f"Source: {s.source}"},
        }
        embeds.append(embed)

        lines.append("")  # spacing

    # Featured Video block
    # IMPORTANT: Keep YouTube link as plain URL line for Discord auto-embed/play.
    lines.append("📺 Featured Video")
    lines.append("")

    if yt:
        yt_url, yt_title = yt
        lines.append(f"▶️ YouTube (latest)")
        lines.append(yt_url)  # plain
        lines.append("")

    if adilo_url:
        lines.append("📺 Adilo (latest)")
        lines.append(adilo_url)  # plain
        lines.append("")
        # Add Adilo as an embed too (thumbnail-ish card)
        embeds.append({
            "title": "Watch today’s Itty Bitty Gaming News (Adilo)",
            "url": adilo_url,
        })

    # Sign-off (no “signal” wording)
    lines.append("—")
    lines.append("That’s it for tonight’s Itty Bitty. 😄")
    lines.append("Catch the snackable breakdown on Itty Bitty Gaming News tomorrow.")

    # Discord content limit is 2000 chars; we keep it under by relying on embeds for summaries.
    content = "\n".join(lines)
    if len(content) > 1900:
        content = content[:1890].rstrip() + "…"

    return content, embeds


# =========================
# Main
# =========================

def main():
    # Guard
    if not guard_should_post_now():
        # clean exit (no failure)
        return

    # Session
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    # Load state (dedupe)
    state = load_state()
    print(f"[DIGEST] Loaded {len(state.get('seen_story_keys', []))} seen_story_keys from {STATE_PATH}")

    # Fetch stories
    stories = fetch_stories()

    # Dedupe vs state
    filtered: List[Story] = []
    for s in stories:
        key = story_key(s.title, s.url)
        if key in state["seen_story_keys"]:
            continue
        filtered.append(s)

    if not filtered:
        print("[DIGEST] No new items found in window. Exiting without posting.")
        return

    # YouTube
    yt = fetch_latest_youtube_video(session)

    # Adilo (newest-by-date)
    adilo_url = fetch_latest_adilo_video(session)
    if not adilo_url:
        print(f"[ADILO] Falling back: {ADILO_PUBLIC_HOME_PAGE}")
        adilo_url = ADILO_PUBLIC_HOME_PAGE

    # Build + post
    content, embeds = build_digest_message(filtered[:DIGEST_TOP_N], yt, adilo_url)
    discord_post(content, embeds)

    # Mark posted items as seen
    for s in filtered[:DIGEST_TOP_N]:
        state["seen_story_keys"].append(story_key(s.title, s.url))
        state["seen_urls"].append(s.url)
        state["seen_titles"].append(s.title)

    # keep state bounded
    state["seen_story_keys"] = state["seen_story_keys"][-3000:]
    state["seen_urls"] = state["seen_urls"][-3000:]
    state["seen_titles"] = state["seen_titles"][-3000:]

    save_state(state)

    print("[DONE] Digest posted.")
    if yt:
        print(f"[DONE] YouTube: {yt[0]}")
    print(f"[DONE] Featured Adilo video: {adilo_url}")


if __name__ == "__main__":
    main()
