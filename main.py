import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Dict
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import feedparser
import requests
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

MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "12"))
TITLE_FUZZY_THRESHOLD = int(os.getenv("TITLE_FUZZY_THRESHOLD", "92"))

# You do NOT need to change this.
STATE_FILE = "state.json"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNewsBot/1.0")

# Remove common tracking parameters so duplicates are easier to detect
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


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_url(url: str) -> str:
    """Remove tracking params and fragments from URLs."""
    try:
        parsed = urlparse(url)
        query = [(k, v) for (k, v) in parse_qsl(parsed.query, keep_blank_values=True)
                 if k.lower() not in TRACKING_PARAMS]
        new_query = urlencode(query, doseq=True)
        parsed = parsed._replace(query=new_query, fragment="")
        return urlunparse(parsed).strip()
    except Exception:
        return url.strip()


def safe_parse_date(entry) -> datetime:
    """Try to get a published date from RSS. Fallback to now."""
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
        return {"seen_urls": [], "seen_titles": []}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_feed(feed_name: str, feed_url: str) -> List[Item]:
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(feed_url, headers=headers, timeout=20)
    resp.raise_for_status()

    parsed = feedparser.parse(resp.text)

    items: List[Item] = []
    for entry in parsed.entries[:50]:
        title = (getattr(entry, "title", "") or "").strip()
        link = (getattr(entry, "link", "") or "").strip()

        # Some RSS/RDF feeds may store links differently; feedparser usually populates .link,
        # but this fallback helps in edge cases.
        if not link:
            links = getattr(entry, "links", None)
            if links and isinstance(links, list) and len(links) > 0:
                link = (links[0].get("href") or "").strip()

        if not title or not link:
            continue

        items.append(Item(
            source=feed_name,
            title=title,
            url=normalize_url(link),
            published_at=safe_parse_date(entry)
        ))
    return items


def is_duplicate(item: Item, state: Dict) -> bool:
    if item.url in state["seen_urls"]:
        return True

    title_norm = re.sub(r"\s+", " ", item.title.strip().lower())
    for seen in state["seen_titles"][-300:]:
        if fuzz.ratio(title_norm, seen) >= TITLE_FUZZY_THRESHOLD:
            return True
    return False


def remember(item: Item, state: Dict) -> None:
    state["seen_urls"].append(item.url)
    title_norm = re.sub(r"\s+", " ", item.title.strip().lower())
    state["seen_titles"].append(title_norm)

    state["seen_urls"] = state["seen_urls"][-2000:]
    state["seen_titles"] = state["seen_titles"][-2000:]


def discord_post(item: Item) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL is not set")

    payload = {
        "embeds": [
            {
                "title": item.title,
                "url": item.url,
                "description": f"Source: **{item.source}**",
                "timestamp": item.published_at.isoformat(),
            }
        ]
    }

    resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=20)
    resp.raise_for_status()


def main():
    state = load_state()

    all_items: List[Item] = []
    for f in FEEDS:
        try:
            items = fetch_feed(f["name"], f["url"])
            all_items.extend(items)
        except Exception as e:
            print(f"[WARN] Feed fetch failed: {f['name']} -> {e}")

    all_items.sort(key=lambda x: x.published_at, reverse=True)

    posted = 0
    for item in all_items:
        if posted >= MAX_POSTS_PER_RUN:
            break
        if is_duplicate(item, state):
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
