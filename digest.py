#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse, parse_qs

import requests
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from rapidfuzz import fuzz


# =========================
# Config / Defaults
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

USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNews/Digest").strip()
STATE_FILE = "state.json"

DIGEST_CACHE_FILE = os.getenv("DIGEST_CACHE_FILE", ".digest_cache.json").strip()
DIGEST_POST_ONCE_PER_DAY = os.getenv("DIGEST_POST_ONCE_PER_DAY", "false").strip().lower() in ("1", "true", "yes")

DIGEST_WINDOW_HOURS = int(os.getenv("DIGEST_WINDOW_HOURS", "24").strip())
DIGEST_TOP_N = int(os.getenv("DIGEST_TOP_N", "5").strip())
DIGEST_MAX_PER_SOURCE = int(os.getenv("DIGEST_MAX_PER_SOURCE", "1").strip())

NEWSLETTER_NAME = os.getenv("NEWSLETTER_NAME", "Itty Bitty Gaming News").strip()
NEWSLETTER_TAGLINE = os.getenv("NEWSLETTER_TAGLINE", "Snackable daily gaming news — five days a week.").strip()

YOUTUBE_RSS_URL = os.getenv("YOUTUBE_RSS_URL", "").strip()
YOUTUBE_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "").strip()

ADILO_PUBLIC_LATEST_PAGE = os.getenv("ADILO_PUBLIC_LATEST_PAGE", "").strip() or "https://adilo.bigcommand.com/c/ittybittygamingnews/video"
ADILO_PUBLIC_HOME_PAGE = os.getenv("ADILO_PUBLIC_HOME_PAGE", "").strip() or "https://adilo.bigcommand.com/c/ittybittygamingnews/home"
ADILO_PUBLIC_KEY = os.getenv("ADILO_PUBLIC_KEY", "").strip()
ADILO_SECRET_KEY = os.getenv("ADILO_SECRET_KEY", "").strip()
ADILO_PROJECT_ID = os.getenv("ADILO_PROJECT_ID", "").strip()

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

DIGEST_FORCE_POST = os.getenv("DIGEST_FORCE_POST", "").strip().lower() in ("1", "true", "yes")

DIGEST_GUARD_TZ = os.getenv("DIGEST_GUARD_TZ", "America/Los_Angeles").strip()
DIGEST_GUARD_LOCAL_HOUR = int(os.getenv("DIGEST_GUARD_LOCAL_HOUR", "19").strip())
DIGEST_GUARD_LOCAL_MINUTE = int(os.getenv("DIGEST_GUARD_LOCAL_MINUTE", "0").strip())
DIGEST_GUARD_WINDOW_MINUTES = int(os.getenv("DIGEST_GUARD_WINDOW_MINUTES", "30").strip())

# YouTube Shorts filtering
FILTER_YT_SHORTS = os.getenv("FILTER_YT_SHORTS", "true").strip().lower() in ("1", "true", "yes")


# =========================
# Data
# =========================

@dataclass
class Story:
    title: str
    url: str
    source: str
    published_at: Optional[datetime]
    summary: str
    tags: List[str]


# =========================
# Utilities
# =========================

def http_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    return s


def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {"seen_urls": [], "seen_titles": [], "seen_story_keys": []}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("seen_urls", [])
            data.setdefault("seen_titles", [])
            data.setdefault("seen_story_keys", [])
            return data
    except Exception:
        pass
    return {"seen_urls": [], "seen_titles": [], "seen_story_keys": []}


def save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def load_cache() -> Dict:
    if not os.path.exists(DIGEST_CACHE_FILE):
        return {}
    try:
        with open(DIGEST_CACHE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_cache(cache: Dict) -> None:
    with open(DIGEST_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def normalize_url(u: str) -> str:
    u = (u or "").strip()
    return u


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
            f"[GUARD] OK. Local now: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            f"Closest target: {closest.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            f"Delta={delta_min:.1f}min <= {DIGEST_GUARD_WINDOW_MINUTES}min"
        )
        return True

    print(
        f"[GUARD] Not within posting window. Local now: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
        f"Closest target: {closest.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
        f"Delta={delta_min:.1f}min > {DIGEST_GUARD_WINDOW_MINUTES}min. Exiting without posting."
    )
    return False


def post_once_per_day_guard(cache: Dict) -> bool:
    if not DIGEST_POST_ONCE_PER_DAY:
        return True
    today = now_local().date().isoformat()
    posted = cache.get("posted_dates", [])
    if today in posted and not DIGEST_FORCE_POST:
        print(f"[CACHE] Already posted for {today}. Skipping.")
        return False
    return True


def mark_posted_today(cache: Dict) -> None:
    today = now_local().date().isoformat()
    posted = cache.get("posted_dates", [])
    if today not in posted:
        posted.append(today)
    cache["posted_dates"] = posted
    save_cache(cache)
    print(f"[CACHE] Marked posted for {today}.")


def safe_parse_datetime(dt_val) -> Optional[datetime]:
    if not dt_val:
        return None
    try:
        if isinstance(dt_val, (int, float)):
            return datetime.fromtimestamp(dt_val)
        return dateparser.parse(str(dt_val))
    except Exception:
        return None


def clamp(s: str, max_len: int) -> str:
    s = (s or "").strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 1].rstrip() + "…"


def derive_tags(title: str, source: str) -> List[str]:
    t = (title or "").lower()
    tags = []
    if "nintendo" in t: tags.append("Nintendo")
    if "playstation" in t or "ps5" in t: tags.append("PlayStation")
    if "xbox" in t: tags.append("Xbox")
    if "steam" in t: tags.append("PC/Steam")
    if "switch" in t: tags.append("Switch")
    if "update" in t or "patch" in t: tags.append("Update")
    if "release" in t or "launch" in t: tags.append("Release")
    if "trailer" in t: tags.append("Trailer")
    if "rumor" in t or "leak" in t: tags.append("Rumor")
    if "delay" in t: tags.append("Delay")
    if not tags:
        tags.append(source.replace("www.", "").split(".")[0].title())
    return tags[:3]


# =========================
# RSS Fetch + Ranking
# =========================

def get_feed_urls() -> List[str]:
    env = os.getenv("FEED_URLS", "").strip()
    if env:
        # allow comma or newline separated
        parts = [p.strip() for p in re.split(r"[,\n]+", env) if p.strip()]
        if parts:
            return parts
    return DEFAULT_FEEDS


def fetch_rss_items() -> List[Story]:
    urls = get_feed_urls()
    stories: List[Story] = []
    session = http_session()

    for u in urls:
        print(f"[RSS] GET {u}")
        try:
            # feedparser can fetch itself, but we want our UA + timeouts
            r = session.get(u, timeout=25)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)

            if getattr(parsed, "bozo", 0):
                # not fatal; many feeds set wrong encoding
                bozo_exc = getattr(parsed, "bozo_exception", None)
                if bozo_exc:
                    print(f"[RSS] bozo=1 for {u}: {bozo_exc}")

            for e in parsed.entries[:200]:
                link = normalize_url(getattr(e, "link", "") or "")
                title = (getattr(e, "title", "") or "").strip()
                if not link or not title:
                    continue

                summary = (getattr(e, "summary", "") or getattr(e, "description", "") or "").strip()
                published = safe_parse_datetime(getattr(e, "published", None) or getattr(e, "updated", None))
                source_host = urlparse(link).netloc.lower() or urlparse(u).netloc.lower()
                source_host = source_host.replace("www.", "")

                stories.append(
                    Story(
                        title=title,
                        url=link,
                        source=source_host,
                        published_at=published,
                        summary=BeautifulSoup(summary, "html.parser").get_text(" ", strip=True),
                        tags=derive_tags(title, source_host),
                    )
                )
        except Exception as ex:
            print(f"[RSS] Feed failed: {u} ({ex})")

    return stories


def dedupe_and_filter(stories: List[Story], state: Dict) -> List[Story]:
    seen_urls = set(state.get("seen_urls", []))
    seen_titles = list(state.get("seen_titles", []))

    cutoff = datetime.utcnow() - timedelta(hours=DIGEST_WINDOW_HOURS)
    out: List[Story] = []

    def is_dupe_title(t: str) -> bool:
        for old in seen_titles[-400:]:
            if fuzz.ratio((t or "").lower(), (old or "").lower()) >= 92:
                return True
        return False

    per_source: Dict[str, int] = {}
    for s in stories:
        if s.url in seen_urls:
            continue
        if is_dupe_title(s.title):
            continue
        if s.published_at and s.published_at.tzinfo:
            pub_utc = s.published_at.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        else:
            pub_utc = s.published_at

        if pub_utc and pub_utc < cutoff:
            continue

        cnt = per_source.get(s.source, 0)
        if cnt >= DIGEST_MAX_PER_SOURCE:
            continue

        per_source[s.source] = cnt + 1
        out.append(s)

    # rank: newest first (fallback: keep)
    out.sort(key=lambda x: x.published_at or datetime(1970, 1, 1), reverse=True)

    return out


# =========================
# YouTube Latest
# =========================

def get_latest_youtube_url() -> str:
    rss = YOUTUBE_RSS_URL.strip()
    if not rss and YOUTUBE_CHANNEL_ID:
        rss = f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}"

    if not rss:
        return ""

    print(f"[YT] Fetch RSS: {rss}")
    try:
        s = http_session()
        r = s.get(rss, timeout=25)
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
        if not parsed.entries:
            return ""
        entry = parsed.entries[0]
        link = (getattr(entry, "link", "") or "").strip()
        if not link:
            return ""

        if FILTER_YT_SHORTS:
            # If YouTube RSS returns a short, try next entries
            for e in parsed.entries[:10]:
                lnk = (getattr(e, "link", "") or "").strip()
                if not lnk:
                    continue
                # Most shorts are /shorts/<id> or have #shorts in title
                if "/shorts/" in lnk.lower():
                    continue
                ttl = (getattr(e, "title", "") or "").lower()
                if "#shorts" in ttl or "shorts" == ttl.strip():
                    continue
                return lnk
        return link
    except Exception as ex:
        print(f"[YT] RSS failed: {ex}")
        return ""


# =========================
# Adilo Latest (API then scrape)
# =========================

ADILO_WATCH_RE = re.compile(r"/watch/([A-Za-z0-9_-]{6,})")
ADILO_VIDEO_ID_RE = re.compile(r"[?&]id=([A-Za-z0-9_-]{6,})")
ADILO_ANY_ID_RE = re.compile(r"([A-Za-z0-9_-]{6,})")

def adilo_api_latest_watch_url() -> str:
    """
    Best-case: use API meta upload_date and pick newest.
    If your keys are wrong or Adilo rejects, this will fail and we fall back.
    """
    if not (ADILO_PROJECT_ID and ADILO_PUBLIC_KEY and ADILO_SECRET_KEY):
        print("[ADILO] API not attempted (missing ADILO_PROJECT_ID / ADILO_PUBLIC_KEY / ADILO_SECRET_KEY).")
        return ""

    base = "https://adilo-api.bigcommand.com/v1"
    s = http_session()
    # NOTE: Adilo auth can vary. If you keep getting 401, it’s almost always header format.
    # We’ll keep what you had previously (public/secret as headers) but log failures.
    s.headers.update({
        "X-Public-Key": ADILO_PUBLIC_KEY,
        "X-Secret-Key": ADILO_SECRET_KEY,
    })

    try:
        # Pull first page; if it’s not sorted, we’ll probe by scanning more
        url = f"{base}/projects/{ADILO_PROJECT_ID}/files?From=1&To=50"
        r = s.get(url, timeout=25)
        r.raise_for_status()
        data = r.json()
        items = data.get("items") or data.get("data") or data.get("Files") or []
        total = data.get("total") or data.get("Total") or len(items)

        # If shape isn't known, just fall back
        if not isinstance(items, list) or not items:
            return ""

        # Try meta for up to first 50 items
        newest_dt = None
        newest_id = None
        for it in items[:50]:
            fid = it.get("id") or it.get("file_id") or it.get("fileId") or it.get("uuid")
            if not fid:
                continue
            meta_url = f"{base}/files/{fid}/meta"
            mr = s.get(meta_url, timeout=25)
            mr.raise_for_status()
            meta = mr.json()
            # common keys
            up = meta.get("upload_date") or meta.get("UploadDate") or meta.get("uploaded_at") or meta.get("created_at")
            dt = safe_parse_datetime(up)
            if dt and (newest_dt is None or dt > newest_dt):
                newest_dt = dt
                newest_id = fid

        if newest_id:
            return f"https://adilo.bigcommand.com/watch/{newest_id}"
        return ""
    except Exception as ex:
        print(f"[ADILO] API failed: {ex}")
        return ""


def adilo_scrape_latest_watch_url(cache: Dict) -> str:
    """
    Scrape public pages. Key design:
    - Avoid returning /home unless truly no other option
    - Prefer explicit video?id=... or og:url with id
    - If multiple IDs exist, choose the LAST unique ID found (tends to be newest with their current markup)
    - Cache last good watch URL so a timeout doesn't nuke you back to /home
    """
    s = http_session()

    def get_html(url: str, timeout: int = 25) -> str:
        r = s.get(url, timeout=timeout)
        r.raise_for_status()
        return r.text

    cb = int(time.time() * 1000)
    candidates = [
        ADILO_PUBLIC_LATEST_PAGE,
        f"{ADILO_PUBLIC_LATEST_PAGE}?cb={cb}",
        f"{ADILO_PUBLIC_LATEST_PAGE}/?cb={cb}",
        f"{ADILO_PUBLIC_LATEST_PAGE}?video=latest&cb={cb}",
        f"{ADILO_PUBLIC_LATEST_PAGE}?id=&cb={cb}",
        ADILO_PUBLIC_HOME_PAGE,
    ]

    all_ids_in_order: List[str] = []

    for url in candidates:
        try:
            print(f"[ADILO] SCRAPE timeout=25 url={url}")
            html = get_html(url, timeout=25)

            # 1) Strong signal: canonical/og:url with ?id=
            soup = BeautifulSoup(html, "html.parser")
            for sel in [
                ("meta", {"property": "og:url"}),
                ("meta", {"name": "og:url"}),
                ("link", {"rel": "canonical"}),
            ]:
                tag = soup.find(sel[0], sel[1])
                if tag:
                    content = tag.get("content") or tag.get("href") or ""
                    m = ADILO_VIDEO_ID_RE.search(content)
                    if m:
                        vid = m.group(1)
                        watch = f"https://adilo.bigcommand.com/watch/{vid}"
                        cache["last_adilo_watch_url"] = watch
                        cache["last_adilo_seen_at"] = now_local().isoformat()
                        save_cache(cache)
                        return watch

            # 2) Next: any explicit /video?id= occurrences
            for m in re.finditer(r"/c/ittybittygamingnews/video\?id=([A-Za-z0-9_-]{6,})", html):
                all_ids_in_order.append(m.group(1))

            # 3) Next: any /watch/<id>
            for m in ADILO_WATCH_RE.finditer(html):
                all_ids_in_order.append(m.group(1))

            # If we collected anything from this page, stop and decide
            if all_ids_in_order:
                # keep order, unique
                uniq: List[str] = []
                seen = set()
                for i in all_ids_in_order:
                    if i not in seen:
                        seen.add(i)
                        uniq.append(i)

                # Heuristic that has worked better with Adilo’s current hub markup:
                # choose the LAST id on the page (newest tends to be appended / later in markup)
                chosen = uniq[-1]
                watch = f"https://adilo.bigcommand.com/watch/{chosen}"
                print(f"[ADILO] SCRAPE found id={chosen} -> {watch}")

                cache["last_adilo_watch_url"] = watch
                cache["last_adilo_seen_at"] = now_local().isoformat()
                save_cache(cache)
                return watch

        except requests.exceptions.Timeout:
            print(f"[ADILO] Timeout url={url} (timeout=25)")
        except Exception as ex:
            print(f"[ADILO] SCRAPE failed: {ex}")

    # If we got here, scraping failed. Use cache if available.
    cached = (cache or {}).get("last_adilo_watch_url", "").strip()
    if cached:
        print(f"[ADILO] Using cached last_adilo_watch_url: {cached}")
        return cached

    print(f"[ADILO] Falling back: {ADILO_PUBLIC_HOME_PAGE}")
    return ADILO_PUBLIC_HOME_PAGE


def get_latest_adilo_watch_url(cache: Dict) -> str:
    # 1) try API
    watch = adilo_api_latest_watch_url()
    if watch:
        cache["last_adilo_watch_url"] = watch
        cache["last_adilo_seen_at"] = now_local().isoformat()
        save_cache(cache)
        return watch

    # 2) scrape
    return adilo_scrape_latest_watch_url(cache)


# =========================
# Discord Posting
# =========================

def discord_post(content: str = "", embeds: Optional[List[Dict]] = None) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL")

    payload: Dict = {}
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds

    s = http_session()
    r = s.post(DISCORD_WEBHOOK_URL, json=payload, timeout=25)
    r.raise_for_status()


def build_story_embeds(stories: List[Story]) -> List[Dict]:
    embeds: List[Dict] = []
    for idx, s in enumerate(stories, start=1):
        desc_lines = []
        if s.summary:
            desc_lines.append(clamp(s.summary, 260))
        # Put the link UNDER the story (this is what you asked for)
        desc_lines.append(s.url)

        tags = " • ".join([f"#{t.replace(' ', '')}" for t in (s.tags or [])])

        embed = {
            "title": f"{idx}) {clamp(s.title, 240)}",
            "description": "\n".join(desc_lines).strip(),
            "footer": {"text": f"Source: {s.source}" + (f"   {tags}" if tags else "")},
        }
        embeds.append(embed)

    return embeds


def build_digest_header(stories: List[Story]) -> str:
    local = now_local()
    date_str = local.strftime("%B %d, %Y")

    bullets = []
    for s in stories[:3]:
        bullets.append(f"► 🎮 {clamp(s.title, 80)}")

    header = []
    header.append(NEWSLETTER_TAGLINE)
    header.append("")
    header.append(date_str)
    header.append("")
    header.append(f"In Tonight’s Edition of {NEWSLETTER_NAME}…")
    header.extend(bullets)
    header.append("")
    header.append("Tonight’s Top Stories")
    return "\n".join(header).strip()


def update_state_with_stories(state: Dict, posted: List[Story]) -> None:
    seen_urls = state.get("seen_urls", [])
    seen_titles = state.get("seen_titles", [])
    seen_story_keys = state.get("seen_story_keys", [])

    for s in posted:
        if s.url not in seen_urls:
            seen_urls.append(s.url)
        if s.title not in seen_titles:
            seen_titles.append(s.title)

        key = f"{s.source}::{s.title.lower().strip()}"
        if key not in seen_story_keys:
            seen_story_keys.append(key)

    # Keep bounded
    state["seen_urls"] = seen_urls[-4000:]
    state["seen_titles"] = seen_titles[-4000:]
    state["seen_story_keys"] = seen_story_keys[-8000:]


# =========================
# Main
# =========================

def main() -> None:
    cache = load_cache()
    if not guard_should_post_now():
        # Exit 0 so GitHub Actions doesn't show failure
        return
    if not post_once_per_day_guard(cache):
        return

    state = load_state()

    # Fetch stories
    fetched = fetch_rss_items()
    if not fetched:
        print("[DIGEST] No feed items fetched. Exiting without posting.")
        return

    filtered = dedupe_and_filter(fetched, state)
    top = filtered[:DIGEST_TOP_N]
    if not top:
        print("[DIGEST] No items found in window. Exiting without posting.")
        return

    # Resolve latest videos
    yt_url = get_latest_youtube_url()
    adilo_watch = get_latest_adilo_watch_url(cache)

    # -------------------------
    # Message #1: Digest + embeds (NO YouTube/Adilo URLs here)
    # -------------------------
    header = build_digest_header(top)
    embeds = build_story_embeds(top)

    # Keep the header short-ish; Discord is picky when content + embeds gets huge
    header = clamp(header, 1500)

    discord_post(header, embeds)

    # -------------------------
    # Message #2: YouTube URL ALONE (for playable unfurl)
    # -------------------------
    # IMPORTANT: to get the playable card, the URL needs to be a plain standalone message.
    if yt_url:
        discord_post(yt_url, embeds=None)

    # -------------------------
    # Message #3: Adilo URL ALONE (for Adilo card/unfurl)
    # -------------------------
    if adilo_watch:
        discord_post(adilo_watch, embeds=None)

    # Mark posted + update state
    update_state_with_stories(state, top)
    save_state(state)

    mark_posted_today(cache)

    print("[DONE] Digest posted.")
    if yt_url:
        print(f"[DONE] YouTube: {yt_url}")
    if adilo_watch:
        print(f"[DONE] Featured Adilo video: {adilo_watch}")


if __name__ == "__main__":
    main()
