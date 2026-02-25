import os
import re
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

import requests
import feedparser
from bs4 import BeautifulSoup

# =============================
# CONFIG
# =============================

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

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNews/Digest").strip()

DIGEST_WINDOW_HOURS = int(os.getenv("DIGEST_WINDOW_HOURS", "24").strip())
DIGEST_TOP_N = int(os.getenv("DIGEST_TOP_N", "5").strip())
DIGEST_MAX_PER_SOURCE = int(os.getenv("DIGEST_MAX_PER_SOURCE", "1").strip())

YOUTUBE_RSS_URL = os.getenv(
    "YOUTUBE_RSS_URL",
    "https://www.youtube.com/feeds/videos.xml?channel_id=UC0SJd4h7GQqoYTVjlDnSzqQ",
).strip()

ADILO_PUBLIC_LATEST_PAGE = os.getenv(
    "ADILO_PUBLIC_LATEST_PAGE",
    "https://adilo.bigcommand.com/c/ittybittygamingnews/video",
).strip()
ADILO_PUBLIC_HOME_PAGE = os.getenv(
    "ADILO_PUBLIC_HOME_PAGE",
    "https://adilo.bigcommand.com/c/ittybittygamingnews/home",
).strip()

DIGEST_FORCE_POST = os.getenv("DIGEST_FORCE_POST", "").strip().lower() in ("1", "true", "yes", "y")

DIGEST_GUARD_TZ = os.getenv("DIGEST_GUARD_TZ", "America/Los_Angeles").strip()
DIGEST_GUARD_LOCAL_HOUR = int(os.getenv("DIGEST_GUARD_LOCAL_HOUR", "19").strip())
DIGEST_GUARD_LOCAL_MINUTE = int(os.getenv("DIGEST_GUARD_LOCAL_MINUTE", "0").strip())
DIGEST_GUARD_WINDOW_MINUTES = int(os.getenv("DIGEST_GUARD_WINDOW_MINUTES", "120").strip())

STATE_PATH = "digest_state.json"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})


# =============================
# TIME / GUARD
# =============================

def now_local() -> datetime:
    return datetime.now(ZoneInfo(DIGEST_GUARD_TZ))


def guard_should_post_now() -> bool:
    if DIGEST_FORCE_POST:
        print("[GUARD] DIGEST_FORCE_POST enabled — bypassing time guard.")
        return True

    tz = ZoneInfo(DIGEST_GUARD_TZ)
    now = datetime.now(tz)
    target = now.replace(
        hour=DIGEST_GUARD_LOCAL_HOUR,
        minute=DIGEST_GUARD_LOCAL_MINUTE,
        second=0,
        microsecond=0,
    )
    delta_min = abs((now - target).total_seconds()) / 60.0

    if delta_min <= DIGEST_GUARD_WINDOW_MINUTES:
        print(f"[GUARD] OK. Local now: {now.strftime('%Y-%m-%d %H:%M:%S %Z')} Delta={delta_min:.1f}min")
        return True

    print(f"[GUARD] Not within posting window. Local now: {now.strftime('%Y-%m-%d %H:%M:%S %Z')} Delta={delta_min:.1f}min")
    return False


# =============================
# STATE
# =============================

def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_posted_digest_date": ""}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# =============================
# NORMALIZE / FILTER
# =============================

def safe_text(s: str) -> str:
    return (s or "").strip()


def normalize_title(t: str) -> str:
    t = safe_text(t).lower()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[^a-z0-9 :'\-–—/().]", "", t)
    return t.strip()


def is_probably_not_news(title: str) -> bool:
    t = normalize_title(title)

    bad_starts = (
        "best ", "top ", "every ", "all ", "ranking", "ranked",
        "review:", "reviews:", "history of", "poll:", "poll ",
        "debate:", "opinion:", "op-ed:", "guide:", "how to",
        "deal:", "deals:", "sale:", "discount", "drops to",
    )
    for p in bad_starts:
        if t.startswith(p):
            return True

    bad_contains = (
        "controller", "power bank", "prime day", "black friday", "cyber monday",
        "at woot", "discount", "letters", "mailbox", "favourite", "favorite",
        "cosplay", "walt disney world", "audio-animatronics", "olaf", "disney",
        "rumor", "rumour", "leak", "leaked", "speculation", "reportedly",
    )
    for c in bad_contains:
        if c in t:
            return True

    return False


def get_source_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "").strip().lower()
    except Exception:
        return "unknown"


def parse_entry_time(entry) -> datetime | None:
    for k in ("published_parsed", "updated_parsed"):
        tp = getattr(entry, k, None)
        if tp:
            try:
                return datetime(*tp[:6], tzinfo=ZoneInfo("UTC"))
            except Exception:
                pass
    return None


def summarize_html(html: str, max_len: int = 220) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


# =============================
# RSS / PICK ITEMS
# =============================

def fetch_feed(url: str):
    print(f"[RSS] GET {url}")
    try:
        r = SESSION.get(url, timeout=25)
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
        return parsed
    except Exception as e:
        print(f"[RSS] Feed failed: {url} ({e})")
        return None


def pick_items(feeds: list[str]):
    window_start = datetime.now(ZoneInfo("UTC")) - timedelta(hours=DIGEST_WINDOW_HOURS)

    raw_items = []
    for f in feeds:
        parsed = fetch_feed(f)
        if not parsed or not getattr(parsed, "entries", None):
            continue

        for e in parsed.entries:
            title = safe_text(getattr(e, "title", ""))
            link = safe_text(getattr(e, "link", ""))
            if not title or not link:
                continue

            if is_probably_not_news(title):
                continue

            dt = parse_entry_time(e)
            if dt and dt < window_start:
                continue

            summary = ""
            if getattr(e, "summary", None):
                summary = summarize_html(e.summary, max_len=220)
            elif getattr(e, "description", None):
                summary = summarize_html(e.description, max_len=220)

            source = get_source_domain(link)
            raw_items.append(
                {
                    "title": title,
                    "link": link,
                    "source": source,
                    "dt": dt.isoformat() if dt else "",
                    "summary": summary,
                }
            )

    # Dedup
    seen = set()
    items = []
    for it in raw_items:
        key = (normalize_title(it["title"]), it["link"])
        if key in seen:
            continue
        seen.add(key)
        items.append(it)

    # Sort newest first if dt exists
    def sort_key(x):
        if x["dt"]:
            try:
                return datetime.fromisoformat(x["dt"])
            except Exception:
                return datetime(1970, 1, 1, tzinfo=ZoneInfo("UTC"))
        return datetime(1970, 1, 1, tzinfo=ZoneInfo("UTC"))

    items.sort(key=sort_key, reverse=True)

    # Variety cap per source
    per_source = {}
    picked = []
    for it in items:
        c = per_source.get(it["source"], 0)
        if c >= DIGEST_MAX_PER_SOURCE:
            continue
        per_source[it["source"]] = c + 1
        picked.append(it)
        if len(picked) >= DIGEST_TOP_N:
            break

    return picked


# =============================
# YOUTUBE / ADILO
# =============================

def fetch_youtube_latest() -> tuple[str, str]:
    """
    Returns (title, url). We keep the URL as a plain line so Discord unfurls it.
    Filters out Shorts.
    """
    if not YOUTUBE_RSS_URL:
        return ("", "")

    try:
        r = SESSION.get(YOUTUBE_RSS_URL, timeout=25)
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
        for e in parsed.entries:
            title = safe_text(getattr(e, "title", ""))
            link = safe_text(getattr(e, "link", ""))
            if not link:
                continue
            if "/shorts/" in link or "#shorts" in title.lower():
                continue
            return (title, link)
    except Exception as ex:
        print(f"[YT] Failed to fetch RSS: {ex}")

    return ("", "")


def scrape_latest_adilo_watch_url() -> str:
    """
    Scrape the public page and pull the first /watch/ID or video?id=ID.
    If anything fails, fall back to the home hub.
    """
    try:
        print(f"[ADILO] SCRAPE {ADILO_PUBLIC_LATEST_PAGE}")
        r = SESSION.get(ADILO_PUBLIC_LATEST_PAGE, timeout=25)
        r.raise_for_status()
        html = r.text

        watch_ids = re.findall(r"/watch/([A-Za-z0-9_-]{6,})", html)
        vid_ids = re.findall(r"video\?id=([A-Za-z0-9_-]{6,})", html)

        candidate = ""
        if watch_ids:
            candidate = watch_ids[0]
        elif vid_ids:
            candidate = vid_ids[0]

        if candidate:
            return f"https://adilo.bigcommand.com/watch/{candidate}"
    except Exception as ex:
        print(f"[ADILO] SCRAPE failed: {ex}")

    print(f"[ADILO] Falling back: {ADILO_PUBLIC_HOME_PAGE}")
    return ADILO_PUBLIC_HOME_PAGE


# =============================
# DISCORD POSTING (MULTI-MESSAGE)
# =============================

def discord_post(content: str, embeds: list[dict] | None = None):
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL")

    payload = {"content": (content or "").strip()}
    if embeds:
        payload["embeds"] = embeds

    # Keep content within Discord 2000 char limit
    if len(payload["content"]) > 1990:
        payload["content"] = payload["content"][:1989] + "…"

    r = SESSION.post(DISCORD_WEBHOOK_URL, json=payload, timeout=25)
    if r.status_code >= 400:
        print(f"[DISCORD] HTTP {r.status_code} response: {r.text[:500]}")
        r.raise_for_status()


# =============================
# MAIN
# =============================

def main():
    if not guard_should_post_now():
        return

    state = load_state()
    today_key = now_local().strftime("%Y-%m-%d")

    if not DIGEST_FORCE_POST and state.get("last_posted_digest_date") == today_key:
        print(f"[DIGEST] Already posted today ({today_key}). Skipping.")
        return

    feeds_env = os.getenv("FEED_URLS", "").strip()
    if feeds_env:
        feeds = [x.strip() for x in feeds_env.split("|") if x.strip()]
    else:
        feeds = DEFAULT_FEEDS

    items = pick_items(feeds)
    if not items:
        print("[DIGEST] No items found in window. Exiting without posting.")
        return

    yt_title, yt_url = fetch_youtube_latest()
    adilo_url = scrape_latest_adilo_watch_url()

    # --- 1) Header post (short, never hits limit)
    today = now_local().strftime("%B %d, %Y")
    header_lines = [
        NEWSLETTER_TAGLINE,
        "",
        today,
        "",
        f"In Tonight’s Edition of {NEWSLETTER_NAME}…",
    ]
    for it in items[:3]:
        header_lines.append(f"► 🎮 {it['title']}")
    header_lines += ["", "Tonight’s Top Stories"]
    discord_post("\n".join(header_lines))

    # --- 2) One post per story, so the embed card appears under the right story
    for idx, it in enumerate(items, start=1):
        story_lines = [
            f"{idx}) {it['title']}",
        ]
        if it.get("summary"):
            story_lines.append(it["summary"])
        story_lines.append(f"Source: {it['source']}")
        story_lines.append(it["link"])  # plain URL line for unfurl

        embed = {
            "title": f"{idx}) {it['title']}",
            "url": it["link"],
            "description": (it.get("summary") or "")[:350],
            "footer": {"text": f"Source: {it['source']}"},
        }
        discord_post("\n".join(story_lines), embeds=[embed])

    # --- 3) Featured videos (separate short posts so they never get cut off)
    if yt_url:
        discord_post("▶️ YouTube (latest)\n" + yt_url)
    else:
        discord_post("▶️ YouTube (latest)\n(No YouTube video found.)")

    discord_post("📺 Adilo (latest)\n" + (adilo_url or ADILO_PUBLIC_HOME_PAGE))

    # --- 4) Sign-off (short)
    discord_post("—\nThat’s it for tonight’s Itty Bitty. 😄\nCatch the snackable breakdown on Itty Bitty Gaming News tomorrow.")

    state["last_posted_digest_date"] = today_key
    save_state(state)

    print("[DONE] Digest posted.")
    print(f"[DONE] YouTube: {yt_url or '(none)'}")
    print(f"[DONE] Featured Adilo video: {adilo_url}")


if __name__ == "__main__":
    main()
