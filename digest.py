import os
import re
import json
import textwrap
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

import requests
import feedparser
from bs4 import BeautifulSoup

# -----------------------------
# Config
# -----------------------------

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

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNews/Digest").strip()

YOUTUBE_RSS_URL = os.getenv("YOUTUBE_RSS_URL", "").strip()

ADILO_PUBLIC_LATEST_PAGE = os.getenv("ADILO_PUBLIC_LATEST_PAGE", "https://adilo.bigcommand.com/c/ittybittygamingnews/video").strip()
ADILO_PUBLIC_HOME_PAGE = os.getenv("ADILO_PUBLIC_HOME_PAGE", "https://adilo.bigcommand.com/c/ittybittygamingnews/home").strip()

DIGEST_FORCE_POST = os.getenv("DIGEST_FORCE_POST", "").strip().lower() in ("1", "true", "yes", "y")

# Guard scheduling
DIGEST_GUARD_TZ = os.getenv("DIGEST_GUARD_TZ", "America/Los_Angeles").strip()
DIGEST_GUARD_LOCAL_HOUR = int(os.getenv("DIGEST_GUARD_LOCAL_HOUR", "19").strip())
DIGEST_GUARD_LOCAL_MINUTE = int(os.getenv("DIGEST_GUARD_LOCAL_MINUTE", "0").strip())
DIGEST_GUARD_WINDOW_MINUTES = int(os.getenv("DIGEST_GUARD_WINDOW_MINUTES", "120").strip())

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})

STATE_PATH = "digest_state.json"


# -----------------------------
# Helpers
# -----------------------------

def now_local():
    return datetime.now(ZoneInfo(DIGEST_GUARD_TZ))

def guard_should_post_now() -> bool:
    if DIGEST_FORCE_POST:
        print("[GUARD] DIGEST_FORCE_POST enabled — bypassing time guard.")
        return True

    tz = ZoneInfo(DIGEST_GUARD_TZ)
    now = datetime.now(tz)
    target = now.replace(hour=DIGEST_GUARD_LOCAL_HOUR, minute=DIGEST_GUARD_LOCAL_MINUTE, second=0, microsecond=0)
    delta_min = abs((now - target).total_seconds()) / 60.0

    if delta_min <= DIGEST_GUARD_WINDOW_MINUTES:
        print(f"[GUARD] OK. Local now: {now.strftime('%Y-%m-%d %H:%M:%S %Z')} Delta={delta_min:.1f}min")
        return True

    print(f"[GUARD] Not within posting window. Local now: {now.strftime('%Y-%m-%d %H:%M:%S %Z')} Delta={delta_min:.1f}min")
    return False

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

def safe_text(s: str) -> str:
    return (s or "").strip()

def normalize_title(t: str) -> str:
    t = safe_text(t).lower()
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"[^a-z0-9 :'\-–—/().]", "", t)
    return t.strip()

def is_probably_not_news(title: str) -> bool:
    """
    Strong heuristic filters to avoid listicles / evergreen / guides / deals / opinions.
    (Matches your earlier "no Best PS5 controllers", "history of", "poll", etc.)
    """
    t = normalize_title(title)

    bad_starts = (
        "best ", "top ", "every ", "all ", "ranking", "ranked", "review:", "reviews:",
        "history of", "poll:", "poll ", "debate:", "opinion:", "op-ed:", "guide:", "how to",
        "deal:", "deals:", "sale:", "discount", "drops to", "now ", "coupon",
    )
    for p in bad_starts:
        if t.startswith(p):
            return True

    bad_contains = (
        "controller", "power bank", "prime day", "black friday", "cyber monday",
        "at woot", "deal", "discount", "best ps5", "best xbox", "best switch",
        "letters", "mailbox", "favourite", "favorite", "cosplay", "rhythm game goat",
        "walt disney world", "audio-animatronics", "olaf", "disney",
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
    # feedparser provides .published_parsed or .updated_parsed
    for k in ("published_parsed", "updated_parsed"):
        tp = getattr(entry, k, None)
        if tp:
            try:
                return datetime(*tp[:6], tzinfo=ZoneInfo("UTC"))
            except Exception:
                pass
    return None

def summarize_html(html: str, max_len: int = 220) -> str:
    html = html or ""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"

def fetch_feed(url: str):
    print(f"[RSS] GET {url}")
    try:
        # feedparser can fetch itself, but we fetch with requests for UA control.
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
                summary = summarize_html(e.summary, max_len=260)
            elif getattr(e, "description", None):
                summary = summarize_html(e.description, max_len=260)

            source = get_source_domain(link)
            raw_items.append({
                "title": title,
                "link": link,
                "source": source,
                "dt": dt.isoformat() if dt else "",
                "summary": summary,
            })

    # Dedup by normalized title + link
    seen_keys = set()
    items = []
    for it in raw_items:
        key = (normalize_title(it["title"]), it["link"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        items.append(it)

    # Sort newest first when timestamps exist, otherwise keep insertion order
    def sort_key(x):
        if x["dt"]:
            try:
                return datetime.fromisoformat(x["dt"])
            except Exception:
                return datetime(1970, 1, 1, tzinfo=ZoneInfo("UTC"))
        return datetime(1970, 1, 1, tzinfo=ZoneInfo("UTC"))

    items.sort(key=sort_key, reverse=True)

    # Enforce max per source for variety
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

def fetch_youtube_latest() -> tuple[str, str]:
    """
    Returns (title, url). Keeps URL plain so Discord will unfurl into a playable card.
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
            # filter shorts
            if "/shorts/" in link or "#shorts" in title.lower() or "shorts" == title.lower().strip():
                continue
            return (title, link)
    except Exception as ex:
        print(f"[YT] Failed to fetch RSS: {ex}")

    return ("", "")

def scrape_latest_adilo_watch_url() -> str:
    """
    Priority: return a watch URL that Discord can unfurl.
    We scrape the public 'video' page and extract the first found ID.
    """
    # Try latest page
    try:
        print(f"[ADILO] SCRAPE {ADILO_PUBLIC_LATEST_PAGE}")
        r = SESSION.get(ADILO_PUBLIC_LATEST_PAGE, timeout=25)
        r.raise_for_status()
        html = r.text

        # Most reliable patterns we’ve seen in Adilo pages:
        # - /watch/ID
        # - video?id=ID
        watch_ids = re.findall(r"/watch/([A-Za-z0-9_-]{6,})", html)
        vid_ids = re.findall(r"video\?id=([A-Za-z0-9_-]{6,})", html)

        candidate_id = ""
        if watch_ids:
            candidate_id = watch_ids[0]
        elif vid_ids:
            candidate_id = vid_ids[0]

        if candidate_id:
            # Use /watch/ID for fastest unfurl card
            return f"https://adilo.bigcommand.com/watch/{candidate_id}"

        # If no IDs found, fall through
    except Exception as ex:
        print(f"[ADILO] SCRAPE failed: {ex}")

    print(f"[ADILO] Falling back: {ADILO_PUBLIC_HOME_PAGE}")
    return ADILO_PUBLIC_HOME_PAGE

def build_message(items, yt_title, yt_url, adilo_url) -> tuple[str, list[dict]]:
    today = now_local().strftime("%B %d, %Y")

    # Header
    lines = []
    lines.append(f"{NEWSLETTER_TAGLINE}")
    lines.append("")
    lines.append(f"{today}")
    lines.append("")
    lines.append(f"In Tonight’s Edition of {NEWSLETTER_NAME}…")

    # Quick bullets (titles)
    for it in items[:3]:
        lines.append(f"► 🎮 {it['title']}")
    lines.append("")

    # Top stories section (NO "cards below" wording)
    lines.append("Tonight’s Top Stories")
    lines.append("")

    # Numbered items with URL directly under the story (your request)
    for idx, it in enumerate(items, start=1):
        lines.append(f"{idx}) {it['title']}")
        if it["summary"]:
            lines.append(it["summary"])
        lines.append(f"Source: {it['source']}")
        lines.append(it["link"])  # plain URL line helps Discord unfurl
        lines.append("")

    # Featured video section: YouTube first, then Adilo (your preference earlier)
    if yt_url or adilo_url:
        lines.append("▶️ YouTube (latest)")
        if yt_url:
            lines.append(yt_url)  # plain URL line for Discord player card
        else:
            lines.append("(No YouTube video found.)")
        lines.append("")
        lines.append("📺 Adilo (latest)")
        lines.append(adilo_url or ADILO_PUBLIC_HOME_PAGE)
        lines.append("")

    # Clean sign-off (no “signal” corny line)
    lines.append("—")
    lines.append("That’s it for tonight’s Itty Bitty. 😄")
    lines.append("Catch the snackable breakdown on Itty Bitty Gaming News tomorrow.")

    content = "\n".join(lines).strip()

    # Embeds for “cards” under the message: 1 embed per story
    embeds = []
    for idx, it in enumerate(items, start=1):
        emb = {
            "title": f"{idx}) {it['title']}",
            "url": it["link"],
            "description": (it["summary"] or "")[:350],
            "footer": {"text": f"Source: {it['source']}"},
        }
        embeds.append(emb)

    # Discord hard limits: content 2000 chars; embeds max 10
    if len(content) > 1950:
        content = content[:1949] + "…"

    return content, embeds[:10]

def discord_post(content: str, embeds: list[dict]):
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL")

    payload = {"content": content}
    if embeds:
        payload["embeds"] = embeds

    r = SESSION.post(DISCORD_WEBHOOK_URL, json=payload, timeout=25)
    if r.status_code >= 400:
        print(f"[DISCORD] HTTP {r.status_code} response: {r.text[:500]}")
        r.raise_for_status()

def main():
    if not guard_should_post_now():
        # exit success to avoid failing Actions
        return

    # Prevent double-posting the same day on scheduled runs (manual runs can override by forcing post)
    state = load_state()
    today_key = now_local().strftime("%Y-%m-%d")

    if not DIGEST_FORCE_POST and state.get("last_posted_digest_date") == today_key:
        print(f"[DIGEST] Already posted today ({today_key}). Skipping.")
        return

    # Feeds from env var (optional)
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

    content, embeds = build_message(items, yt_title, yt_url, adilo_url)
    discord_post(content, embeds)

    state["last_posted_digest_date"] = today_key
    save_state(state)

    print("[DONE] Digest posted.")
    print(f"[DONE] YouTube: {yt_url or '(none)'}")
    print(f"[DONE] Featured Adilo video: {adilo_url}")

if __name__ == "__main__":
    main()
