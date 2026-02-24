import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Tuple, Optional, Any
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from zoneinfo import ZoneInfo


# ----------------------------
# CONFIG
# ----------------------------

FEEDS = [
    {"name": "IGN", "url": "http://feeds.ign.com/ign/all"},
    {"name": "GameSpot", "url": "http://www.gamespot.com/feeds/mashup/"},
    {"name": "Blue's News", "url": "https://www.bluesnews.com/news/news_1_0.rdf"},
    {"name": "VGC", "url": "https://www.videogameschronicle.com/category/news/feed/"},
    {"name": "Gematsu", "url": "https://www.gematsu.com/feed"},
    {"name": "Polygon", "url": "https://www.polygon.com/rss/news/index.xml"},
    {"name": "Nintendo Life", "url": "https://www.nintendolife.com/feeds/latest"},
    {"name": "PC Gamer", "url": "https://www.pcgamer.com/rss"},
]

SOURCE_PRIORITY = [
    "IGN", "GameSpot", "VGC", "Gematsu",
    "Polygon", "Nintendo Life", "PC Gamer", "Blue's News",
]

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNews/Digest").strip()

WINDOW_HOURS = int(os.getenv("DIGEST_WINDOW_HOURS", "24"))
TOP_N = int(os.getenv("DIGEST_TOP_N", "5"))
MAX_PER_SOURCE = int(os.getenv("DIGEST_MAX_PER_SOURCE", "1"))

NEWSLETTER_NAME = os.getenv("NEWSLETTER_NAME", "Itty Bitty Gaming News").strip()
NEWSLETTER_TAGLINE = os.getenv("NEWSLETTER_TAGLINE", "Snackable daily gaming news — five days a week.").strip()

FEATURED_VIDEO_TITLE = os.getenv("FEATURED_VIDEO_TITLE", "Watch today’s Itty Bitty Gaming News").strip()
FEATURED_VIDEO_FALLBACK_URL = os.getenv(
    "FEATURED_VIDEO_FALLBACK_URL",
    "https://adilo.bigcommand.com/c/ittybittygamingnews/home"
).strip()
FEATURED_VIDEO_FALLBACK_ID = os.getenv("FEATURED_VIDEO_FALLBACK_ID", "").strip()

# If set, we will ALWAYS use it (this is what can “stick” to an older video)
FEATURED_VIDEO_FORCE_ID = os.getenv("FEATURED_VIDEO_FORCE_ID", "").strip()

# YouTube featured (same episode)
YOUTUBE_FEATURED_URL = os.getenv("YOUTUBE_FEATURED_URL", "").strip()
YOUTUBE_FEATURED_TITLE = os.getenv("YOUTUBE_FEATURED_TITLE", "").strip() or "Watch on YouTube"

# Adilo API
ADILO_PUBLIC_KEY = os.getenv("ADILO_PUBLIC_KEY", "").strip()
ADILO_SECRET_KEY = os.getenv("ADILO_SECRET_KEY", "").strip()
ADILO_PROJECT_ID = os.getenv("ADILO_PROJECT_ID", "").strip()
ADILO_API_BASE = "https://adilo-api.bigcommand.com/v1"

# Public pages to scrape if API seems stale
ADILO_PUBLIC_LATEST_PAGE = os.getenv(
    "ADILO_PUBLIC_LATEST_PAGE",
    "https://adilo.bigcommand.com/c/ittybittygamingnews/video"
).strip()
ADILO_PUBLIC_HOME_PAGE = os.getenv(
    "ADILO_PUBLIC_HOME_PAGE",
    "https://adilo.bigcommand.com/c/ittybittygamingnews/home"
).strip()

# DST-safe schedule guard
DIGEST_GUARD_TZ = os.getenv("DIGEST_GUARD_TZ", "America/Los_Angeles").strip()
DIGEST_GUARD_LOCAL_HOUR = int(os.getenv("DIGEST_GUARD_LOCAL_HOUR", "19"))  # 7pm PT
DIGEST_GUARD_WINDOW_MINUTES = int(os.getenv("DIGEST_GUARD_WINDOW_MINUTES", "15"))

# Discord limits
DISCORD_SAFE_CONTENT = 1850
EMBED_DESC_LIMIT = 900

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_reader", "utm_referrer",
    "gclid", "fbclid", "mc_cid", "mc_eid", "ref", "source"
}


# ----------------------------
# FILTERS (news-only)
# ----------------------------

GAME_TERMS = [
    "video game", "videogame", "gaming",
    "xbox", "playstation", "ps5", "ps4", "nintendo", "switch",
    "steam", "epic games", "gog", "game pass",
    "pc gaming", "console", "handheld",
    "dlc", "expansion", "season", "battle pass",
    "patch", "update", "hotfix",
    "release date", "launch", "early access", "beta", "alpha", "demo",
    "studio", "developer", "publisher",
    "esports", "tournament",
    "playstation studios", "ubisoft", "ea", "activision", "blizzard", "bethesda", "capcom",
    "bandai namco", "square enix", "sega", "take-two", "2k", "rockstar", "valve",
]

ADJACENT_TERMS = [
    "gpu", "graphics card", "nvidia", "amd", "intel", "driver", "dlss", "fsr",
    "steam deck", "rog ally", "handheld pc",
    "unity", "unreal engine", "unreal",
    "discord", "twitch", "youtube gaming", "streaming",
    "vr", "virtual reality", "meta quest",
]

LISTICLE_GUIDE_BLOCK = [
    "best ", "top ", "ranked", "ranking", "tier list",
    "everything you need to know", "explained",
    "review", "preview", "impressions",
    "guide", "walkthrough", "tips", "tricks",
]

EVERGREEN_BLOCK = [
    "history of", "timeline", "retrospective", "complete history",
    "recap", "ending explained", "lore", "beginner's guide",
    "what we know so far",
]

COMMUNITY_OPINION_BLOCK = [
    "opinion:", "editorial:", "commentary", "column:", "feature:",
    "roundtable", "debate:", "discussion:", "hot take",
    "poll:", "quiz:", "mailbox:", "mailbag", "letters", "community",
    "favorite", "favourite",
]

DEALS_BLOCK = [
    "deal", "deals", "sale", "discount", "save ",
    "coupon", "promo code", "price drop", "drops to", "lowest price",
    "now %", "% off", "limited-time",
    "for just $", "for only $",
    "woot", "amazon", "best buy", "walmart", "target", "newegg",
    "power bank", "mah", "charger", "charging", "usb-c",
]

RUMOR_BLOCK = [
    "rumor", "rumour", "leak", "leaked", "leaks",
    "speculation", "speculate", "reportedly", "allegedly",
    "unconfirmed", "according to sources", "insider",
]

NON_GAMING_ENTERTAINMENT_BLOCK = [
    "walt disney world", "disney world", "disneyland", "disney's hollywood studios",
    "audio-animatronics", "animation academy", "olaf", "frozen",
    "theme park", "theme-park", "ride", "attraction",
    "movie", "film", "tv", "television", "series", "episode",
    "netflix", "hulu", "disney+", "paramount", "max", "hbo",
    "comic", "comics", "dc ", "marvel", "green arrow", "catwoman",
]

NEWS_HINTS = [
    "announced", "announcement", "revealed", "reveal",
    "launch", "release date", "out now", "available now", "live now",
    "delay", "delayed", "layoff", "layoffs",
    "shutdown", "closed", "acquisition", "acquired", "merger",
    "lawsuit", "sued",
    "patch", "hotfix", "update",
    "retire", "retirement", "steps down", "stepping down", "resigns", "resignation",
]


# ----------------------------
# UTIL
# ----------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def local_now() -> datetime:
    return datetime.now(ZoneInfo(DIGEST_GUARD_TZ))

def normalize_url(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
        query = [(k, v) for (k, v) in parse_qsl(parsed.query, keep_blank_values=True)
                 if k.lower() not in TRACKING_PARAMS]
        parsed = parsed._replace(query=urlencode(query, doseq=True), fragment="")
        parsed = parsed._replace(netloc=parsed.netloc.lower())
        return urlunparse(parsed).strip()
    except Exception:
        return url.strip()

def strip_html(text: str) -> str:
    if text is None:
        return ""
    s = str(text)
    soup = BeautifulSoup(f"<div>{s}</div>", "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()

def shorten(text: str, max_len: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"

def safe_parse_date(entry) -> datetime:
    if getattr(entry, "published_parsed", None):
        return datetime.fromtimestamp(time.mktime(entry.published_parsed), tz=timezone.utc)
    if getattr(entry, "updated_parsed", None):
        return datetime.fromtimestamp(time.mktime(entry.updated_parsed), tz=timezone.utc)
    for key in ["published", "updated", "created", "date"]:
        val = getattr(entry, key, None)
        if val:
            try:
                dt = dateparser.parse(val)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                pass
    return utcnow()

def contains_any(hay: str, terms: List[str]) -> bool:
    h = hay.lower()
    return any(t.lower() in h for t in terms)

def has_money_signals(text: str) -> bool:
    return bool(re.search(r"(\$\d)|(\d+\s*%(\s*off)?)", text, flags=re.IGNORECASE))

def looks_like_a_specific_game_title(title: str) -> bool:
    t = title.strip()
    if len(t) < 12:
        return False
    return (":" in t) or (" - " in t)

def game_or_adjacent(title: str, summary: str) -> bool:
    hay = f"{title} {summary}".lower()
    if contains_any(hay, NON_GAMING_ENTERTAINMENT_BLOCK):
        return False
    if contains_any(hay, GAME_TERMS) or contains_any(hay, ADJACENT_TERMS):
        return True
    if looks_like_a_specific_game_title(title):
        return True
    return False

def block_reason(title: str, summary: str) -> str:
    hay = f"{title} {summary}".lower()
    if contains_any(hay, NON_GAMING_ENTERTAINMENT_BLOCK):
        return "NON_GAMING_ENTERTAINMENT"
    if not game_or_adjacent(title, summary):
        return "NOT_GAME_OR_ADJACENT"
    if contains_any(hay, COMMUNITY_OPINION_BLOCK):
        return "COMMUNITY/OPINION"
    if contains_any(hay, LISTICLE_GUIDE_BLOCK):
        return "LISTICLE/GUIDE/REVIEW"
    if contains_any(hay, EVERGREEN_BLOCK):
        return "EVERGREEN/SEO_REFRESH"
    if contains_any(hay, DEALS_BLOCK) or has_money_signals(hay):
        return "DEALS/SHOPPING"
    if contains_any(hay, RUMOR_BLOCK):
        return "RUMOR/SPECULATION"
    return ""

def normalize_title_key(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", title.lower())).strip()

def sentence_split(text: str) -> List[str]:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return []
    parts = re.split(r"(?<=[.!?])\s+", t)
    return [p.strip() for p in parts if p.strip()]

def build_story_summary(raw_summary: str, source: str, featured: bool = False) -> str:
    sents = sentence_split(raw_summary)
    if not sents:
        return f"{source} posted an update — hit the source link for full details."
    target = 5 if featured else 3
    out = []
    for s in sents[:target]:
        s = re.sub(r"^Read more.*$", "", s, flags=re.IGNORECASE).strip()
        if len(s) < 20:
            continue
        out.append(s)
    if not out:
        return shorten(raw_summary, 520 if featured else 360)
    return shorten(" ".join(out), 520 if featured else 360)

def md_link(text: str, url: str) -> str:
    safe = text.replace("[", "(").replace("]", ")")
    return f"[{safe}]({url})"


# ----------------------------
# TAGGING (IBGN)
# ----------------------------

def classify_tag(title: str, summary: str) -> str:
    hay = f"{title} {summary}".lower()

    if contains_any(hay, ["out now", "available now", "live now", "launch", "release date", "releases", "released"]):
        return "🗓️ Launch/Release"
    if contains_any(hay, ["update", "patch", "hotfix", "season", "expansion", "dlc"]):
        return "🛠️ Update/Patch"
    if contains_any(hay, ["announce", "announced", "announcement", "revealed", "reveal", "first look", "trailer"]):
        return "🧩 Reveal/Announcement"
    if contains_any(hay, ["layoff", "layoffs", "shutdown", "closed", "acquisition", "acquired", "merger", "union"]):
        return "🏢 Business/Industry"
    if contains_any(hay, ["lawsuit", "sued", "court", "legal", "tariff", "regulation"]):
        return "⚖️ Legal/Policy"
    if contains_any(hay, ["hack", "hacked", "breach", "security", "ransomware", "ddos"]):
        return "🔐 Security"
    if contains_any(hay, ["steps down", "stepping down", "resigns", "resignation", "retire", "retirement", "depart", "leaves"]):
        return "🧠 People/Leadership"
    if contains_any(hay, ["gpu", "nvidia", "amd", "intel", "driver", "hardware", "console", "steam deck", "rog ally"]):
        return "🖥️ Tech/Hardware"
    if contains_any(hay, ["esports", "tournament", "championship", "finals"]):
        return "🎯 Esports"

    return "🎮 Gaming News"


# ----------------------------
# OPEN GRAPH
# ----------------------------

def fetch_open_graph(url: str) -> Tuple[str, str]:
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=18)
        resp.raise_for_status()
        html = resp.text
    except Exception:
        return "", ""
    soup = BeautifulSoup(html, "html.parser")

    def meta(name: str) -> str:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return tag["content"].strip()
        return ""

    desc = meta("og:description") or meta("description") or meta("twitter:description")
    img = meta("og:image") or meta("twitter:image") or meta("twitter:image:src")
    return strip_html(desc), (img or "").strip()


# ----------------------------
# ADILO (API + scrape fallback)
# ----------------------------

def adilo_headers() -> Dict[str, str]:
    return {
        "X-Public-Key": ADILO_PUBLIC_KEY,
        "X-Secret-Key": ADILO_SECRET_KEY,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }

def adilo_get_json(url: str) -> Any:
    r = requests.get(url, headers=adilo_headers(), timeout=30)
    print(f"[ADILO] GET {url} -> HTTP {r.status_code}")
    if r.status_code >= 400:
        try:
            print("[ADILO] Error body snippet:", r.text[:800])
        except Exception:
            pass
        r.raise_for_status()
    return r.json()

def parse_dt_any(val: Any) -> Optional[datetime]:
    if not val:
        return None
    try:
        dt = dateparser.parse(str(val))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def _meta_total(meta_obj: Any) -> Optional[int]:
    if not isinstance(meta_obj, dict):
        return None
    for k in ["total", "Total", "totalCount", "total_count", "TotalCount", "records", "recordCount", "record_count"]:
        v = meta_obj.get(k)
        if v is None:
            continue
        try:
            return int(v)
        except Exception:
            pass
    return None

def _fetch_files_page_custom(project_id: str, from_i: int, to_i: int, extra_qs: str) -> Tuple[List[Dict[str, Any]], Optional[int], str]:
    url = f"{ADILO_API_BASE}/projects/{project_id}/files?From={from_i}&To={to_i}{extra_qs}"
    data = adilo_get_json(url)
    total = None
    payload_list: List[Dict[str, Any]] = []
    if isinstance(data, dict):
        total = _meta_total(data.get("meta"))
        payload = data.get("payload")
        if isinstance(payload, list):
            payload_list = [x for x in payload if isinstance(x, dict)]
    return payload_list, total, url

def _meta_upload_date(file_id: str) -> Optional[datetime]:
    meta = adilo_get_json(f"{ADILO_API_BASE}/files/{file_id}/meta")
    mp = meta.get("payload") if isinstance(meta, dict) else None
    upload_date = mp.get("upload_date") if isinstance(mp, dict) else None
    dt = parse_dt_any(upload_date)
    print(f"[ADILO] meta file_id={file_id} upload_date={upload_date}")
    return dt

def scrape_latest_adilo_id() -> Optional[str]:
    headers = {"User-Agent": USER_AGENT}
    candidates = [ADILO_PUBLIC_LATEST_PAGE, ADILO_PUBLIC_HOME_PAGE]
    for url in candidates:
        for attempt in range(1, 4):
            try:
                print(f"[ADILO] SCRAPE {url} attempt={attempt}")
                r = requests.get(url, headers=headers, timeout=18)
                print(f"[ADILO] SCRAPE status={r.status_code}")
                if r.status_code >= 400:
                    continue
                html = r.text

                # Try video?id=XXXX first
                m = re.search(r"video\?id=([A-Za-z0-9_-]{6,})", html)
                if m:
                    return m.group(1)

                # Try /watch/XXXX
                m2 = re.search(r"/watch/([A-Za-z0-9_-]{6,})", html)
                if m2:
                    return m2.group(1)

            except Exception as e:
                print(f"[ADILO] SCRAPE error: {e}")
                time.sleep(0.5)
    return None

def resolve_featured_adilo_watch_url() -> str:
    # 1) If force ID is set, ALWAYS use it.
    if FEATURED_VIDEO_FORCE_ID:
        print(f"[ADILO] Using FEATURED_VIDEO_FORCE_ID: https://adilo.bigcommand.com/watch/{FEATURED_VIDEO_FORCE_ID}")
        return f"https://adilo.bigcommand.com/watch/{FEATURED_VIDEO_FORCE_ID}"

    fallback_watch_url = ""
    if FEATURED_VIDEO_FALLBACK_ID:
        fallback_watch_url = f"https://adilo.bigcommand.com/watch/{FEATURED_VIDEO_FALLBACK_ID}"

    # 2) Try API
    if ADILO_PUBLIC_KEY and ADILO_SECRET_KEY and ADILO_PROJECT_ID:
        PROBES = ["", "&Sort=desc", "&sort=desc", "&OrderBy=upload_date&Order=desc", "&OrderBy=UploadDate&Order=desc"]
        best_dt = None
        best_id = None

        for extra in PROBES:
            try:
                items, total, url_used = _fetch_files_page_custom(ADILO_PROJECT_ID, 1, 50, extra)
                print(f"[ADILO] PROBE extra='{extra}' items={len(items)} total={total} url={url_used}")
                if not items:
                    continue

                sample_ids = [str(it.get("id")) for it in items[:15] if it.get("id")]
                newest_dt_probe = None
                newest_id_probe = None

                for fid in sample_ids:
                    dt = _meta_upload_date(fid)
                    if dt and (newest_dt_probe is None or dt > newest_dt_probe):
                        newest_dt_probe = dt
                        newest_id_probe = fid
                    time.sleep(0.03)

                if newest_dt_probe and newest_id_probe:
                    if best_dt is None or newest_dt_probe > best_dt:
                        best_dt = newest_dt_probe
                        best_id = newest_id_probe

                # If API result looks reasonably fresh, accept it immediately
                if newest_dt_probe and newest_dt_probe > (utcnow() - timedelta(days=14)):
                    return f"https://adilo.bigcommand.com/watch/{newest_id_probe}"

            except Exception as e:
                print(f"[ADILO] PROBE failed extra='{extra}': {e}")

        # If API found something but it looks stale, fall through to scrape
        if best_id and best_dt:
            candidate = f"https://adilo.bigcommand.com/watch/{best_id}"
            print(f"[ADILO] API newest candidate: {candidate} dt={best_dt.isoformat()}")
            print("[ADILO] API appears stale. Falling back to scrape.")
        else:
            print("[ADILO] API did not return usable items. Falling back to scrape.")
    else:
        print("[ADILO] Missing Adilo API settings. Falling back to scrape.")

    # 3) Scrape latest from public page
    latest_id = scrape_latest_adilo_id()
    if latest_id:
        print(f"[ADILO] SCRAPE newest id: {latest_id}")
        return f"https://adilo.bigcommand.com/watch/{latest_id}"

    return fallback_watch_url or FEATURED_VIDEO_FALLBACK_URL


# ----------------------------
# DISCORD POSTING (multi-message so embeds align)
# ----------------------------

def discord_post(content: str = "", embeds: Optional[List[Dict]] = None) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL is not set")

    payload: Dict[str, Any] = {"content": (content or "").strip()}
    if embeds:
        payload["embeds"] = embeds

    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)
    r.raise_for_status()

def discord_post_long_text(text: str) -> None:
    remaining = (text or "").strip()
    if not remaining:
        return

    while remaining:
        if len(remaining) <= DISCORD_SAFE_CONTENT:
            discord_post(remaining)
            break
        cut = remaining.rfind("\n\n", 0, DISCORD_SAFE_CONTENT)
        if cut == -1 or cut < 200:
            cut = DISCORD_SAFE_CONTENT
        chunk = remaining[:cut].rstrip()
        discord_post(chunk)
        remaining = remaining[cut:].lstrip()


# ----------------------------
# SCHEDULE GUARD (DST safe)
# ----------------------------

def should_run_now() -> bool:
    now = local_now()
    if now.hour != DIGEST_GUARD_LOCAL_HOUR:
        return False
    if now.minute >= DIGEST_GUARD_WINDOW_MINUTES:
        return False
    return True


# ----------------------------
# NEWSLETTER COPY (IBGN voice)
# ----------------------------

def build_header(date_line: str, teaser_lines: List[str]) -> str:
    teaser_block = "\n".join(teaser_lines)
    return (
        f"{date_line}\n\n"
        f"**{NEWSLETTER_NAME} — Nightly Recap**\n"
        f"*{NEWSLETTER_TAGLINE}*\n\n"
        "**Tonight’s quick bites:**\n"
        f"{teaser_block}\n\n"
        "Now, the full top 5:\n"
    )

def build_story_text(i: int, tag: str, title: str, summary: str, source: str, url: str) -> str:
    return (
        f"**{i}) {tag} — {title}**\n"
        f"{summary}\n"
        f"Source: {md_link(source, url)}"
    )

def build_footer() -> str:
    # removed “just the signal” line per your note
    return (
        "—\n"
        "That’s the recap.\n"
        f"See you tomorrow on **{NEWSLETTER_NAME}**."
    )


# ----------------------------
# MAIN
# ----------------------------

def main():
    event = os.getenv("GITHUB_EVENT_NAME", "").strip()
    if event == "schedule" and not should_run_now():
        ln = local_now().strftime("%Y-%m-%d %H:%M:%S %Z")
        print(f"[GUARD] Not within posting window. Local now: {ln}. Exiting without posting.")
        return

    cutoff = utcnow() - timedelta(hours=WINDOW_HOURS)

    items: List[Dict] = []
    for f in FEEDS:
        try:
            resp = requests.get(f["url"], headers={"User-Agent": USER_AGENT}, timeout=20)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.text)

            for entry in parsed.entries[:200]:
                title = (getattr(entry, "title", "") or "").strip()
                link = (getattr(entry, "link", "") or "").strip()
                if not title or not link:
                    continue

                url = normalize_url(link)
                published_at = safe_parse_date(entry)

                summary = ""
                for key in ["summary", "description", "subtitle"]:
                    val = getattr(entry, key, None)
                    if val:
                        summary = strip_html(val)
                        break

                image_url = ""
                media_content = getattr(entry, "media_content", None)
                if media_content and isinstance(media_content, list):
                    for m in media_content:
                        u = (m.get("url") or "").strip()
                        if u:
                            image_url = u
                            break

                items.append({
                    "source": f["name"],
                    "title": title,
                    "url": url,
                    "published_at": published_at,
                    "summary": summary,
                    "image_url": image_url,
                })
        except Exception as e:
            print(f"[WARN] Feed fetch failed: {f['name']} -> {e}")

    kept = []
    for it in items:
        if it["published_at"] < cutoff:
            continue
        if block_reason(it["title"], it["summary"]) != "":
            continue
        kept.append(it)

    seen = set()
    deduped = []
    for it in sorted(kept, key=lambda x: x["published_at"], reverse=True):
        key = normalize_title_key(it["title"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)

    def score_item(item: Dict) -> float:
        prio = {s: i for i, s in enumerate(SOURCE_PRIORITY)}
        p = prio.get(item["source"], 999)
        age_hours = max(0.0, (utcnow() - item["published_at"]).total_seconds() / 3600.0)
        recency_score = max(0.0, 36.0 - age_hours)
        source_score = max(0.0, 10.0 - (p * 0.8))
        hay = f'{item["title"]} {item["summary"]}'.lower()
        hint = 8.0 if contains_any(hay, NEWS_HINTS) else 0.0
        blues_penalty = 2.5 if item["source"] == "Blue's News" else 0.0
        return recency_score + source_score + hint - blues_penalty

    by_source: Dict[str, List[Dict]] = {}
    for it in deduped:
        by_source.setdefault(it["source"], []).append(it)
    for s in by_source:
        by_source[s].sort(key=score_item, reverse=True)

    picked: List[Dict] = []
    used = set()
    counts: Dict[str, int] = {}

    for s in SOURCE_PRIORITY:
        if len(picked) >= TOP_N:
            break
        if s not in by_source or not by_source[s]:
            continue
        it = by_source[s][0]
        k = normalize_title_key(it["title"])
        if k in used:
            continue
        picked.append(it)
        used.add(k)
        counts[s] = counts.get(s, 0) + 1

    if len(picked) < TOP_N:
        remaining = sorted(deduped, key=score_item, reverse=True)
        for it in remaining:
            if len(picked) >= TOP_N:
                break
            s = it["source"]
            if counts.get(s, 0) >= MAX_PER_SOURCE:
                continue
            k = normalize_title_key(it["title"])
            if k in used:
                continue
            picked.append(it)
            used.add(k)
            counts[s] = counts.get(s, 0) + 1

    ranked = sorted(picked, key=score_item, reverse=True)

    # Enrich summaries/images + tags
    for idx, it in enumerate(ranked):
        if not it["summary"] or not it["image_url"]:
            desc, img = fetch_open_graph(it["url"])
            if not it["summary"] and desc:
                it["summary"] = desc
            if not it["image_url"] and img:
                it["image_url"] = img

        it["summary"] = build_story_summary(strip_html(it["summary"]), it["source"], featured=(idx == 0))
        it["tag"] = classify_tag(it["title"], it["summary"])

    # Resolve Adilo
    featured_adilo_url = resolve_featured_adilo_watch_url()
    adilo_desc, adilo_img = fetch_open_graph(featured_adilo_url)

    # ----------------------------
    # POST: header → story-by-story (embed matches) → YouTube → Adilo → footer
    # ----------------------------

    ln = local_now()
    date_line = ln.strftime("%B %d, %Y")

    if not ranked:
        discord_post_long_text(
            f"{date_line}\n\n"
            f"**{NEWSLETTER_NAME} — Nightly Recap**\n"
            f"*{NEWSLETTER_TAGLINE}*\n\n"
            "Nothing cleared the news-only filters tonight. Quiet one.\n"
        )
    else:
        teasers = ranked[:3]
        teaser_lines = [f"► {t['tag']} — {t['title']}" for t in teasers]
        discord_post_long_text(build_header(date_line, teaser_lines))

        for i, it in enumerate(ranked, start=1):
            story_text = build_story_text(i, it["tag"], it["title"], it["summary"], it["source"], it["url"])

            embed = {
                "title": f"{i}) {it['tag']} — {it['title']}",
                "url": it["url"],
                "description": shorten(it["summary"], EMBED_DESC_LIMIT),
                "footer": {"text": f"Source: {it['source']}"},
                "timestamp": it["published_at"].isoformat(),
            }
            if it.get("image_url"):
                embed["image"] = {"url": it["image_url"]}

            discord_post(story_text, embeds=[embed])

    # Videos: YouTube first (raw URL line so Discord embeds it), then Adilo with thumbnail embed
    if YOUTUBE_FEATURED_URL:
        youtube_msg = (
            "**▶️ Featured Video (YouTube)**\n"
            f"{YOUTUBE_FEATURED_TITLE}\n"
            f"{YOUTUBE_FEATURED_URL}"
        )
        discord_post_long_text(youtube_msg)

    adilo_msg = (
        "**📺 Featured Video (Adilo)**\n"
        f"{md_link(FEATURED_VIDEO_TITLE, featured_adilo_url)}"
    )
    adilo_embed = {
        "title": f"{FEATURED_VIDEO_TITLE} (Adilo)",
        "url": featured_adilo_url,
        "description": shorten(adilo_desc or "Watch the latest episode on Adilo.", 500),
    }
    if adilo_img:
        adilo_embed["image"] = {"url": adilo_img}

    discord_post(adilo_msg, embeds=[adilo_embed])

    discord_post_long_text(build_footer())

    print(f"Digest posted. Items: {len(ranked)}")
    print("Featured Adilo video:", featured_adilo_url)
    if FEATURED_VIDEO_FORCE_ID:
        print("FEATURED_VIDEO_FORCE_ID is set -> forced video used.")
    if YOUTUBE_FEATURED_URL:
        print("YouTube featured:", YOUTUBE_FEATURED_URL)


if __name__ == "__main__":
    main()
