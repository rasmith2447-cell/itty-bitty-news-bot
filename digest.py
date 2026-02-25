#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import json
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

# YouTube: easiest + most reliable is RSS url
YOUTUBE_RSS_URL = os.getenv("YOUTUBE_RSS_URL", "").strip()
YOUTUBE_FEATURED_URL = os.getenv("YOUTUBE_FEATURED_URL", "").strip()
YOUTUBE_FEATURED_TITLE = os.getenv("YOUTUBE_FEATURED_TITLE", "").strip()

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
# HTTP
# =========================
def http_get(url: str, timeout: int = 25) -> requests.Response:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    return requests.get(url, headers=headers, timeout=timeout)


def safe_text(s: str) -> str:
    return (s or "").replace("\r", " ").strip()


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    url = re.sub(r"#.*$", "", url)
    return url


def truncate(s: str, n: int) -> str:
    s = safe_text(s)
    if len(s) <= n:
        return s
    return s[: max(0, n - 1)].rstrip() + "…"


# =========================
# GUARD
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
        "rumor", "rumour", "speculation", "reportedly", "allegedly", "leak", "leaked",
        "opinion", "debate:", "ranking", "best ", "top ", "history of", "review", "preview", "hands-on",
        "letters", "poll:", "deal", "sale", "drops to", "percent off", "discount", "buy now", "gift guide",
        "power bank", "walt disney world", "audio-animatronics",
    ]
    return any(b in text for b in bad)


# =========================
# FEEDS
# =========================
def get_feed_urls() -> List[str]:
    env = os.getenv("FEED_URLS", "").strip()
    if not env:
        return DEFAULT_FEEDS[:]
    parts: List[str] = []
    for line in env.splitlines():
        line = line.strip()
        if not line:
            continue
        parts.extend([p.strip() for p in line.split(",") if p.strip()])
    return parts or DEFAULT_FEEDS[:]


def parse_entry_datetime(entry: Dict[str, Any]) -> Optional[datetime]:
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
    for k in ("published_parsed", "updated_parsed"):
        v = entry.get(k)
        if v:
            try:
                return datetime(*v[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def extract_source_name(entry: Dict[str, Any], feed_url: str) -> str:
    src = ""
    if "source" in entry and isinstance(entry["source"], dict):
        src = safe_text(entry["source"].get("title", ""))
    if not src:
        m = re.match(r"^https?://([^/]+)/", feed_url)
        src = m.group(1) if m else "Source"
    return src


def fetch_feed_items() -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for feed_url in get_feed_urls():
        try:
            print(f"[RSS] GET {feed_url}")
            r = http_get(feed_url, timeout=25)
            r.raise_for_status()
            d = feedparser.parse(r.content)

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
                if is_short_form(link, title):
                    continue
                if looks_like_rumor_or_opinion(title, summary):
                    continue

                dt = parse_entry_datetime(e) or datetime.now(timezone.utc)
                src = extract_source_name(e, feed_url)

                items.append({"title": title, "link": link, "summary": summary, "source": src, "dt": dt})
        except Exception as e:
            print(f"[RSS] Feed failed: {feed_url} ({e})")
    return items


# =========================
# STATE
# =========================
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


# =========================
# TAGS
# =========================
def build_tags_from_title(title: str) -> str:
    raw_words = re.findall(r"\b[A-Z][A-Za-z0-9]+\b", title or "")
    candidates = raw_words[:]
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


# =========================
# YOUTUBE
# =========================
def fetch_latest_youtube() -> Tuple[str, str]:
    # 1) explicit featured URL wins
    if YOUTUBE_FEATURED_URL and not is_short_form(YOUTUBE_FEATURED_URL, YOUTUBE_FEATURED_TITLE):
        return YOUTUBE_FEATURED_URL, (YOUTUBE_FEATURED_TITLE or "Watch on YouTube")

    # 2) RSS URL auto-latest
    if not YOUTUBE_RSS_URL:
        return "", ""

    try:
        print(f"[YT] GET {YOUTUBE_RSS_URL}")
        r = http_get(YOUTUBE_RSS_URL, timeout=20)
        r.raise_for_status()
        d = feedparser.parse(r.content)
        if not d.entries:
            return "", ""
        e = d.entries[0]
        link = safe_text(e.get("link", ""))
        title = safe_text(e.get("title", "")) or "Watch on YouTube"
        if link and not is_short_form(link, title):
            return link, title
    except Exception as e:
        print(f"[YT] RSS failed: {e}")

    return "", ""


# =========================
# ADILO
# =========================
def adilo_watch_url_from_id(watch_id: str) -> str:
    return f"https://adilo.bigcommand.com/watch/{watch_id}"


def scrape_latest_adilo_watch_id() -> Optional[str]:
    """
    Robust scrape:
    - looks for /watch/ID
    - looks for video?id=ID
    - looks for watch/ID in any JS/HTML
    """
    try_urls = [ADILO_PUBLIC_LATEST_PAGE]

    for url in try_urls:
        try:
            print(f"[ADILO] SCRAPE {url}")
            r = http_get(url, timeout=25)
            print(f"[ADILO] SCRAPE status={r.status_code}")
            if r.status_code != 200:
                continue

            html = r.text or ""

            # 1) direct /watch/ID anywhere
            m = re.search(r"/watch/([A-Za-z0-9_-]{6,})", html)
            if m:
                return m.group(1)

            # 2) video?id=ID anywhere
            m = re.search(r"video\?id=([A-Za-z0-9_-]{6,})", html)
            if m:
                return m.group(1)

            # 3) sometimes ID is in JSON-like blobs
            m = re.search(r"\"id\"\s*:\s*\"([A-Za-z0-9_-]{6,})\"", html)
            if m:
                return m.group(1)

            # 4) fallback parse anchors
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                m = re.search(r"/watch/([A-Za-z0-9_-]{6,})", href)
                if m:
                    return m.group(1)
                m = re.search(r"video\?id=([A-Za-z0-9_-]{6,})", href)
                if m:
                    return m.group(1)

        except Exception as e:
            print(f"[ADILO] SCRAPE failed: {e}")

    return None


def get_featured_adilo_watch_url() -> str:
    if FEATURED_VIDEO_FORCE_ID:
        url = adilo_watch_url_from_id(FEATURED_VIDEO_FORCE_ID)
        print(f"[ADILO] Using FEATURED_VIDEO_FORCE_ID: {url}")
        return url

    latest_id = scrape_latest_adilo_watch_id()
    if latest_id:
        url = adilo_watch_url_from_id(latest_id)
        print(f"[ADILO] Using scraped latest watch id: {url}")
        return url

    print(f"[ADILO] Falling back: {FEATURED_VIDEO_FALLBACK_URL}")
    return FEATURED_VIDEO_FALLBACK_URL or ADILO_PUBLIC_HOME_PAGE


# =========================
# DISCORD POST (CONTENT + EMBEDS)
# =========================
def discord_post(content: str, embeds: List[Dict[str, Any]]) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL")

    payload: Dict[str, Any] = {"content": content}
    if embeds:
        payload["embeds"] = embeds[:10]

    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=25)
    r.raise_for_status()


def clean_summary(summary_html: str, max_len: int) -> str:
    txt = BeautifulSoup(summary_html or "", "html.parser").get_text(" ", strip=True)
    return truncate(txt, max_len)


def build_story_embed(i: int, item: Dict[str, Any], summary_max: int) -> Dict[str, Any]:
    title = item["title"]
    link = item["link"]
    src = item["source"]
    summary = clean_summary(item.get("summary", ""), summary_max)
    tags = build_tags_from_title(title)

    desc_parts = []
    if summary:
        desc_parts.append(summary)
    if tags:
        desc_parts.append(tags)

    embed: Dict[str, Any] = {
        "title": f"{i}) {title}",
        "url": link,
        "description": "\n".join(desc_parts)[:4096],
        "footer": {"text": f"Source: {src}"},
    }
    return embed


def build_newsletter_content(top: List[Dict[str, Any]], now_local: datetime, yt_url: str, adilo_url: str) -> str:
    date_str = now_local.strftime("%B %d, %Y")

    bullets = []
    for item in top[:3]:
        bullets.append(f"► 🎮 {truncate(item['title'], 90)}")
    bullet_block = "\n".join(bullets) if bullets else "► 🎮 (No major updates found)"

    # Put YouTube FIRST, as requested. Include the URL in content so Discord unfurls a playable preview.
    video_lines = []
    if yt_url:
        video_lines.append(f"▶️ YouTube (latest)\n{yt_url}")
    if adilo_url:
        video_lines.append(f"📺 Adilo (latest)\n{adilo_url}")

    video_block = "\n\n".join(video_lines).strip()

    content = (
        f"{date_str}\n\n"
        f"In Tonight’s Edition of {NEWSLETTER_NAME}…\n"
        f"{bullet_block}\n\n"
        f"{NEWSLETTER_TAGLINE}\n\n"
        f"{video_block}\n\n"
        f"Tonight’s Top Stories (cards below)\n"
        f"—\n"
        f"That’s it for tonight’s Itty Bitty. 😄\n"
        f"Catch the snackable breakdown on Itty Bitty Gaming News tomorrow.\n"
    )

    # Keep content comfortably under Discord limit (embeds carry the details)
    return content[:1900]


# =========================
# MAIN
# =========================
def main() -> None:
    if not guard_should_post_now():
        return

    items = fetch_feed_items()
    if not items:
        print("[DIGEST] No feed items fetched. Exiting without posting.")
        return

    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(hours=DIGEST_WINDOW_HOURS)
    items = [it for it in items if it["dt"] >= cutoff]
    if not items:
        print("[DIGEST] No items in time window. Exiting without posting.")
        return

    items.sort(key=lambda x: x["dt"], reverse=True)

    st = load_state()
    seen_keys = set(st.get("seen_story_keys", []))

    per_source: Dict[str, int] = {}
    picked: List[Dict[str, Any]] = []
    for it in items:
        k = story_key(it["link"], it["title"])
        if k in seen_keys:
            continue
        src = it["source"]
        per_source[src] = per_source.get(src, 0)
        if per_source[src] >= DIGEST_MAX_PER_SOURCE:
            continue
        picked.append(it)
        per_source[src] += 1
        if len(picked) >= DIGEST_TOP_N:
            break

    if not picked:
        print("[DIGEST] No items after de-dupe. Exiting without posting.")
        return

    # Featured videos
    yt_url, yt_title = fetch_latest_youtube()
    adilo_url = get_featured_adilo_watch_url()

    now_local = datetime.now(ZoneInfo(DIGEST_GUARD_TZ))
    content = build_newsletter_content(picked, now_local, yt_url, adilo_url)

    # Build embeds: one card per story, in order.
    # If Discord complains about size, reduce summary length.
    summary_max = 260
    embeds = [build_story_embed(i + 1, it, summary_max) for i, it in enumerate(picked)]

    # Try posting; if webhook returns 400 due to embed size, shrink summaries and retry once.
    try:
        discord_post(content, embeds)
    except requests.exceptions.HTTPError as e:
        # 400 is common when embed descriptions are too large collectively
        if "400" in str(e):
            summary_max = 140
            embeds = [build_story_embed(i + 1, it, summary_max) for i, it in enumerate(picked)]
            discord_post(content, embeds)
        else:
            raise

    # Update state
    for it in picked:
        seen_keys.add(story_key(it["link"], it["title"]))
    st["seen_story_keys"] = list(seen_keys)[-5000:]
    save_state(st)

    print("[DONE] Digest posted.")
    if yt_url:
        print(f"[DONE] YouTube featured: {yt_url}")
    print(f"[DONE] Featured Adilo video: {adilo_url}")


if __name__ == "__main__":
    main()
