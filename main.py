import json
import os
import re
import time
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Dict, Tuple, Optional
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from rapidfuzz import fuzz

# ----------------------------
# BEGINNER CONFIG SECTION
# ----------------------------

FEEDS = [
    {"name": "IGN", "url": "http://feeds.ign.com/ign/all"},
    {"name": "GameSpot", "url": "http://www.gamespot.com/feeds/mashup/"},
    {"name": "Blue's News", "url": "https://www.bluesnews.com/news/news_1_0.rdf"},
]

# Prefer one source when the same story appears across multiple sources
SOURCE_PRIORITY = ["IGN", "GameSpot", "Blue's News"]

MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "12"))
TITLE_FUZZY_THRESHOLD = int(os.getenv("TITLE_FUZZY_THRESHOLD", "92"))

# Balanced filter: Gaming + adjacent
STRONG_GAME_TERMS = [
    "video game", "videogame", "game", "gaming",
    "xbox", "playstation", "ps5", "ps4", "nintendo", "switch", "steam", "epic games", "gog", "game pass",
    "pc gaming", "console", "handheld",
    "release date", "launch", "early access", "beta", "alpha", "demo",
    "patch", "update", "hotfix", "season", "battle pass", "dlc", "expansion", "roadmap",
    "servers", "crossplay", "cross-play",
    "preorder", "pre-order", "price", "pricing",
    "studio", "developer", "publisher",
    "esports", "tournament", "championship",

    # Important “news” terms (helps catch stories like Bluepoint closure)
    "closed", "closing", "closure", "shut down", "shutdown",
    "layoff", "layoffs", "cut", "cuts",
    "canceled", "cancelled", "delayed", "delay",
    "acquired", "acquisition", "merger",
    "lawsuit", "sued",
    "retire", "retirement",
    "playstation studios",

    # Specific studios/franchises you care about (add more anytime)
    "bluepoint",
]

ADJACENT_TERMS = [
    "gpu", "graphics card", "nvidia", "amd", "intel", "driver", "dlss", "fsr",
    "steam deck", "rog ally", "handheld pc",
    "unity", "unreal engine", "unreal", "engine", "mod", "mods", "modding",
    "discord", "twitch", "youtube gaming", "streaming",
    "vr", "virtual reality", "meta quest",
]

# “No fluff” blockers
CONTENT_TYPE_BLOCK = [
    "review", "preview", "impressions",
    "guide", "walkthrough", "tips", "tricks",
    "best ", "top ", "ranked", "ranking", "tier list",
    "everything you need to know", "explained",
]

# Entertainment-only signals we want to avoid unless strong gaming terms are present
ENTERTAINMENT_BLOCK = [
    "movie", "film", "tv", "television", "series", "episode", "season finale",
    "netflix", "hulu", "disney", "paramount", "max", "hbo",
    "box office", "actor", "actress", "cast", "celebrity", "red carpet",
    "anime",
]

# If a story was already posted, only allow a repeat if the title contains an “update” keyword.
UPDATE_KEYWORDS = [
    "update", "updated", "new details", "more details", "confirmed", "now",
    "patch", "hotfix", "statement", "responds", "clarifies", "report",
    "finally", "release date", "launch date",
]

STATE_FILE = "state.json"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNewsBot/1.3")

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_reader", "utm_referrer",
    "gclid", "fbclid", "mc_cid", "mc_eid", "ref", "source"
}

# ----------------------------
# END BEGINNER CONFIG SECTION
# ----------------------------


@dataclass
class Item:
    source: str
    title: str
    url: str
    published_at: datetime
    summary: str = ""
    image_url: str = ""
    story_key: str = ""  # used for clustering & dedupe


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_url(url: str) -> str:
    """Remove tracking params + fragments; normalize scheme/hostname casing."""
    try:
        parsed = urlparse(url.strip())
        # strip tracking params
        query = [(k, v) for (k, v) in parse_qsl(parsed.query, keep_blank_values=True)
                 if k.lower() not in TRACKING_PARAMS]
        new_query = urlencode(query, doseq=True)
        parsed = parsed._replace(query=new_query, fragment="")

        # normalize netloc casing
        netloc = parsed.netloc.lower()
        parsed = parsed._replace(netloc=netloc)

        return urlunparse(parsed).strip()
    except Exception:
        return url.strip()


def strip_html(text: str) -> str:
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def shorten(text: str, max_len: int = 280) -> str:
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


def load_state() -> Dict:
    if not os.path.exists(STATE_FILE):
        return {"seen_urls": [], "seen_titles": [], "seen_story_keys": []}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)
    # backward compatible with older state.json
    state.setdefault("seen_story_keys", [])
    return state


def save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _count_hits(hay: str, terms: List[str]) -> int:
    return sum(1 for t in terms if t in hay)


def is_relevant(title: str, summary: str) -> bool:
    hay = f"{title} {summary}".lower()

    strong = _count_hits(hay, [t.lower() for t in STRONG_GAME_TERMS])
    adjacent = _count_hits(hay, [t.lower() for t in ADJACENT_TERMS])
    entertainment = _count_hits(hay, [t.lower() for t in ENTERTAINMENT_BLOCK])
    fluff = _count_hits(hay, [t.lower() for t in CONTENT_TYPE_BLOCK])

    # Entertainment-only: reject unless clearly gaming news
    if entertainment > 0 and strong == 0:
        return False

    # No-fluff: reject guides/listicles unless clearly gaming news
    if fluff > 0 and strong == 0:
        return False

    # Accept if strong gaming signal exists
    if strong >= 1:
        return True

    # Accept adjacent-only if it’s clearly gaming-adjacent (2+ hits, not entertainment, not fluff)
    if adjacent >= 2 and entertainment == 0 and fluff == 0:
        return True

    return False


def contains_update_keyword(title: str, summary: str) -> bool:
    hay = f"{title} {summary}".lower()
    return any(k.lower() in hay for k in UPDATE_KEYWORDS)


def make_story_key(title: str) -> str:
    """
    Story key used for clustering:
    - normalize title
    - remove punctuation / extra whitespace
    - hash it
    """
    t = title.lower()
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    # shorten titles that have trailing site labels etc.
    t = re.sub(r"\s+\-\s+\w+$", "", t).strip()
    return hashlib.sha1(t.encode("utf-8")).hexdigest()


def extract_from_entry(entry) -> Tuple[str, str]:
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

    if not image_url:
        media_thumbnail = getattr(entry, "media_thumbnail", None)
        if media_thumbnail and isinstance(media_thumbnail, list):
            for m in media_thumbnail:
                u = (m.get("url") or "").strip()
                if u:
                    image_url = u
                    break

    if not image_url:
        enclosures = getattr(entry, "enclosures", None)
        if enclosures and isinstance(enclosures, list):
            for e in enclosures:
                u = (e.get("href") or e.get("url") or "").strip()
                t = (e.get("type") or "").lower()
                if u and ("image" in t or u.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))):
                    image_url = u
                    break

    return summary, image_url


def fetch_open_graph(url: str) -> Tuple[str, str]:
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
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


def fetch_feed(feed_name: str, feed_url: str) -> List[Item]:
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(feed_url, headers=headers, timeout=20)
    resp.raise_for_status()

    parsed = feedparser.parse(resp.text)

    items: List[Item] = []
    for entry in parsed.entries[:80]:
        title = (getattr(entry, "title", "") or "").strip()
        link = (getattr(entry, "link", "") or "").strip()

        if not link:
            links = getattr(entry, "links", None)
            if links and isinstance(links, list) and len(links) > 0:
                link = (links[0].get("href") or "").strip()

        if not title or not link:
            continue

        url = normalize_url(link)
        published_at = safe_parse_date(entry)

        entry_summary, entry_image = extract_from_entry(entry)

        items.append(Item(
            source=feed_name,
            title=title,
            url=url,
            published_at=published_at,
            summary=entry_summary,
            image_url=entry_image,
            story_key=make_story_key(title)
        ))

    return items


def is_duplicate_or_allowed_update(item: Item, state: Dict) -> bool:
    """
    Skip if:
      - URL already posted
      - OR story_key already posted AND not an update
      - OR fuzzy-title matches already posted AND not an update
    Allow repeat only if title/summary includes update keywords.
    """
    # exact URL seen
    if item.url in state["seen_urls"]:
        return True

    is_update = contains_update_keyword(item.title, item.summary)

    # story_key seen
    if item.story_key in state["seen_story_keys"] and not is_update:
        return True

    # fuzzy title seen (extra safety)
    title_norm = re.sub(r"\s+", " ", item.title.strip().lower())
    for seen in state["seen_titles"][-400:]:
        if fuzz.ratio(title_norm, seen) >= TITLE_FUZZY_THRESHOLD and not is_update:
            return True

    return False


def remember(item: Item, state: Dict) -> None:
    state["seen_urls"].append(item.url)
    title_norm = re.sub(r"\s+", " ", item.title.strip().lower())
    state["seen_titles"].append(title_norm)
    state["seen_story_keys"].append(item.story_key)

    # keep state bounded
    state["seen_urls"] = state["seen_urls"][-4000:]
    state["seen_titles"] = state["seen_titles"][-4000:]
    state["seen_story_keys"] = state["seen_story_keys"][-4000:]


def pick_best_source(cluster: List[Item]) -> Item:
    """
    Pick the best item from a cluster using SOURCE_PRIORITY.
    """
    priority = {name: i for i, name in enumerate(SOURCE_PRIORITY)}
    cluster_sorted = sorted(
        cluster,
        key=lambda x: (priority.get(x.source, 999), -x.published_at.timestamp())
    )
    return cluster_sorted[0]


def cluster_items(items: List[Item]) -> List[Item]:
    """
    Cluster by story_key, then pick one source per cluster.
    """
    buckets: Dict[str, List[Item]] = {}
    for it in items:
        buckets.setdefault(it.story_key, []).append(it)

    chosen: List[Item] = []
    for key, group in buckets.items():
        chosen.append(pick_best_source(group))

    # newest first
    chosen.sort(key=lambda x: x.published_at, reverse=True)
    return chosen


def discord_post(item: Item) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL is not set")

    summary = item.summary or ""
    image_url = item.image_url or ""

    # If missing, pull from the article metadata
    if not summary or not image_url:
        og_desc, og_img = fetch_open_graph(item.url)
        if not summary and og_desc:
            summary = og_desc
        if not image_url and og_img:
            image_url = og_img

    summary = shorten(summary, 320)

    embed = {
        "title": item.title,
        "url": item.url,
        "timestamp": item.published_at.isoformat(),
        "footer": {"text": f"Source: {item.source}"},
    }

    if summary:
        embed["description"] = summary
    if image_url:
        embed["image"] = {"url": image_url}

    payload = {"embeds": [embed]}
    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)
    resp.raise_for_status()


def main():
    state = load_state()

    all_items: List[Item] = []
    for f in FEEDS:
        try:
            all_items.extend(fetch_feed(f["name"], f["url"]))
        except Exception as e:
            print(f"[WARN] Feed fetch failed: {f['name']} -> {e}")

    # First apply relevancy filter (all sources)
    filtered = [it for it in all_items if is_relevant(it.title, it.summary)]

    # Then cluster to avoid multi-source repeats
    clustered = cluster_items(filtered)

    posted = 0
    for item in clustered:
        if posted >= MAX_POSTS_PER_RUN:
            break

        # Avoid reposts unless it's an update
        if is_duplicate_or_allowed_update(item, state):
            continue

        try:
            discord_post(item)
            remember(item, state)
            posted += 1
            print(f"[POSTED] {item.source}: {item.title}")
        except Exception as e:
            print(f"[ERROR] Post failed: {item.title} -> {e}")

    save_state(state)
    print(f"Done. Posted {posted} item(s).")


if __name__ == "__main__":
    main()
