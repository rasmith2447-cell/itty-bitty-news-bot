import json
import os
import re
import time
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Tuple
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from rapidfuzz import fuzz

# ----------------------------
# CONFIG
# ----------------------------

FEEDS = [
    {"name": "IGN", "url": "http://feeds.ign.com/ign/all"},
    {"name": "GameSpot", "url": "http://www.gamespot.com/feeds/mashup/"},
    {"name": "Blue's News", "url": "https://www.bluesnews.com/news/news_1_0.rdf"},

    # Higher-signal news feeds
    {"name": "VGC", "url": "https://www.videogameschronicle.com/category/news/feed/"},
    {"name": "Gematsu", "url": "https://www.gematsu.com/feed"},
    {"name": "Polygon", "url": "https://www.polygon.com/rss/news/index.xml"},
    {"name": "Nintendo Life", "url": "https://www.nintendolife.com/feeds/latest"},
    {"name": "PC Gamer", "url": "https://www.pcgamer.com/rss"},
]

SOURCE_PRIORITY = [
    "IGN", "GameSpot", "VGC", "Gematsu",
    "Polygon", "Nintendo Life", "PC Gamer", "Blue's News"
]

MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "12"))
TITLE_FUZZY_THRESHOLD = int(os.getenv("TITLE_FUZZY_THRESHOLD", "92"))

BREAKING_MODE = os.getenv("BREAKING_MODE", "0").strip() == "1"
BREAKING_MAX_AGE_HOURS = int(os.getenv("BREAKING_MAX_AGE_HOURS", "72"))

MODE = os.getenv("MODE", "RAW").strip().upper()
SKIP_STATE_UPDATE = os.getenv("SKIP_STATE_UPDATE", "0").strip() == "1"
DEBUG = os.getenv("DEBUG", "0").strip() == "1"

STATE_FILE = "state.json"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNewsBot/2.0")

# ----------------------------
# FILTER TERMS
# ----------------------------

GAME_TERMS = [
    "game", "gaming", "video game", "videogame",
    "playstation", "ps5", "ps4", "xbox", "nintendo", "switch",
    "steam", "epic games", "game pass",
    "patch", "update", "hotfix",
    "release", "launch",
    "developer", "studio", "publisher",
]

LISTICLE_BLOCK = [
    "best ", "top ", "ranked", "ranking",
    "review", "preview", "impressions",
    "guide", "walkthrough", "tips", "tricks",
]

EVERGREEN_BLOCK = [
    "history of", "timeline", "recap", "retrospective",
    "what we know so far", "ending explained",
]

DEALS_BLOCK = [
    "deal", "deals", "discount", "sale",
    "drops to", "lowest price",
    "save ", "% off", "$",
    "woot", "amazon", "best buy", "newegg",
]

RUMOR_BLOCK = [
    "rumor", "rumour", "leak", "leaked",
    "speculation", "reportedly", "allegedly",
]

BREAKING_KEYWORDS = [
    "announced", "revealed", "announcement",
    "patch", "update", "hotfix",
    "delay", "delayed",
    "layoff", "layoffs",
    "acquisition", "merger",
    "shutdown", "closed",
    "out now", "available now", "live now",
]

UPDATE_KEYWORDS = ["update", "hotfix", "patch"]

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign",
    "utm_term", "utm_content", "gclid",
    "fbclid", "ref", "source"
}

# ----------------------------
# DATA STRUCTURE
# ----------------------------

@dataclass
class Item:
    source: str
    title: str
    url: str
    published_at: datetime
    summary: str = ""
    image_url: str = ""
    story_key: str = ""


# ----------------------------
# UTILITIES
# ----------------------------

def utcnow():
    return datetime.now(timezone.utc)


def normalize_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        query = [(k, v) for (k, v) in parse_qsl(parsed.query)
                 if k.lower() not in TRACKING_PARAMS]
        parsed = parsed._replace(query=urlencode(query), fragment="")
        return urlunparse(parsed)
    except Exception:
        return url


def strip_html(text: str) -> str:
    if not text:
        return ""
    if "<" not in text:
        return re.sub(r"\s+", " ", text).strip()
    soup = BeautifulSoup(text, "html.parser")
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def make_story_key(title: str) -> str:
    t = re.sub(r"[^a-z0-9\s]", " ", title.lower())
    t = re.sub(r"\s+", " ", t).strip()
    return hashlib.sha1(t.encode()).hexdigest()


def contains_any(text: str, terms: List[str]) -> bool:
    return any(term in text for term in terms)


# ----------------------------
# FILTER LOGIC
# ----------------------------

def block_reason(title, summary):
    hay = (title + " " + summary).lower()

    if not contains_any(hay, GAME_TERMS):
        return "NOT_GAME"

    if contains_any(hay, LISTICLE_BLOCK):
        return "LISTICLE"

    if contains_any(hay, EVERGREEN_BLOCK):
        return "EVERGREEN"

    if contains_any(hay, DEALS_BLOCK):
        return "DEALS"

    if contains_any(hay, RUMOR_BLOCK):
        return "RUMOR"

    return ""


def is_breaking(title, summary, published_at):
    if utcnow() - published_at > timedelta(hours=BREAKING_MAX_AGE_HOURS):
        return False
    hay = (title + " " + summary).lower()
    return contains_any(hay, BREAKING_KEYWORDS)


# ----------------------------
# FETCHING
# ----------------------------

def fetch_feed(feed_name, feed_url):
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(feed_url, headers=headers, timeout=20)
    resp.raise_for_status()

    parsed = feedparser.parse(resp.text)
    items = []

    for entry in parsed.entries[:200]:
        title = getattr(entry, "title", "").strip()
        link = getattr(entry, "link", "").strip()
        if not title or not link:
            continue

        summary = strip_html(getattr(entry, "summary", ""))
        published = utcnow()

        items.append(Item(
            source=feed_name,
            title=title,
            url=normalize_url(link),
            published_at=published,
            summary=summary,
            story_key=make_story_key(title),
        ))

    return items


# ----------------------------
# POSTING
# ----------------------------

def discord_post(item: Item):
    embed = {
        "title": item.title,
        "url": item.url,
        "description": item.summary[:300],
        "footer": {"text": f"Source: {item.source}"}
    }

    resp = requests.post(
        DISCORD_WEBHOOK_URL,
        json={"embeds": [embed]},
        timeout=15
    )
    resp.raise_for_status()


# ----------------------------
# MAIN
# ----------------------------

def main():
    state = {"seen_story_keys": []}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)

    all_items = []
    for f in FEEDS:
        try:
            fetched = fetch_feed(f["name"], f["url"])
            all_items.extend(fetched)
        except Exception as e:
            print(f"[WARN] Feed failed: {f['name']} -> {e}")

    eligible = []
    reasons = {}

    for item in all_items:
        r = block_reason(item.title, item.summary)
        if r:
            reasons[r] = reasons.get(r, 0) + 1
            continue

        if BREAKING_MODE and not is_breaking(
            item.title, item.summary, item.published_at
        ):
            continue

        eligible.append(item)

    clustered = {}
    for item in eligible:
        clustered.setdefault(item.story_key, []).append(item)

    final_items = [v[0] for v in clustered.values()]
    final_items = sorted(final_items, key=lambda x: x.source)

    posted = 0
    for item in final_items:
        if posted >= MAX_POSTS_PER_RUN:
            break

        if MODE != "DIGEST":
            if item.story_key in state.get("seen_story_keys", []):
                continue

        try:
            discord_post(item)
            posted += 1
            if MODE != "DIGEST":
                state.setdefault("seen_story_keys", []).append(item.story_key)
        except Exception as e:
            print(f"[ERROR] {item.title} -> {e}")

    if MODE != "DIGEST":
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)

    print("---- SUMMARY ----")
    print(f"Fetched: {len(all_items)}")
    print(f"Eligible: {len(eligible)}")
    print(f"After clustering: {len(final_items)}")
    print(f"Posted: {posted}")
    print(f"Blocked reasons: {reasons}")


if __name__ == "__main__":
    main()
