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
# BEGINNER CONFIG SECTION
# ----------------------------

FEEDS = [
    {"name": "IGN", "url": "http://feeds.ign.com/ign/all"},
    {"name": "GameSpot", "url": "http://www.gamespot.com/feeds/mashup/"},
    {"name": "Blue's News", "url": "https://www.bluesnews.com/news/news_1_0.rdf"},
]

SOURCE_PRIORITY = ["IGN", "GameSpot", "Blue's News"]

MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "12"))
TITLE_FUZZY_THRESHOLD = int(os.getenv("TITLE_FUZZY_THRESHOLD", "92"))

BREAKING_MODE = os.getenv("BREAKING_MODE", "0").strip() == "1"
# Make breaking slightly less brittle; still strict on keywords + relevance
BREAKING_MAX_AGE_HOURS = int(os.getenv("BREAKING_MAX_AGE_HOURS", "72"))

GAME_TERMS = [
    "video game", "videogame", "game", "gaming",
    "xbox", "playstation", "ps5", "ps4", "nintendo", "switch",
    "steam", "epic games", "gog", "game pass",
    "pc gaming", "console", "handheld",
    "dlc", "expansion", "season", "battle pass",
    "patch", "update", "hotfix",
    "release date", "launch", "early access", "beta", "alpha", "demo",
    "studio", "developer", "publisher",
    "esports", "tournament",
    "bluepoint", "playstation studios",
]

ADJACENT_TERMS = [
    "gpu", "graphics card", "nvidia", "amd", "intel", "driver", "dlss", "fsr",
    "steam deck", "rog ally", "handheld pc",
    "unity", "unreal engine", "unreal",
    "discord", "twitch", "youtube gaming", "streaming",
    "vr", "virtual reality", "meta quest",
]

# HARD BLOCKS
LISTICLE_GUIDE_BLOCK = [
    "best ", "top ", "ranked", "ranking", "tier list",
    "everything you need to know", "explained",
    "review", "preview", "impressions",
    "guide", "walkthrough", "tips", "tricks",
]

# Evergreen/SEO refresh content (your “History of Resident Evil (2026 Update)” case)
EVERGREEN_BLOCK = [
    "history of", "timeline", "retrospective", "complete history",
    "recap", "ending explained", "lore", "beginner's guide",
    "what we know so far",
]

DEALS_BLOCK = [
    "deal", "deals", "sale", "discount", "save ",
    "coupon", "promo code", "price drop", "drops to", "lowest price",
    "now %", "% off", "off)", "limited-time",
    "for just $", "for only $",
    "woot", "amazon", "best buy", "walmart", "target", "newegg",
    # common deal-item terms that leak in
    "power bank", "mAh", "charger", "charging", "usb-c",
]

RUMOR_BLOCK = [
    "rumor", "rumour", "leak", "leaked", "leaks",
    "speculation", "speculate", "reportedly", "allegedly",
    "unconfirmed", "according to sources", "insider",
]

NON_GAME_ENTERTAINMENT_BLOCK = [
    "movie", "film", "tv", "television", "series", "episode",
    "netflix", "hulu", "disney", "paramount", "max", "hbo",
    "comic", "comics", "dc ", "marvel", "green arrow", "catwoman",
    "anime",
]

BREAKING_KEYWORDS = [
    "shut down", "shutdown", "closed", "closing", "closure",
    "layoff", "layoffs",
    "canceled", "cancelled",
    "delay", "delayed",
    "release date", "launch date", "launch",
    "patch", "hotfix", "update",
    "outage", "servers down", "service down",
    "security", "breach", "vulnerability",
    "price increase", "price hike",
    "acquisition", "acquired", "merger",
    "lawsuit", "sued",
    "retire", "retirement",
]

UPDATE_KEYWORDS = [
    "update", "updated", "new details", "more details", "confirmed",
    "statement", "responds", "clarifies", "patch", "hotfix",
]

STATE_FILE = "state.json"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNewsBot/1.5")

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
    story_key: str = ""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


def shorten(text: str, max_len: int = 320) -> str:
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
    state.setdefault("seen_story_keys", [])
    return state


def save_state(state: Dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def contains_any(hay: str, terms: List[str]) -> bool:
    return any(t.lower() in hay for t in terms)


def game_or_adjacent(title: str, summary: str) -> bool:
    hay = f"{title} {summary}".lower()
    return contains_any(hay, GAME_TERMS) or contains_any(hay, ADJACENT_TERMS)


def has_money_signals(text: str) -> bool:
    # catches: $39.99, 60% Off, etc.
    return bool(re.search(r"(\$\d)|(\d+\s*%(\s*off)?)", text, flags=re.IGNORECASE))


def hard_block(title: str, summary: str) -> bool:
    hay = f"{title} {summary}".lower()

    # kill listicles/guides/reviews
    if contains_any(hay, LISTICLE_GUIDE_BLOCK):
        return True

    # kill evergreen/SEO refresh content
    if contains_any(hay, EVERGREEN_BLOCK):
        return True

    # kill deals/shopping content (including money signals)
    if contains_any(hay, DEALS_BLOCK) or has_money_signals(hay):
        return True

    # kill rumors/speculation
    if contains_any(hay, RUMOR_BLOCK):
        return True

    # kill non-game entertainment unless it’s clearly game/adjacent
    if contains_any(hay, NON_GAME_ENTERTAINMENT_BLOCK) and not game_or_adjacent(title, summary):
        return True

    return False


def is_relevant(title: str, summary: str) -> bool:
    if not game_or_adjacent(title, summary):
        return False
    if hard_block(title, summary):
        return False
    return True


def is_breaking(title: str, summary: str, published_at: datetime) -> bool:
    if utcnow() - published_at > timedelta(hours=BREAKING_MAX_AGE_HOURS):
        return False
    if not is_relevant(title, summary):
        return False

    hay = f"{title} {summary}".lower()
    if not contains_any(hay, BREAKING_KEYWORDS):
        return False

    return True


def contains_update_keyword(title: str, summary: str) -> bool:
    hay = f"{title} {summary}".lower()
    return contains_any(hay, UPDATE_KEYWORDS)


def make_story_key(title: str) -> str:
    t = title.lower()
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
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
            story_key=make_story_key(title),
        ))

    return items


def pick_best_source(cluster: List[Item]) -> Item:
    priority = {name: i for i, name in enumerate(SOURCE_PRIORITY)}
    return sorted(cluster, key=lambda x: (priority.get(x.source, 999), -x.published_at.timestamp()))[0]


def cluster_items(items: List[Item]) -> List[Item]:
    buckets: Dict[str, List[Item]] = {}
    for it in items:
        buckets.setdefault(it.story_key, []).append(it)
    chosen = [pick_best_source(group) for group in buckets.values()]
    chosen.sort(key=lambda x: x.published_at, reverse=True)
    return chosen


def is_duplicate_or_allowed_update(item: Item, state: Dict) -> bool:
    if item.url in state["seen_urls"]:
        return True

    is_update = contains_update_keyword(item.title, item.summary)

    if item.story_key in state["seen_story_keys"] and not is_update:
        return True

    title_norm = re.sub(r"\s+", " ", item.title.strip().lower())
    for seen in state["seen_titles"][-500:]:
        if fuzz.ratio(title_norm, seen) >= TITLE_FUZZY_THRESHOLD and not is_update:
            return True
    return False


def remember(item: Item, state: Dict) -> None:
    state["seen_urls"].append(item.url)
    state["seen_story_keys"].append(item.story_key)
    state["seen_titles"].append(re.sub(r"\s+", " ", item.title.strip().lower()))

    state["seen_urls"] = state["seen_urls"][-5000:]
    state["seen_story_keys"] = state["seen_story_keys"][-5000:]
    state["seen_titles"] = state["seen_titles"][-5000:]


def discord_post(item: Item) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL is not set")

    summary = item.summary or ""
    image_url = item.image_url or ""

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

    resp = requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=20)
    resp.raise_for_status()


def main():
    state = load_state()

    all_items: List[Item] = []
    for f in FEEDS:
        try:
            all_items.extend(fetch_feed(f["name"], f["url"]))
        except Exception as e:
            print(f"[WARN] Feed fetch failed: {f['name']} -> {e}")

    filtered: List[Item] = []
    for it in all_items:
        if BREAKING_MODE:
            if is_breaking(it.title, it.summary, it.published_at):
                filtered.append(it)
        else:
            if is_relevant(it.title, it.summary):
                filtered.append(it)

    clustered = cluster_items(filtered)

    posted = 0
    for item in clustered:
        if posted >= MAX_POSTS_PER_RUN:
            break
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
    print(f"Done. Posted {posted} item(s). BREAKING_MODE={BREAKING_MODE}")


if __name__ == "__main__":
    main()
