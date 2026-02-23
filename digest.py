import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Tuple, Optional
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from zoneinfo import ZoneInfo


# ----------------------------
# EXISTING DIGEST SETTINGS (keep your feeds/filters etc.)
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
USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNews/Digest-API-1.0")

WINDOW_HOURS = int(os.getenv("DIGEST_WINDOW_HOURS", "24"))
TOP_N = int(os.getenv("DIGEST_TOP_N", "5"))
MAX_PER_SOURCE = int(os.getenv("DIGEST_MAX_PER_SOURCE", "1"))

FEATURED_VIDEO_TITLE = os.getenv("FEATURED_VIDEO_TITLE", "Watch today’s Itty Bitty Gaming News").strip()

# Hub fallback (fast-player link is preferred, but this prevents broken posts)
FEATURED_VIDEO_FALLBACK_URL = os.getenv(
    "FEATURED_VIDEO_FALLBACK_URL",
    "https://adilo.bigcommand.com/c/ittybittygamingnews/home"
).strip()

# Adilo API settings (from GitHub Secrets passed via workflow env)
ADILO_PUBLIC_KEY = os.getenv("ADILO_PUBLIC_KEY", "").strip()
ADILO_SECRET_KEY = os.getenv("ADILO_SECRET_KEY", "").strip()
ADILO_PROJECT_SEARCH = os.getenv("ADILO_PROJECT_SEARCH", "Itty Bitty Gaming News").strip()
ADILO_API_BASE = "https://adilo-api.bigcommand.com/v1"

DISCORD_SAFE_CONTENT = 1850
EMBED_DESC_LIMIT = 900

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_reader", "utm_referrer",
    "gclid", "fbclid", "mc_cid", "mc_eid", "ref", "source"
}


# ----------------------------
# SMALL UTILS
# ----------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def pacific_now() -> datetime:
    return datetime.now(ZoneInfo("America/Los_Angeles"))

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
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()

def shorten(text: str, max_len: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"

def md_link(text: str, url: str) -> str:
    safe = text.replace("[", "(").replace("]", ")")
    return f"[{safe}]({url})"

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


# ----------------------------
# ADILO API: GET LATEST VIDEO WATCH LINK
# ----------------------------

def adilo_headers() -> Dict[str, str]:
    return {
        "X-Public-Key": ADILO_PUBLIC_KEY,
        "X-Secret-Key": ADILO_SECRET_KEY,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }

def adilo_get_json(url: str) -> Dict:
    r = requests.get(url, headers=adilo_headers(), timeout=25)
    r.raise_for_status()
    return r.json()

def extract_watch_id_from_any(obj) -> Optional[str]:
    """
    Tries to find an 8ish-char watch id like K4AxdfCP in any JSON blob.
    Also accepts longer ids if Adilo uses a different length.
    """
    if obj is None:
        return None

    # Direct string URL like https://adilo.bigcommand.com/watch/K4AxdfCP
    if isinstance(obj, str):
        m = re.search(r"/watch/([A-Za-z0-9_-]{6,})", obj)
        if m:
            return m.group(1)

        # Share style ...video?id=K4AxdfCP
        m = re.search(r"[?&]id=([A-Za-z0-9_-]{6,})", obj)
        if m:
            return m.group(1)

        return None

    # Lists
    if isinstance(obj, list):
        for item in obj:
            got = extract_watch_id_from_any(item)
            if got:
                return got
        return None

    # Dicts
    if isinstance(obj, dict):
        # Common keys you might see
        for key in [
            "watch_id", "watchId", "public_id", "publicId",
            "short_id", "shortId", "code", "video_id", "videoId",
            "share_id", "shareId", "embed_id", "embedId",
            "watch_url", "watchUrl", "url", "link"
        ]:
            if key in obj:
                got = extract_watch_id_from_any(obj.get(key))
                if got:
                    return got

        # Scan all values
        for v in obj.values():
            got = extract_watch_id_from_any(v)
            if got:
                return got

    return None

def parse_datetime_from_any(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        # could be unix seconds
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        try:
            dt = dateparser.parse(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None
    return None

def find_project_id() -> Optional[str]:
    """
    Uses Adilo endpoint: GET /projects/search/{string}
    """
    if not (ADILO_PUBLIC_KEY and ADILO_SECRET_KEY):
        return None

    q = requests.utils.quote(ADILO_PROJECT_SEARCH, safe="")
    url = f"{ADILO_API_BASE}/projects/search/{q}"
    data = adilo_get_json(url)

    # Response shape can vary; try multiple common shapes
    candidates = []
    if isinstance(data, dict):
        for key in ["data", "projects", "results", "items"]:
            if isinstance(data.get(key), list):
                candidates = data[key]
                break
        if not candidates and isinstance(data.get("data"), dict):
            # sometimes nested
            for key in ["projects", "results", "items"]:
                if isinstance(data["data"].get(key), list):
                    candidates = data["data"][key]
                    break
    elif isinstance(data, list):
        candidates = data

    for p in candidates:
        if isinstance(p, dict) and p.get("id"):
            return str(p["id"])
    return None

def list_project_files(project_id: str, limit: int = 50) -> List[Dict]:
    """
    Uses Adilo endpoint: GET /projects/{project_id}/files
    """
    url = f"{ADILO_API_BASE}/projects/{project_id}/files?From=1&To={limit}"
    data = adilo_get_json(url)

    # Try common list locations
    if isinstance(data, dict):
        for key in ["data", "files", "results", "items"]:
            if isinstance(data.get(key), list):
                return data[key]
        if isinstance(data.get("data"), dict):
            for key in ["files", "results", "items"]:
                if isinstance(data["data"].get(key), list):
                    return data["data"][key]
    if isinstance(data, list):
        return data
    return []

def get_file_meta(file_id: str) -> Dict:
    """
    Uses Adilo endpoint: GET /files/{file_id}/meta
    """
    url = f"{ADILO_API_BASE}/files/{file_id}/meta"
    return adilo_get_json(url)

def get_latest_adilo_watch_url() -> Optional[str]:
    """
    Best effort:
    1) Find project id by search
    2) List files in project
    3) Choose most recent by timestamp fields
    4) Extract watch id directly or via file meta
    """
    if not (ADILO_PUBLIC_KEY and ADILO_SECRET_KEY):
        return None

    project_id = find_project_id()
    if not project_id:
        return None

    files = list_project_files(project_id, limit=50)
    if not files:
        return None

    # Sort by best timestamp we can find
    def file_ts(f: Dict) -> datetime:
        for k in ["created_at", "createdAt", "updated_at", "updatedAt", "date", "created", "updated"]:
            dt = parse_datetime_from_any(f.get(k))
            if dt:
                return dt
        return datetime(1970, 1, 1, tzinfo=timezone.utc)

    files_sorted = sorted(
        [f for f in files if isinstance(f, dict)],
        key=file_ts,
        reverse=True
    )

    for f in files_sorted[:10]:
        # 1) try to get watch id directly from file object
        wid = extract_watch_id_from_any(f)
        if wid:
            return f"https://adilo.bigcommand.com/watch/{wid}"

        # 2) try meta endpoint to discover the watch URL/id
        fid = f.get("id")
        if fid:
            try:
                meta = get_file_meta(str(fid))
                wid2 = extract_watch_id_from_any(meta)
                if wid2:
                    return f"https://adilo.bigcommand.com/watch/{wid2}"
            except Exception:
                pass

    return None


# ----------------------------
# FEED FETCH (unchanged-ish)
# ----------------------------

def fetch_feed(feed_name: str, feed_url: str) -> List[Dict]:
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(feed_url, headers=headers, timeout=20)
    resp.raise_for_status()

    parsed = feedparser.parse(resp.text)
    out = []
    for entry in parsed.entries[:200]:
        title = (getattr(entry, "title", "") or "").strip()
        link = (getattr(entry, "link", "") or "").strip()
        if not title or not link:
            continue

        published_at = safe_parse_date(entry)
        summary = ""
        for key in ["summary", "description", "subtitle"]:
            val = getattr(entry, key, None)
            if val:
                summary = strip_html(val)
                break

        out.append({
            "source": feed_name,
            "title": title,
            "url": normalize_url(link),
            "published_at": published_at,
            "summary": summary,
        })
    return out

def post_to_discord(content: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL is not set")
    payload = {"content": content}
    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=25)
    r.raise_for_status()


# ----------------------------
# MAIN (digest simplified here to focus on featured video)
# ----------------------------

def main():
    pn = pacific_now()
    date_line = pn.strftime("%B %d, %Y")

    # Featured video: API-first, fallback to hub
    featured = get_latest_adilo_watch_url() or FEATURED_VIDEO_FALLBACK_URL

    content = (
        f"{date_line}\n\n"
        f"**Itty Bitty Gaming News — Featured Video**\n\n"
        f"📺 {md_link(FEATURED_VIDEO_TITLE, featured)}\n"
    )

    post_to_discord(content)
    print("Posted featured video:", featured)

if __name__ == "__main__":
    main()
