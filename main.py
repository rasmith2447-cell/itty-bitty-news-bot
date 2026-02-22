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

MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "12"))
TITLE_FUZZY_THRESHOLD = int(os.getenv("TITLE_FUZZY_THRESHOLD", "92"))

MODE = os.getenv("MODE", "RAW").strip().upper()          # RAW | DIGEST
SKIP_STATE_UPDATE = os.getenv("SKIP_STATE_UPDATE", "0").strip() == "1"
DEBUG = os.getenv("DEBUG", "0").strip() == "1"

BREAKING_MODE = os.getenv("BREAKING_MODE", "0").strip() == "1"
BREAKING_MAX_AGE_HOURS = int(os.getenv("BREAKING_MAX_AGE_HOURS", "72"))

STATE_FILE = "state.json"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNewsBot/2.2")

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_reader", "utm_referrer",
    "gclid", "fbclid", "mc_cid", "mc_eid", "ref", "source"
}

# ----------------------------
# FILTER TERMS
# ----------------------------

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
    "playstation studios", "bluepoint",
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

# NEW: block community/opinion/mailbag/polls (Nintendo Life “Mailbox/Letters/Poll” stuff)
COMMUNITY_OPINION_BLOCK = [
    "poll:", "poll -", "poll —", "poll ",
    "mailbox:", "letters", "letter:", "community",
    "what's your favourite", "what's your favorite",
    "favourite", "favorite gen", "which is your",
    "fun with strangers", "sterility",  # Nintendo Life Letters recurring titles
    "quiz:", "commentary", "opinion:", "editorial:",
]

DEALS_BLOCK = [
    "deal", "deals", "sale", "discount", "save ",
    "coupon", "promo code", "price drop", "drops to", "lowest price",
    "now %", "% off", "off)", "limited-time",
    "for just $", "for only $",
    "woot", "amazon", "best buy", "walmart", "target", "newegg",
    "power bank", "mah", "charger", "charging", "usb-c",
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
    "outage", "servers down", "service down",
    "security", "breach", "vulnerability",
    "price increase", "price hike",
    "acquisition", "acquired", "merger",
    "lawsuit", "sued",
    "retire", "retirement",
    "release date", "launch date", "launch",
    "patch", "hotfix", "update",
    "announced", "announcement",
    "revealed", "reveal",
    "debut", "premiere",
    "drops today", "drops", "available now", "out now", "live now",
    "shadow drop", "shadowdrop",
]

UPDATE_KEYWORDS = [
    "update", "updated", "new details", "more details", "confirmed",
    "statement", "responds", "clarifies", "patch", "hotfix",
]

# ----------------------------
# DATA
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
    if "<" not in text and ">" not in text and "&" not in text:
        return re.sub(r"\s+", " ", text).strip()
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
    h = hay.lower()
    return any(t.lower() in h for t in terms)


def has_money_signals(text: str) -> bool:
    return bool(re.search(r"(\$\d)|(\d+\s*%(\s*off)?)", text, flags=re.IGNORECASE))


def game_or_adjacent(title: str, summary: str) -> bool:
    hay = f"{title} {summary}".lower()
    return contains_any(hay, GAME_TERMS) or contains_any(hay, ADJACENT_TERMS)


def hard_block(title: str, summary: str) -> str:
    hay = f"{title} {summary}".lower()

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
    if contains_any(hay, NON_GAME_ENTERTAINMENT_BLOCK) and not game_or_adjacent(title, summary):
        return "NON_GAME_ENTERTAINMENT"
    return ""


def is_relevant(title: str, summary: str) -> bool:
    return hard_block(title, summary) == ""


def is_breaking(title: str, summary: str, published_at: datetime) -> bool:
    if utcnow() - published_at > timedelta(hours=BREAKING_MAX_AGE_HOURS):
        return False
    if not is_relevant(title, summary):
        return False
    hay = f"{title} {summary}".lower()
    return contains_any(hay, BREAKING_KEYWORDS)


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
    for entry in parsed.entries[:200]:
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


def make_tags(title: str, summary: str) -> List[str]:
    hay = f"{title} {summary}".lower()
    tags: List[str] = []

    if contains_any(hay, ["announced", "announcement", "revealed", "reveal", "debut", "premiere"]):
        tags.append("ANNOUNCEMENT")
    if contains_any(hay, ["drops today", "available now", "out now", "live now", "shadow drop", "shadowdrop"]):
        tags.append("DROP")
    if contains_any(hay, ["patch", "hotfix", "update"]):
        tags.append("PATCH")
    if contains_any(hay, ["delay", "delayed"]):
        tags.append("DELAY")
    if contains_any(hay, ["layoff", "layoffs"]):
        tags.append("LAYOFFS")
    if contains_any(hay, ["shut down", "shutdown", "closed", "closing", "closure"]):
        tags.append("SHUTDOWN")
    if contains_any(hay, ["acquisition", "acquired", "merger"]):
        tags.append("M&A")
    if contains_any(hay, ["lawsuit", "sued"]):
        tags.append("LEGAL")
    if contains_any(hay, ["retire", "retirement"]):
        tags.append("RETIREMENT")
    if contains_any(hay, ["outage", "servers down", "service down"]):
        tags.append("OUTAGE")
    if contains_any(hay, ["security", "breach", "vulnerability"]):
        tags.append("SECURITY")

    if contains_any(hay, ["playstation", "ps5", "ps4"]):
        tags.append("PLAYSTATION")
    if contains_any(hay, ["xbox", "game pass"]):
        tags.append("XBOX")
    if contains_any(hay, ["nintendo", "switch"]):
        tags.append("NINTENDO")
    if contains_any(hay, ["steam", "pc gaming", " pc "]):
        tags.append("PC")

    if contains_any(hay, ["nvidia", "amd", "intel", "gpu", "graphics card", "driver", "dlss", "fsr"]):
        tags.append("HARDWARE")
    if contains_any(hay, ["unreal", "unity", "engine"]):
        tags.append("DEV/ENGINE")
    if contains_any(hay, ["esports", "tournament", "championship"]):
        tags.append("ESPORTS")
    if contains_any(hay, ["steam deck", "rog ally", "handheld pc"]):
        tags.append("HANDHELD")

    out, seen = [], set()
    for t in tags:
        if t not in seen:
            out.append(t)
            seen.add(t)
    return out[:8]


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
    tags = make_tags(item.title, summary)

    embed = {
        "title": item.title,
        "url": item.url,
        "timestamp": item.published_at.isoformat(),
        "footer": {"text": f"Source: {item.source}"},
    }
    if summary:
        embed["description"] = summary
    if tags:
        embed["fields"] = [{
            "name": "Tags",
            "value": " ".join([f"`{t}`" for t in tags]),
            "inline": False
        }]
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

    reasons: Dict[str, int] = {}
    filtered: List[Item] = []

    for it in all_items:
        if BREAKING_MODE:
            if is_breaking(it.title, it.summary, it.published_at):
                filtered.append(it)
            else:
                r = hard_block(it.title, it.summary) or "NOT_BREAKING_KEYWORD_OR_TOO_OLD"
                reasons[r] = reasons.get(r, 0) + 1
        else:
            r = hard_block(it.title, it.summary)
            if r == "":
                filtered.append(it)
            else:
                reasons[r] = reasons.get(r, 0) + 1

    clustered = cluster_items(filtered)

    posted = 0
    skipped_dupe = 0

    for item in clustered:
        if posted >= MAX_POSTS_PER_RUN:
            break

        if MODE != "DIGEST":
            if is_duplicate_or_allowed_update(item, state):
                skipped_dupe += 1
                continue

        try:
            discord_post(item)
            posted += 1
            print(f"[POSTED] {item.source}: {item.title}")

            if MODE != "DIGEST" and not SKIP_STATE_UPDATE:
                remember(item, state)

        except Exception as e:
            print(f"[ERROR] Post failed: {item.title} -> {e}")

    if MODE != "DIGEST" and not SKIP_STATE_UPDATE:
        save_state(state)

    print("---- SUMMARY ----")
    print(f"MODE={MODE} BREAKING_MODE={BREAKING_MODE}")
    print(f"Eligible after filters: {len(filtered)}")
    print(f"After clustering: {len(clustered)}")
    print(f"Skipped duplicates (RAW only): {skipped_dupe}")
    if reasons:
        top = sorted(reasons.items(), key=lambda x: x[1], reverse=True)[:10]
        print("Top filter reasons:")
        for k, v in top:
            print(f"  - {k}: {v}")
    print(f"Done. Posted {posted} item(s).")
    if DEBUG:
        print("DEBUG enabled.")


if __name__ == "__main__":
    main()
