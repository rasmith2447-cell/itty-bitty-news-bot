#!/usr/bin/env python3
import os
import re
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional, Tuple
from urllib.parse import urlparse, parse_qs

import requests
import feedparser
from bs4 import BeautifulSoup

# ----------------------------
# Config + Constants
# ----------------------------
DEFAULT_FEEDS = [
    "https://www.bluesnews.com/news/news_1_0.rdf",
    "https://www.ign.com/rss/v2/articles/feed?categories=games",
    "https://www.gamespot.com/feeds/mashup/",
    "https://gamerant.com/feed",
    "https://www.polygon.com/rss/index.xml",
    "https://www.videogameschronicle.com/feed/",
    "https://www.gematsu.com/feed",
]

UA_DEFAULT = "IttyBittyGamingNews/Digest"
REQ_TIMEOUT = 25

DISCORD_CONTENT_LIMIT = 2000
DISCORD_EMBEDS_LIMIT = 10  # webhook embeds limit
DISCORD_EMBED_DESC_LIMIT = 4096
DISCORD_EMBED_TITLE_LIMIT = 256

# ----------------------------
# Data Structures
# ----------------------------
@dataclass
class Story:
    title: str
    url: str
    source: str
    published: Optional[datetime]
    summary: str
    tags: List[str]


# ----------------------------
# Helpers
# ----------------------------
def getenv(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None else str(v)


def now_local(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def safe_trunc(s: str, n: int) -> str:
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= n else s[: max(0, n - 1)].rstrip() + "…"


def normalize_url(u: str) -> str:
    if not u:
        return ""
    return u.strip()


def domain_from_url(u: str) -> str:
    try:
        return urlparse(u).netloc.replace("www.", "").lower()
    except Exception:
        return ""


def load_cache(path: str) -> Dict:
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(path: str, data: Dict) -> None:
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
    except Exception as e:
        print(f"[CACHE] Failed to save cache: {e}")


# ----------------------------
# Guard: posting window + once/day
# ----------------------------
def guard_should_post_now() -> bool:
    # Manual override
    if getenv("DIGEST_FORCE_POST", "").strip().lower() in ("1", "true", "yes", "y"):
        print("[GUARD] DIGEST_FORCE_POST enabled — bypassing time guard.")
        return True

    tz_name = getenv("DIGEST_GUARD_TZ", "America/Los_Angeles").strip()
    target_hour = int(getenv("DIGEST_GUARD_LOCAL_HOUR", "19").strip())
    target_minute = int(getenv("DIGEST_GUARD_LOCAL_MINUTE", "0").strip())
    window_minutes = int(getenv("DIGEST_GUARD_WINDOW_MINUTES", "30").strip())

    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)

    target_today = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
    candidates = [target_today - timedelta(days=1), target_today, target_today + timedelta(days=1)]
    closest = min(candidates, key=lambda t: abs((now - t).total_seconds()))
    delta_min = abs((now - closest).total_seconds()) / 60.0

    if delta_min <= window_minutes:
        print(
            f"[GUARD] OK. Local now: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            f"Closest target: {closest.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            f"Delta={delta_min:.1f}min <= {window_minutes}min"
        )
        return True

    print(
        f"[GUARD] Not within posting window. Local now: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
        f"Closest target: {closest.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
        f"Delta={delta_min:.1f}min > {window_minutes}min. Exiting without posting."
    )
    return False


def guard_once_per_day(cache: Dict) -> bool:
    if getenv("DIGEST_FORCE_POST", "").strip().lower() in ("1", "true", "yes", "y"):
        return True

    if getenv("DIGEST_POST_ONCE_PER_DAY", "").strip().lower() not in ("1", "true", "yes", "y"):
        return True

    tz_name = getenv("DIGEST_GUARD_TZ", "America/Los_Angeles").strip()
    today = now_local(tz_name).strftime("%Y-%m-%d")
    posted = cache.get("posted_dates", [])
    if today in posted:
        print(f"[GUARD] Already posted for {today}. Exiting without posting.")
        return False
    return True


def mark_posted_today(cache: Dict) -> None:
    tz_name = getenv("DIGEST_GUARD_TZ", "America/Los_Angeles").strip()
    today = now_local(tz_name).strftime("%Y-%m-%d")
    cache.setdefault("posted_dates", [])
    if today not in cache["posted_dates"]:
        cache["posted_dates"].append(today)
        print(f"[CACHE] Marked posted for {today}.")


# ----------------------------
# Fetch RSS stories
# ----------------------------
def parse_published(entry) -> Optional[datetime]:
    # feedparser sets .published_parsed or .updated_parsed
    for attr in ("published_parsed", "updated_parsed"):
        st = getattr(entry, attr, None)
        if st:
            try:
                return datetime.fromtimestamp(time.mktime(st), tz=ZoneInfo("UTC"))
            except Exception:
                pass
    return None


def extract_tags(entry) -> List[str]:
    tags = []
    for t in getattr(entry, "tags", []) or []:
        term = getattr(t, "term", None)
        if term:
            tags.append(str(term).strip())
    # de-dupe, preserve order
    out = []
    seen = set()
    for x in tags:
        key = x.lower()
        if key not in seen:
            seen.add(key)
            out.append(x)
    return out


def clean_summary(html_or_text: str) -> str:
    if not html_or_text:
        return ""
    # Strip HTML and collapse whitespace
    soup = BeautifulSoup(str(html_or_text), "html.parser")
    txt = soup.get_text(" ", strip=True)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def load_feed_urls() -> List[str]:
    raw = getenv("FEED_URLS", "").strip()
    if raw:
        # support comma or newline separated
        parts = []
        for chunk in re.split(r"[\n,]+", raw):
            u = chunk.strip()
            if u:
                parts.append(u)
        return parts or DEFAULT_FEEDS
    return DEFAULT_FEEDS


def fetch_stories() -> List[Story]:
    ua = getenv("USER_AGENT", UA_DEFAULT)
    headers = {"User-Agent": ua}

    urls = load_feed_urls()
    window_hours = int(getenv("DIGEST_WINDOW_HOURS", "24").strip())
    cutoff = datetime.now(ZoneInfo("UTC")) - timedelta(hours=window_hours)

    all_stories: List[Story] = []
    for url in urls:
        try:
            print(f"[RSS] GET {url}")
            # feedparser can fetch itself, but we supply our own request for UA control
            r = requests.get(url, headers=headers, timeout=REQ_TIMEOUT)
            r.raise_for_status()
            feed = feedparser.parse(r.content)

            # Even if bozo=1, entries often still parse fine; don't hard-fail.
            if getattr(feed, "bozo", 0) == 1:
                bozo_exc = getattr(feed, "bozo_exception", None)
                print(f"[RSS] bozo=1 for {url}: {bozo_exc}")

            for e in feed.entries or []:
                link = normalize_url(getattr(e, "link", "") or "")
                title = (getattr(e, "title", "") or "").strip()
                if not link or not title:
                    continue
                pub = parse_published(e)
                if pub and pub < cutoff:
                    continue

                source = domain_from_url(link) or domain_from_url(url) or "source"
                summary = clean_summary(getattr(e, "summary", "") or getattr(e, "description", "") or "")
                tags = extract_tags(e)

                all_stories.append(
                    Story(
                        title=title,
                        url=link,
                        source=source,
                        published=pub,
                        summary=summary,
                        tags=tags,
                    )
                )
        except Exception as ex:
            print(f"[RSS] Feed failed: {url} ({ex})")

    # Filter by cutoff again if entries had no pub date
    recent = []
    for s in all_stories:
        if s.published is None:
            # keep undated items (some feeds omit date); they will be sorted later
            recent.append(s)
        else:
            recent.append(s)

    print(f"[DIGEST] After {window_hours}h window filter: {len(recent)} item(s)")
    return recent


def pick_top_stories(stories: List[Story]) -> List[Story]:
    top_n = int(getenv("DIGEST_TOP_N", "5").strip())
    max_per_source = int(getenv("DIGEST_MAX_PER_SOURCE", "1").strip())

    # Sort: newest first; undated go last
    def sort_key(s: Story):
        if s.published is None:
            return (0, datetime(1970, 1, 1, tzinfo=ZoneInfo("UTC")))
        return (1, s.published)

    stories_sorted = sorted(stories, key=sort_key, reverse=True)

    picked: List[Story] = []
    per_source: Dict[str, int] = {}
    seen_urls = set()

    for s in stories_sorted:
        if len(picked) >= top_n:
            break
        if s.url in seen_urls:
            continue
        seen_urls.add(s.url)

        src = s.source or "source"
        per_source.setdefault(src, 0)
        if per_source[src] >= max_per_source:
            continue

        per_source[src] += 1
        picked.append(s)

    return picked


# ----------------------------
# YouTube latest via RSS
# ----------------------------
def youtube_latest() -> Optional[Tuple[str, str]]:
    rss = getenv("YOUTUBE_RSS_URL", "").strip()
    if not rss:
        # optional: build from channel id
        cid = getenv("YOUTUBE_CHANNEL_ID", "").strip()
        if cid:
            rss = f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
    if not rss:
        return None

    ua = getenv("USER_AGENT", UA_DEFAULT)
    headers = {"User-Agent": ua}

    try:
        print(f"[YT] Fetch RSS: {rss}")
        r = requests.get(rss, headers=headers, timeout=REQ_TIMEOUT)
        r.raise_for_status()

        # Simple parse: find first <entry>, then <yt:videoId> and <title>
        # We avoid extra deps; regex is enough for this feed format.
        text = r.text

        # Grab first entry block
        m_entry = re.search(r"<entry\b.*?</entry>", text, flags=re.DOTALL)
        if not m_entry:
            return None
        entry = m_entry.group(0)

        m_vid = re.search(r"<yt:videoId>([^<]+)</yt:videoId>", entry)
        m_title = re.search(r"<title>([^<]+)</title>", entry)
        if not m_vid:
            return None

        vid = m_vid.group(1).strip()
        title = (m_title.group(1).strip() if m_title else "Latest video").strip()

        # Filter shorts by title heuristic (best we can do via RSS)
        if getenv("YOUTUBE_FILTER_SHORTS", "true").strip().lower() in ("1", "true", "yes", "y"):
            t = title.lower()
            if "#shorts" in t or "shorts" in t or "short" in t and "shortage" not in t:
                # If first is a short, try next entry
                entries = re.findall(r"<entry\b.*?</entry>", text, flags=re.DOTALL)
                for ent in entries[1:10]:
                    m_vid2 = re.search(r"<yt:videoId>([^<]+)</yt:videoId>", ent)
                    m_title2 = re.search(r"<title>([^<]+)</title>", ent)
                    if not m_vid2:
                        continue
                    vid2 = m_vid2.group(1).strip()
                    title2 = (m_title2.group(1).strip() if m_title2 else "Latest video").strip()
                    t2 = title2.lower()
                    if "#shorts" in t2 or "shorts" in t2 or ("short" in t2 and "shortage" not in t2):
                        continue
                    vid, title = vid2, title2
                    break

        url = f"https://www.youtube.com/watch?v={vid}"
        return (url, title)
    except Exception as ex:
        print(f"[YT] Failed to fetch RSS: {ex}")
        return None


# ----------------------------
# Adilo latest: API (optional) + scrape + cache last-good
# ----------------------------
def adilo_latest_via_api() -> Optional[str]:
    pub = getenv("ADILO_PUBLIC_KEY", "").strip()
    sec = getenv("ADILO_SECRET_KEY", "").strip()
    pid = getenv("ADILO_PROJECT_ID", "").strip()
    if not (pub and sec and pid):
        print("[ADILO] API not attempted (missing ADILO_PROJECT_ID / ADILO_PUBLIC_KEY / ADILO_SECRET_KEY).")
        return None

    base = "https://adilo-api.bigcommand.com/v1"
    url = f"{base}/projects/{pid}/files?From=1&To=50"
    try:
        # NOTE: I don't know Adilo's exact auth header scheme; this mirrors what *often* works:
        # If your API still returns 401, scrape will take over.
        headers = {
            "User-Agent": getenv("USER_AGENT", UA_DEFAULT),
            "Accept": "application/json",
            "X-Public-Key": pub,
            "X-Secret-Key": sec,
        }
        r = requests.get(url, headers=headers, timeout=REQ_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        payload = data.get("payload", [])
        # If the API returns newest first, first item is latest. If not, we could meta-probe, but keep simple.
        if not payload:
            return None

        file_id = payload[0].get("id") or ""
        if not file_id:
            return None
        return f"https://adilo.bigcommand.com/watch/{file_id}"
    except Exception as ex:
        print(f"[ADILO] API failed: {ex}")
        return None


def extract_adilo_ids_from_html(html: str) -> List[str]:
    if not html:
        return []

    ids = []

    # Patterns we have seen / likely:
    # - /watch/<id>
    # - video?id=<id>
    # - stage/videos/<id>
    patterns = [
        r"/watch/([A-Za-z0-9_-]{6,})",
        r"video\?id=([A-Za-z0-9_-]{6,})",
        r"/stage/videos/([A-Za-z0-9_-]{6,})",
        r'"id"\s*:\s*"([A-Za-z0-9_-]{6,})"',
    ]

    for pat in patterns:
        for m in re.findall(pat, html):
            ids.append(m)

    # De-dupe preserve order
    out = []
    seen = set()
    for x in ids:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def scrape_url_text(url: str, headers: Dict[str, str], timeout: int) -> Optional[str]:
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as ex:
        print(f"[ADILO] SCRAPE failed: {ex}")
        return None


def adilo_latest_via_scrape(cache: Dict) -> Optional[str]:
    latest_page = getenv("ADILO_PUBLIC_LATEST_PAGE", "https://adilo.bigcommand.com/c/ittybittygamingnews/video").strip()
    home_page = getenv("ADILO_PUBLIC_HOME_PAGE", "https://adilo.bigcommand.com/c/ittybittygamingnews/home").strip()

    ua = getenv("USER_AGENT", UA_DEFAULT)
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    cb = int(time.time() * 1000)
    candidates = [
        latest_page,
        f"{latest_page}?cb={cb}",
        f"{latest_page}/?cb={cb}",
        f"{latest_page}?video=latest&cb={cb}",
        f"{latest_page}?id=&cb={cb}",  # sometimes redirects or renders latest
    ]

    for u in candidates:
        print(f"[ADILO] SCRAPE attempt=1 timeout={REQ_TIMEOUT} url={u}")
        text = scrape_url_text(u, headers, REQ_TIMEOUT)
        if not text:
            continue

        ids = extract_adilo_ids_from_html(text)
        if ids:
            # Best guess: first ID tends to be newest on these pages when it works
            vid = ids[0]
            watch = f"https://adilo.bigcommand.com/watch/{vid}"
            print(f"[ADILO] Found candidate id={vid} -> {watch}")
            cache["last_good_adilo_watch_url"] = watch
            return watch

        # Also try to parse canonical/og:url for a direct video URL
        soup = BeautifulSoup(text, "html.parser")
        og = soup.find("meta", property="og:url")
        if og and og.get("content"):
            ogu = og["content"].strip()
            # If it contains a video id, convert to watch URL
            q = parse_qs(urlparse(ogu).query)
            if "id" in q and q["id"]:
                vid = q["id"][0]
                watch = f"https://adilo.bigcommand.com/watch/{vid}"
                print(f"[ADILO] Found og:url id={vid} -> {watch}")
                cache["last_good_adilo_watch_url"] = watch
                return watch
            m = re.search(r"/watch/([A-Za-z0-9_-]{6,})", ogu)
            if m:
                vid = m.group(1)
                watch = f"https://adilo.bigcommand.com/watch/{vid}"
                print(f"[ADILO] Found og:url watch id={vid} -> {watch}")
                cache["last_good_adilo_watch_url"] = watch
                return watch

    # If scrape failed, use last-good from cache before falling back to home
    last_good = cache.get("last_good_adilo_watch_url")
    if last_good:
        print(f"[ADILO] Using cached last-good Adilo URL: {last_good}")
        return last_good

    print(f"[ADILO] Falling back: {home_page}")
    return home_page


def adilo_latest(cache: Dict) -> str:
    # Prefer API if it works; else scrape; always avoid hard-locking IDs.
    api_url = adilo_latest_via_api()
    if api_url:
        cache["last_good_adilo_watch_url"] = api_url
        return api_url
    return adilo_latest_via_scrape(cache) or getenv("ADILO_PUBLIC_HOME_PAGE", "").strip()


# ----------------------------
# Discord webhook posting
# ----------------------------
def discord_webhook_url() -> str:
    u = getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not u:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL")
    return u


def post_webhook(content: str = "", embeds: Optional[List[Dict]] = None) -> None:
    url = discord_webhook_url()
    payload: Dict = {}
    if content is not None:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds[:DISCORD_EMBEDS_LIMIT]

    r = requests.post(url, json=payload, timeout=REQ_TIMEOUT)
    r.raise_for_status()


def build_story_embed(idx: int, story: Story) -> Dict:
    title = safe_trunc(f"{idx}) {story.title}", DISCORD_EMBED_TITLE_LIMIT)
    desc_parts = []

    if story.summary:
        desc_parts.append(safe_trunc(story.summary, 320))

    if story.tags:
        # show up to 6 tags
        shown = story.tags[:6]
        desc_parts.append("**Tags:** " + ", ".join(shown))

    # Put source + link in embed (this is what makes “cards under story” work)
    desc_parts.append(f"**Source:** {story.source}")

    desc = "\n".join([p for p in desc_parts if p]).strip()
    desc = safe_trunc(desc, DISCORD_EMBED_DESC_LIMIT)

    embed = {
        "title": title,
        "url": story.url,  # clickable title -> story
        "description": desc,
    }

    # If we have published time, show it
    if story.published:
        # Discord expects ISO 8601
        embed["timestamp"] = story.published.astimezone(ZoneInfo("UTC")).isoformat()

    return embed


def build_digest_text(top_titles: List[str]) -> str:
    tz_name = getenv("DIGEST_GUARD_TZ", "America/Los_Angeles").strip()
    today = now_local(tz_name).strftime("%B %d, %Y")

    name = getenv("NEWSLETTER_NAME", "Itty Bitty Gaming News").strip()
    tagline = getenv("NEWSLETTER_TAGLINE", "Snackable daily gaming news — five days a week.").strip()

    lines = []
    if tagline:
        lines.append(tagline)
        lines.append("")
    lines.append(today)
    lines.append("")
    lines.append(f"In Tonight’s Edition of {name}…")
    for t in top_titles[:3]:
        lines.append(f"► 🎮 {t}")
    lines.append("")
    lines.append("Tonight’s Top Stories")
    # NOTE: story embeds will appear directly under this message
    return "\n".join(lines).strip()


def maybe_export_digest(content: str, path: str) -> None:
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[EXPORT] Wrote {path}")
    except Exception as ex:
        print(f"[EXPORT] Failed to write {path}: {ex}")


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    cache_file = getenv("DIGEST_CACHE_FILE", ".digest_cache.json").strip()
    cache = load_cache(cache_file)

    if not guard_should_post_now():
        # Exit success so Actions doesn't show failure
        return

    if not guard_once_per_day(cache):
        return

    stories = fetch_stories()
    if not stories:
        print("[DIGEST] No feed items fetched. Exiting without posting.")
        return

    top = pick_top_stories(stories)
    if not top:
        print("[DIGEST] No items selected. Exiting without posting.")
        return

    # Build digest message + story embeds
    top_titles = [s.title for s in top]
    digest_text = build_digest_text(top_titles)
    embeds = [build_story_embed(i + 1, s) for i, s in enumerate(top)]

    export_path = getenv("DIGEST_EXPORT_FILE", "").strip()
    if export_path:
        maybe_export_digest(digest_text, export_path)

    # 1) Post digest message (text + story embeds)
    post_webhook(content=digest_text, embeds=embeds)

    # 2) Post Adilo standalone URL (for best chance at unfurl/card)
    adilo_url = adilo_latest(cache)
    # Avoid posting pure hub/home if we can
    if adilo_url and "/home" not in adilo_url:
        post_webhook(content=adilo_url, embeds=None)
    else:
        # If it's home, only post if there's no cached last-good
        last_good = cache.get("last_good_adilo_watch_url")
        if last_good and "/home" not in last_good:
            post_webhook(content=last_good, embeds=None)
            adilo_url = last_good
        else:
            # Don't spam home; skip
            print(f"[ADILO] Using fallback/no-video URL. Not posting standalone: {adilo_url}")

    # 3) Post YouTube standalone URL LAST (per your preference)
    yt = youtube_latest()
    yt_url = yt[0] if yt else ""
    if yt_url:
        post_webhook(content=yt_url, embeds=None)

    # Mark posted
    mark_posted_today(cache)
    save_cache(cache_file, cache)

    print("[DONE] Digest posted.")
    if yt_url:
        print(f"[DONE] YouTube: {yt_url}")
    if adilo_url:
        print(f"[DONE] Featured Adilo video: {adilo_url}")


if __name__ == "__main__":
    main()
