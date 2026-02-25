#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import json
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple

import requests
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dateparser


# =========================
# CONFIG
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
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

DIGEST_WINDOW_HOURS = int(os.getenv("DIGEST_WINDOW_HOURS", "24").strip() or "24")
DIGEST_TOP_N = int(os.getenv("DIGEST_TOP_N", "5").strip() or "5")
DIGEST_MAX_PER_SOURCE = int(os.getenv("DIGEST_MAX_PER_SOURCE", "1").strip() or "1")

NEWSLETTER_NAME = os.getenv("NEWSLETTER_NAME", "Itty Bitty Gaming News").strip() or "Itty Bitty Gaming News"
NEWSLETTER_TAGLINE = os.getenv("NEWSLETTER_TAGLINE", "Snackable daily gaming news — five days a week.").strip()

DIGEST_FORCE_POST = (os.getenv("DIGEST_FORCE_POST", "").strip().lower() in ("1", "true", "yes", "y", "on"))

DIGEST_GUARD_TZ = os.getenv("DIGEST_GUARD_TZ", "America/Los_Angeles").strip() or "America/Los_Angeles"
DIGEST_GUARD_LOCAL_HOUR = int(os.getenv("DIGEST_GUARD_LOCAL_HOUR", "19").strip() or "19")
DIGEST_GUARD_LOCAL_MINUTE = int(os.getenv("DIGEST_GUARD_LOCAL_MINUTE", "0").strip() or "0")
DIGEST_GUARD_WINDOW_MINUTES = int(os.getenv("DIGEST_GUARD_WINDOW_MINUTES", "120").strip() or "120")

# YouTube (auto / forced)
YOUTUBE_FEATURED_URL = os.getenv("YOUTUBE_FEATURED_URL", "").strip()
YOUTUBE_FEATURED_TITLE = os.getenv("YOUTUBE_FEATURED_TITLE", "").strip()
YOUTUBE_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "").strip()  # optional if you want auto-latest
YOUTUBE_UPLOADS_PLAYLIST_ID = os.getenv("YOUTUBE_UPLOADS_PLAYLIST_ID", "").strip()  # optional

# Adilo
FEATURED_VIDEO_TITLE = os.getenv("FEATURED_VIDEO_TITLE", f"Watch today’s {NEWSLETTER_NAME}").strip()
FEATURED_VIDEO_FALLBACK_URL = os.getenv(
    "FEATURED_VIDEO_FALLBACK_URL",
    "https://adilo.bigcommand.com/c/ittybittygamingnews/home",
).strip()

FEATURED_VIDEO_FORCE_ID = os.getenv("FEATURED_VIDEO_FORCE_ID", "").strip()  # watch id (e.g. K4AxdfCP)
ADILO_PUBLIC_LATEST_PAGE = os.getenv(
    "ADILO_PUBLIC_LATEST_PAGE",
    "https://adilo.bigcommand.com/c/ittybittygamingnews/video",
).strip()
ADILO_PUBLIC_HOME_PAGE = os.getenv(
    "ADILO_PUBLIC_HOME_PAGE",
    "https://adilo.bigcommand.com/c/ittybittygamingnews/home",
).strip()

STATE_PATH = "state.json"


# =========================
# HELPERS
# =========================
def http_get(url: str, timeout: int = 20) -> requests.Response:
    headers = {"User-Agent": USER_AGENT}
    return requests.get(url, headers=headers, timeout=timeout)


def safe_text(s: str) -> str:
    return (s or "").replace("\r", " ").strip()


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    url = re.sub(r"#.*$", "", url)
    return url


def is_short_form(url: str, title: str) -> bool:
    t = (title or "").lower()
    u = (url or "").lower()
    if "/shorts/" in u:
        return True
    if "#shorts" in t:
        return True
    return False


def looks_like_rumor_or_opinion(title: str, summary: str) -> bool:
    text = f"{title} {summary}".lower()
    bad = [
        "rumor",
        "rumour",
        "speculation",
        "reportedly",
        "could",
        "might",
        "maybe",
        "leak",
        "leaked",
        "allegedly",
        "opinion",
        "debate:",
        "ranking",
        "best ",
        "top ",
        "history of",
        "review",
        "preview",
        "hands-on",
        "letters",
        "poll:",
        "poll -",
        "deal",
        "sale",
        "drops to",
        "percent off",
        "discount",
        "buy now",
        "gift guide",
        "controllers available",
        "power bank",
        "walt disney world",
        "audio-animatronics",
    ]
    return any(b in text for b in bad)


def extract_source_name(entry: Dict[str, Any], feed_url: str) -> str:
    # Prefer feed title if present; else host
    src = ""
    if "source" in entry and isinstance(entry["source"], dict):
        src = safe_text(entry["source"].get("title", ""))
    if not src:
        src = safe_text(entry.get("publisher", "")) or safe_text(entry.get("author", ""))
    if not src:
        m = re.match(r"^https?://([^/]+)/", feed_url)
        src = m.group(1) if m else "Source"
    return src


def parse_entry_datetime(entry: Dict[str, Any]) -> Optional[datetime]:
    # Try common feedparser fields
    for k in ("published", "updated", "created"):
        v = entry.get(k)
        if v:
            try:
                dt = dateparser.parse(v)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                pass
    # structured_time
    for k in ("published_parsed", "updated_parsed"):
        v = entry.get(k)
        if v:
            try:
                dt = datetime(*v[:6], tzinfo=timezone.utc)
                return dt
            except Exception:
                pass
    return None


def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_PATH):
        return {"seen_story_keys": []}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            st = json.load(f)
        if "seen_story_keys" not in st:
            st["seen_story_keys"] = []
        return st
    except Exception:
        return {"seen_story_keys": []}


def save_state(st: Dict[str, Any]) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, ensure_ascii=False)


def story_key(url: str, title: str) -> str:
    u = normalize_url(url)
    t = re.sub(r"\s+", " ", (title or "").strip().lower())
    return f"{u}::{t}"


def truncate(s: str, n: int) -> str:
    s = safe_text(s)
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)].rstrip() + "…"


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
# ADILO + YOUTUBE
# =========================
def adilo_watch_url_from_id(watch_id: str) -> str:
    return f"https://adilo.bigcommand.com/watch/{watch_id}"


def scrape_latest_adilo_watch_id() -> Optional[str]:
    """
    Pull the newest public video ID from your public page.
    This avoids the API ordering/staleness issues.
    """
    urls_to_try = [
        ADILO_PUBLIC_LATEST_PAGE,
        # A second format that sometimes exists:
        "https://adilo.bigcommand.com/c/ittybittygamingnews/video?id=",
    ]
    for base in urls_to_try:
        try:
            print(f"[ADILO] SCRAPE {base}")
            r = http_get(base, timeout=25)
            print(f"[ADILO] SCRAPE status={r.status_code}")
            if r.status_code != 200:
                continue

            html = r.text or ""
            soup = BeautifulSoup(str(html), "html.parser")

            # Strategy 1: look for links containing /watch/
            for a in soup.find_all("a", href=True):
                href = a["href"]
                m = re.search(r"/watch/([A-Za-z0-9_-]{6,})", href)
                if m:
                    return m.group(1)

            # Strategy 2: look for "video?id=XXXX"
            text = html
            m = re.search(r"video\?id=([A-Za-z0-9_-]{6,})", text)
            if m:
                return m.group(1)

            # Strategy 3: look for "/watch/XXXX" anywhere
            m = re.search(r"https?://adilo\.bigcommand\.com/watch/([A-Za-z0-9_-]{6,})", text)
            if m:
                return m.group(1)

        except Exception as e:
            print(f"[ADILO] SCRAPE failed: {e}")

    return None


def get_featured_adilo_watch_url() -> str:
    # 1) Forced ID always wins (what worked for you)
    if FEATURED_VIDEO_FORCE_ID:
        url = adilo_watch_url_from_id(FEATURED_VIDEO_FORCE_ID)
        print(f"[ADILO] Using FEATURED_VIDEO_FORCE_ID: {url}")
        return url

    # 2) Try scrape latest
    latest_id = scrape_latest_adilo_watch_id()
    if latest_id:
        url = adilo_watch_url_from_id(latest_id)
        print(f"[ADILO] Using scraped latest watch id: {url}")
        return url

    # 3) Fallback
    print(f"[ADILO] Falling back: {FEATURED_VIDEO_FALLBACK_URL}")
    return FEATURED_VIDEO_FALLBACK_URL or ADILO_PUBLIC_HOME_PAGE


def youtube_id_from_url(url: str) -> Optional[str]:
    u = (url or "").strip()
    # watch?v=VIDEOID
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{6,})", u)
    if m:
        return m.group(1)
    # youtu.be/VIDEOID
    m = re.search(r"youtu\.be/([A-Za-z0-9_-]{6,})", u)
    if m:
        return m.group(1)
    return None


def youtube_thumbnail(video_id: str) -> str:
    # maxres sometimes 404; discord will handle. fallback would be hqdefault.
    return f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg"


def fetch_latest_youtube_from_rss() -> Tuple[str, str]:
    """
    Best-effort latest YouTube upload.
    Uses channel_id RSS if provided. Otherwise returns empty.
    """
    candidates = []
    if YOUTUBE_CHANNEL_ID:
        candidates.append(f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}")
    if YOUTUBE_UPLOADS_PLAYLIST_ID:
        candidates.append(f"https://www.youtube.com/feeds/videos.xml?playlist_id={YOUTUBE_UPLOADS_PLAYLIST_ID}")

    for rss in candidates:
        try:
            print(f"[YT] GET {rss}")
            r = http_get(rss, timeout=20)
            r.raise_for_status()
            d = feedparser.parse(r.content)
            if d.entries:
                e = d.entries[0]
                link = safe_text(e.get("link", ""))
                title = safe_text(e.get("title", ""))
                if link and not is_short_form(link, title):
                    return link, title
        except Exception as e:
            print(f"[YT] RSS failed: {e}")

    return "", ""


def get_featured_youtube() -> Tuple[str, str]:
    """
    Priority:
      1) env-provided featured URL (your vars)
      2) auto-latest from RSS (if channel_id/playlist_id provided)
      3) nothing
    """
    if YOUTUBE_FEATURED_URL:
        # Filter shorts
        if not is_short_form(YOUTUBE_FEATURED_URL, YOUTUBE_FEATURED_TITLE):
            return YOUTUBE_FEATURED_URL, (YOUTUBE_FEATURED_TITLE or "Watch on YouTube")
        return "", ""

    link, title = fetch_latest_youtube_from_rss()
    return link, title


# =========================
# FEEDS
# =========================
def get_feed_urls() -> List[str]:
    env = os.getenv("FEED_URLS", "").strip()
    if not env:
        return DEFAULT_FEEDS[:]
    # FEED_URLS can be newline or comma separated
    parts = []
    for line in env.splitlines():
        line = line.strip()
        if not line:
            continue
        parts.extend([p.strip() for p in line.split(",") if p.strip()])
    return parts or DEFAULT_FEEDS[:]


def fetch_feed_items() -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for feed_url in get_feed_urls():
        try:
            print(f"[RSS] GET {feed_url}")
            r = http_get(feed_url, timeout=25)
            r.raise_for_status()

            d = feedparser.parse(r.content)

            # bozo is not fatal; log it and continue
            if getattr(d, "bozo", 0) == 1:
                ex = getattr(d, "bozo_exception", None)
                if ex:
                    print(f"[RSS] bozo=1 for {feed_url}: {ex}")

            for e in d.entries or []:
                link = normalize_url(safe_text(e.get("link", "")))
                title = safe_text(e.get("title", ""))
                summary = safe_text(e.get("summary", "")) or safe_text(e.get("description", ""))

                if not link or not title:
                    continue

                # Filter short-form (YouTube shorts-like in general)
                if is_short_form(link, title):
                    continue

                # Filter rumor/opinion/deals/etc
                if looks_like_rumor_or_opinion(title, summary):
                    continue

                dt = parse_entry_datetime(e) or datetime.now(timezone.utc)

                src = extract_source_name(e, feed_url)
                items.append(
                    {
                        "title": title,
                        "link": link,
                        "summary": summary,
                        "source": src,
                        "dt": dt,
                    }
                )
        except Exception as e:
            print(f"[RSS] Feed failed: {feed_url} ({e})")

    return items


# =========================
# DISCORD
# =========================
def build_tags_from_title(title: str) -> str:
    """
    Lightweight tags: pull major tokens, keep it short.
    """
    t = re.sub(r"[^A-Za-z0-9\s:-]", " ", title or "")
    tokens = [w for w in t.split() if len(w) >= 4]
    # Prefer capital-ish words (original title)
    raw_words = re.findall(r"\b[A-Z][A-Za-z0-9]+\b", title or "")
    candidates = raw_words[:]
    for w in tokens:
        if w not in candidates and w[0].isalpha():
            candidates.append(w)

    # De-dupe preserve order
    seen = set()
    out = []
    for w in candidates:
        key = w.lower()
        if key in seen:
            continue
        seen.add(key)
        if len(out) >= 3:
            break
        out.append("#" + re.sub(r"[^A-Za-z0-9]", "", w))

    return " ".join(out)


def make_newsletter_message(top: List[Dict[str, Any]], now_local: datetime) -> str:
    date_str = now_local.strftime("%B %d, %Y")
    header = f"{date_str}\n\nIn Tonight’s Edition of {NEWSLETTER_NAME}…\n"
    bullets = []
    for item in top[:3]:
        bullets.append(f"► 🎮 {truncate(item['title'], 90)}")
    bullet_block = "\n".join(bullets) if bullets else "► 🎮 (No major updates found)"
    intro = (
        f"{header}{bullet_block}\n\n"
        f"{NEWSLETTER_TAGLINE}\n\n"
        f"Tonight’s Top Stories\n"
    )

    # Per-story blocks with link directly under the story (your requirement)
    blocks = []
    for i, item in enumerate(top, start=1):
        title = item["title"]
        src = item["source"]
        link = item["link"]
        summary = item["summary"]

        # Clean summary from HTML if present
        summary_txt = BeautifulSoup(summary or "", "html.parser").get_text(" ", strip=True)
        summary_txt = truncate(summary_txt, 320)

        tags = build_tags_from_title(title)

        block = (
            f"\n{i}) {title}\n"
            f"{summary_txt}\n"
            f"Source: {src}\n"
            f"{link}\n"
        )
        if tags:
            block += f"{tags}\n"

        blocks.append(block)

    outro = (
        "\n—\n"
        "That’s it for tonight’s Itty Bitty. 😄\n"
        "Catch the snackable breakdown on Itty Bitty Gaming News tomorrow.\n"
    )
    return intro + "".join(blocks) + outro


def discord_post(content: str, embeds: Optional[List[Dict[str, Any]]] = None) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL")

    payload: Dict[str, Any] = {"content": content}
    if embeds:
        payload["embeds"] = embeds[:10]

    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=25)
    r.raise_for_status()


def build_video_embeds(youtube_url: str, youtube_title: str, adilo_url: str) -> List[Dict[str, Any]]:
    embeds: List[Dict[str, Any]] = []

    # YouTube first (your preference)
    if youtube_url:
        vid = youtube_id_from_url(youtube_url)
        emb: Dict[str, Any] = {
            "title": youtube_title or "Watch on YouTube",
            "url": youtube_url,
        }
        if vid:
            emb["thumbnail"] = {"url": youtube_thumbnail(vid)}
        embeds.append(emb)

    # Adilo (thumbnail best-effort; not required)
    if adilo_url:
        emb2: Dict[str, Any] = {
            "title": FEATURED_VIDEO_TITLE or "Featured video (Adilo)",
            "url": adilo_url,
        }
        embeds.append(emb2)

    return embeds


# =========================
# MAIN
# =========================
def main() -> None:
    if not guard_should_post_now():
        # exit cleanly so Actions doesn't show failure
        return

    # Pull items
    items = fetch_feed_items()
    if not items:
        print("[DIGEST] No feed items fetched. Exiting without posting.")
        return

    # Filter by time window
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=DIGEST_WINDOW_HOURS)
    items = [it for it in items if it["dt"] >= cutoff]

    # De-dupe and cap per source
    st = load_state()
    seen_keys = set(st.get("seen_story_keys", []))

    # Sort newest first
    items.sort(key=lambda x: x["dt"], reverse=True)

    per_source_count: Dict[str, int] = {}
    picked: List[Dict[str, Any]] = []
    for it in items:
        k = story_key(it["link"], it["title"])
        if k in seen_keys:
            continue
        src = it["source"]
        per_source_count[src] = per_source_count.get(src, 0)
        if per_source_count[src] >= DIGEST_MAX_PER_SOURCE:
            continue
        picked.append(it)
        per_source_count[src] += 1
        if len(picked) >= DIGEST_TOP_N:
            break

    if not picked:
        print("[DIGEST] No items found in window after de-dupe. Exiting without posting.")
        return

    # Build message
    now_local = datetime.now(ZoneInfo(DIGEST_GUARD_TZ))

    msg = make_newsletter_message(picked, now_local)

    # Ensure Discord 2000 char limit (leave room)
    # If too long, reduce summaries and/or drop last story.
    summary_max_len = 320
    while len(msg) > 1950 and summary_max_len > 140:
        summary_max_len -= 40
        # rebuild with shorter summaries
        for it in picked:
            it["summary"] = truncate(BeautifulSoup(it["summary"] or "", "html.parser").get_text(" ", strip=True), summary_max_len)
        msg = make_newsletter_message(picked, now_local)

    while len(msg) > 1950 and len(picked) > 3:
        print(f"[DISCORD] Still too long. Dropping story #{len(picked)} to fit Discord limits.")
        picked = picked[:-1]
        msg = make_newsletter_message(picked, now_local)

    print(f"[DISCORD] Message size OK: {len(msg)} chars (summary_max_len={summary_max_len})")

    # Featured videos
    yt_url, yt_title = get_featured_youtube()
    adilo_url = get_featured_adilo_watch_url()

    embeds = build_video_embeds(yt_url, yt_title, adilo_url)

    # Post
    discord_post(msg, embeds=embeds)

    # Persist state (mark picked as seen)
    for it in picked:
        seen_keys.add(story_key(it["link"], it["title"]))

    st["seen_story_keys"] = list(seen_keys)[-5000:]  # cap growth
    save_state(st)

    print("[DONE] Digest posted.")
    if yt_url:
        print(f"[DONE] YouTube featured: {yt_url}")
    print(f"[DONE] Featured Adilo video: {adilo_url}")


if __name__ == "__main__":
    main()
