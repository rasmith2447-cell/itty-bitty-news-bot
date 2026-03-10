"""
shared.py — Itty Bitty Gaming News
Single source of truth for feeds, filtering, fetching, deduplication, and scoring.
Imported by both main.py (RAW/breaking) and digest.py (newsletter).
"""

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from rapidfuzz import fuzz

# ---------------------------------------------------------------------------
# FEEDS
# ---------------------------------------------------------------------------

FEEDS = [
    # Tier 1 — most reliable, highest signal
    {"name": "IGN",           "url": "http://feeds.ign.com/ign/all"},
    {"name": "GameSpot",      "url": "http://www.gamespot.com/feeds/mashup/"},
    {"name": "VGC",           "url": "https://www.videogameschronicle.com/category/news/feed/"},
    {"name": "Gematsu",       "url": "https://www.gematsu.com/feed"},
    # Tier 2 — strong signal
    {"name": "Polygon",       "url": "https://www.polygon.com/rss/news/index.xml"},
    {"name": "Eurogamer",     "url": "https://www.eurogamer.net/?format=rss&type=news"},
    {"name": "Rock Paper Shotgun", "url": "https://www.rockpapershotgun.com/feed/news"},
    {"name": "PC Gamer",      "url": "https://www.pcgamer.com/rss"},
    # Tier 3 — good supplemental coverage
    {"name": "Nintendo Life", "url": "https://www.nintendolife.com/feeds/latest"},
    {"name": "Push Square",   "url": "https://www.pushsquare.com/feeds/latest"},   # PlayStation focus
    {"name": "Pure Xbox",     "url": "https://www.purexbox.com/feeds/latest"},     # Xbox focus
    {"name": "GamesIndustry", "url": "https://www.gamesindustry.biz/feed"},
    {"name": "Blue's News",   "url": "https://www.bluesnews.com/news/news_1_0.rdf"},
    {"name": "Game Rant",     "url": "https://gamerant.com/feed/"},
]

# Source priority for deduplication clustering (lower index = preferred)
SOURCE_PRIORITY = [
    "IGN", "GameSpot", "VGC", "Gematsu",
    "Eurogamer", "Polygon", "Rock Paper Shotgun", "PC Gamer",
    "GamesIndustry", "Nintendo Life", "Push Square", "Pure Xbox",
    "Game Rant", "Blue's News",
]

# ---------------------------------------------------------------------------
# ENV HELPERS
# ---------------------------------------------------------------------------

def getenv(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return default if v is None else str(v).strip()


USER_AGENT    = getenv("USER_AGENT", "IttyBittyGamingNewsBot/3.0")
DEBUG         = getenv("DEBUG", "0") == "1"
STATE_FILE    = getenv("STATE_FILE", "state.json")

TITLE_FUZZY_THRESHOLD = int(getenv("TITLE_FUZZY_THRESHOLD", "92"))

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "utm_reader", "utm_referrer",
    "gclid", "fbclid", "mc_cid", "mc_eid", "ref", "source",
}

# ---------------------------------------------------------------------------
# FILTER TERM LISTS
# ---------------------------------------------------------------------------

GAME_TERMS = [
    "video game", "videogame", "game", "gaming",
    "xbox", "playstation", "ps5", "ps4", "nintendo", "switch",
    "steam", "epic games", "gog", "game pass",
    "pc gaming", "console", "handheld",
    "dlc", "expansion", "season pass", "battle pass",
    "patch", "hotfix", "update",
    "release date", "launch", "early access", "beta", "alpha", "demo",
    "studio", "developer", "publisher",
    "esports", "tournament",
]

ADJACENT_TERMS = [
    "gpu", "graphics card", "nvidia", "amd", "intel", "dlss", "fsr",
    "steam deck", "rog ally", "handheld pc",
    "unity engine", "unreal engine",
    "twitch", "youtube gaming",
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
    "opinion:", "editorial:", "commentary", "column:", "columns:",
    "feature:", "features:", "roundtable", "debate:", "discussion:",
    "hot take", "take:", "we asked", "letters", "mailbag",
    "poll:", "quiz:", "reader", "community",
    "i only needed", "my go-to", "when i can't", "i can't get",
    "i love", "i hate", "goat", "favorite", "favourite",
]

DEALS_BLOCK = [
    "deal", "deals", "sale", "discount", "save ",
    "coupon", "promo code", "price drop", "lowest price",
    "% off", "limited-time", "for just $", "for only $",
    "woot", "best buy", "walmart", "target", "newegg",
    "power bank", "charger", "usb-c",
]

RUMOR_BLOCK = [
    "rumor", "rumour", "leak", "leaked", "leaks",
    "speculation", "speculate", "reportedly", "allegedly",
    "unconfirmed", "according to sources", "insider",
]

NON_GAME_ENTERTAINMENT_BLOCK = [
    "movie", "film", " tv ", "television", "series", "episode",
    "netflix", "hulu", "disney+", "paramount", "hbo",
    "comic book", "dc comics", "marvel comics",
]

# ---------------------------------------------------------------------------
# KEYWORD LISTS FOR SCORING & TAGGING
# ---------------------------------------------------------------------------

BREAKING_KEYWORDS = [
    "shut down", "shutdown", "closed", "closing", "closure",
    "layoff", "layoffs", "laid off",
    "canceled", "cancelled",
    "delay", "delayed",
    "outage", "servers down", "service down",
    "security breach", "vulnerability",
    "price increase", "price hike",
    "acquisition", "acquired", "merger",
    "lawsuit", "sued",
    "retire", "retirement",
    "release date", "launch date", "launch",
    "patch", "hotfix",
    "announced", "announcement",
    "revealed", "reveal",
    "drops today", "available now", "out now", "live now",
    "shadow drop", "shadowdrop",
]

UPDATE_KEYWORDS = [
    "update", "updated", "new details", "more details", "confirmed",
    "statement", "responds", "clarifies", "patch", "hotfix",
]

# Score bonuses for digest ranking
SCORE_BONUSES: List[Tuple[List[str], int]] = [
    # (keyword list, bonus points)
    (["announced", "announcement", "revealed", "reveal"], 15),
    (["release date", "launch date", "out now", "available now", "shadow drop"], 20),
    (["shut down", "shutdown", "layoff", "layoffs", "canceled", "cancelled"], 25),
    (["delay", "delayed"], 18),
    (["acquisition", "acquired", "merger"], 20),
    (["patch", "hotfix", "update"], 8),
    (["lawsuit", "sued"], 15),
    (["price increase", "price hike"], 18),
]

# High-profile franchise/brand terms add signal
MARQUEE_TERMS = [
    "call of duty", "grand theft auto", "gta", "zelda", "mario", "metroid",
    "halo", "forza", "fable", "elder scrolls", "fallout", "starfield",
    "god of war", "spider-man", "horizon", "last of us", "ghost of tsushima",
    "final fantasy", "dragon quest", "monster hunter", "elden ring", "fromsoft",
    "cyberpunk", "witcher", "assassin's creed", "ubisoft", "ea sports",
    "minecraft", "fortnite", "apex legends", "valorant", "overwatch",
    "pokemon", "diablo", "world of warcraft", "wow",
]

# ---------------------------------------------------------------------------
# DATA MODEL
# ---------------------------------------------------------------------------

@dataclass
class Item:
    source: str
    title: str
    url: str
    published_at: datetime
    summary: str = ""
    image_url: str = ""
    story_key: str = ""
    score: int = 0         # computed digest relevance score
    tags: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# UTILITIES
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_url(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
        query = [
            (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS
        ]
        parsed = parsed._replace(
            query=urlencode(query, doseq=True),
            fragment="",
            netloc=parsed.netloc.lower(),
        )
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


def contains_any(hay: str, terms: List[str]) -> bool:
    h = hay.lower()
    return any(t.lower() in h for t in terms)


def has_money_signals(text: str) -> bool:
    return bool(re.search(r"(\$\d)|(\d+\s*%(\s*off)?)", text, re.IGNORECASE))


def game_or_adjacent(title: str, summary: str) -> bool:
    hay = f"{title} {summary}".lower()
    return contains_any(hay, GAME_TERMS) or contains_any(hay, ADJACENT_TERMS)


# ---------------------------------------------------------------------------
# FILTERING
# ---------------------------------------------------------------------------

def hard_block(title: str, summary: str) -> str:
    """
    Returns empty string if item passes all filters.
    Returns a reason string if it should be blocked.
    """
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
    # Only block entertainment if it has NO game signal at all
    if contains_any(hay, NON_GAME_ENTERTAINMENT_BLOCK) and not contains_any(hay, GAME_TERMS):
        return "NON_GAME_ENTERTAINMENT"

    return ""


def is_relevant(title: str, summary: str) -> bool:
    return hard_block(title, summary) == ""


def is_breaking(title: str, summary: str, published_at: datetime, max_age_hours: int = 72) -> bool:
    if utcnow() - published_at > timedelta(hours=max_age_hours):
        return False
    if not is_relevant(title, summary):
        return False
    hay = f"{title} {summary}".lower()
    return contains_any(hay, BREAKING_KEYWORDS)


def contains_update_keyword(title: str, summary: str) -> bool:
    hay = f"{title} {summary}".lower()
    return contains_any(hay, UPDATE_KEYWORDS)


# ---------------------------------------------------------------------------
# SCORING  (used by digest to rank the top-5 intelligently)
# ---------------------------------------------------------------------------

def compute_score(item: Item) -> int:
    """
    Score an item for digest relevance. Higher = more newsletter-worthy.
    Factors: recency, breaking signal, marquee brands, source tier.
    """
    score = 0
    hay = f"{item.title} {item.summary}".lower()

    # Recency bonus — decay over 24h
    age_hours = (utcnow() - item.published_at).total_seconds() / 3600
    if age_hours <= 2:
        score += 30
    elif age_hours <= 6:
        score += 20
    elif age_hours <= 12:
        score += 10
    elif age_hours <= 24:
        score += 5

    # Breaking/high-impact keyword bonuses
    for keywords, bonus in SCORE_BONUSES:
        if contains_any(hay, keywords):
            score += bonus

    # Marquee franchise mention
    if contains_any(hay, MARQUEE_TERMS):
        score += 12

    # Source tier bonus
    priority = {name: i for i, name in enumerate(SOURCE_PRIORITY)}
    tier = priority.get(item.source, len(SOURCE_PRIORITY))
    if tier <= 3:
        score += 10
    elif tier <= 7:
        score += 5

    # Penalise if no image (lower visual quality for newsletter)
    if not item.image_url:
        score -= 5

    return score


def topic_similarity(title_a: str, title_b: str) -> int:
    """
    Returns a fuzzy similarity score 0-100 between two titles.
    Used by digest to penalise stories covering the same topic.
    Strips common noise words first for a cleaner match.
    """
    noise = re.compile(
        r"\b(the|a|an|is|are|was|were|has|have|its|it|in|on|at|to|of|for|and|or|but|"
        r"with|new|first|last|final|latest|official|full|big|review|trailer|"
        r"video|watch|exclusive|breaking|report|says|get|gets|will|what|how|"
        r"why|who|when|where|that|this|these|those)\b",
        re.IGNORECASE,
    )
    a = re.sub(r"\s+", " ", noise.sub(" ", title_a.lower())).strip()
    b = re.sub(r"\s+", " ", noise.sub(" ", title_b.lower())).strip()
    return fuzz.token_set_ratio(a, b)



# ---------------------------------------------------------------------------
# TAGGING
# ---------------------------------------------------------------------------

def make_tags(title: str, summary: str) -> List[str]:    hay = f"{title} {summary}".lower()
    tags: List[str] = []

    tag_rules = [
        (["announced", "announcement", "revealed", "reveal", "debut", "premiere"], "📣 ANNOUNCEMENT"),
        (["drops today", "available now", "out now", "live now", "shadow drop", "shadowdrop"], "🚀 OUT NOW"),
        (["patch", "hotfix"], "🔧 PATCH"),
        (["update"], "🔄 UPDATE"),
        (["delay", "delayed"], "⏳ DELAY"),
        (["layoff", "layoffs", "laid off"], "💼 LAYOFFS"),
        (["shut down", "shutdown", "closed", "closing", "closure"], "🔒 SHUTDOWN"),
        (["acquisition", "acquired", "merger"], "🤝 M&A"),
        (["lawsuit", "sued"], "⚖️ LEGAL"),
        (["retire", "retirement"], "🎖️ RETIREMENT"),
        (["price increase", "price hike"], "💸 PRICE CHANGE"),
        (["release date", "launch date"], "📅 DATE CONFIRMED"),
        (["free", "free to play", "f2p"], "🆓 FREE"),
    ]

    for keywords, label in tag_rules:
        if contains_any(hay, keywords):
            tags.append(label)

    # Platform tags
    platform_rules = [
        (["playstation", "ps5", "ps4"], "🎮 PlayStation"),
        (["xbox", "game pass"], "🟢 Xbox"),
        (["nintendo", "switch"], "🔴 Nintendo"),
        (["steam", "pc gaming", " pc "], "🖥️ PC"),
        (["mobile", "ios", "android"], "📱 Mobile"),
    ]
    for keywords, label in platform_rules:
        if contains_any(hay, keywords):
            tags.append(label)

    # Deduplicate preserving order
    seen, out = set(), []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:6]


# ---------------------------------------------------------------------------
# STORY KEY / DEDUPLICATION
# ---------------------------------------------------------------------------

def make_story_key(title: str) -> str:
    t = re.sub(r"https?://\S+", "", title.lower())
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return hashlib.sha1(t.encode("utf-8")).hexdigest()


def pick_best_source(cluster: List[Item]) -> Item:
    priority = {name: i for i, name in enumerate(SOURCE_PRIORITY)}
    return sorted(
        cluster,
        key=lambda x: (priority.get(x.source, 999), -x.published_at.timestamp()),
    )[0]


def cluster_items(items: List[Item]) -> List[Item]:
    """Group by story_key, pick the best source per cluster."""
    buckets: Dict[str, List[Item]] = {}
    for it in items:
        buckets.setdefault(it.story_key, []).append(it)
    chosen = [pick_best_source(group) for group in buckets.values()]
    chosen.sort(key=lambda x: x.published_at, reverse=True)
    return chosen


# ---------------------------------------------------------------------------
# STATE  (RAW/breaking deduplication)
# ---------------------------------------------------------------------------

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
    for key in ("seen_urls", "seen_story_keys", "seen_titles"):
        state[key] = state[key][-5000:]


# ---------------------------------------------------------------------------
# FEED FETCHING
# ---------------------------------------------------------------------------

def safe_parse_date(entry) -> datetime:
    for attr in ("published_parsed", "updated_parsed"):
        st = getattr(entry, attr, None)
        if st:
            try:
                return datetime.fromtimestamp(time.mktime(st), tz=timezone.utc)
            except Exception:
                pass
    for key in ("published", "updated", "created", "date"):
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


def extract_from_entry(entry) -> Tuple[str, str]:
    """Extract summary text and image URL from a feed entry."""
    summary = ""
    for key in ("summary", "description", "subtitle"):
        val = getattr(entry, key, None)
        if val:
            summary = strip_html(val)
            break

    image_url = ""
    for attr in ("media_content", "media_thumbnail"):
        media = getattr(entry, attr, None)
        if media and isinstance(media, list):
            for m in media:
                u = (m.get("url") or "").strip()
                if u:
                    image_url = u
                    break
        if image_url:
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
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception:
        return "", ""

    def meta(name: str) -> str:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        return (tag.get("content") or "").strip() if tag else ""

    desc = meta("og:description") or meta("description") or meta("twitter:description")
    img  = meta("og:image") or meta("twitter:image") or meta("twitter:image:src")
    return strip_html(desc), img.strip()


def fetch_feed(feed_name: str, feed_url: str) -> List[Item]:
    headers = {"User-Agent": USER_AGENT}
    resp = requests.get(feed_url, headers=headers, timeout=20)
    resp.raise_for_status()

    parsed = feedparser.parse(resp.text)
    items: List[Item] = []

    for entry in parsed.entries[:200]:
        title = (getattr(entry, "title", "") or "").strip()
        link  = (getattr(entry, "link",  "") or "").strip()
        if not title or not link:
            continue

        url          = normalize_url(link)
        published_at = safe_parse_date(entry)
        summary, img = extract_from_entry(entry)
        tags         = make_tags(title, summary)

        items.append(Item(
            source=feed_name,
            title=title,
            url=url,
            published_at=published_at,
            summary=summary,
            image_url=img,
            story_key=make_story_key(title),
            tags=tags,
        ))

    return items


def fetch_all_feeds(
    feed_list: Optional[List[Dict]] = None,
    breaking_mode: bool = False,
    breaking_max_age_hours: int = 72,
) -> Tuple[List[Item], Dict[str, int]]:
    """
    Fetch all feeds, apply filters, cluster duplicates.
    Returns (clustered_items, filter_reason_counts).

    breaking_mode=True:
      - Skips hard_block (so rumor/opinion filters don't kill breaking stories)
      - Only keeps items that pass is_breaking() — must have a breaking keyword
        AND be within breaking_max_age_hours
      - Game/adjacent check is still enforced so pure non-gaming stories are excluded

    breaking_mode=False (default / RAW):
      - Full hard_block filter pipeline
    """
    feed_list = feed_list or FEEDS
    raw_items: List[Item] = []

    for f in feed_list:
        try:
            raw_items.extend(fetch_feed(f["name"], f["url"]))
            if DEBUG:
                print(f"[DEBUG] Fetched {f['name']}: OK")
        except Exception as e:
            print(f"[WARN] Feed fetch failed: {f['name']} -> {e}")

    reasons: Dict[str, int] = {}
    filtered: List[Item] = []

    for it in raw_items:
        if breaking_mode:
            # Must be game/adjacent first
            if not game_or_adjacent(it.title, it.summary):
                reasons["NOT_GAME_OR_ADJACENT"] = reasons.get("NOT_GAME_OR_ADJACENT", 0) + 1
                continue
            # Must have a breaking keyword and be recent enough
            if is_breaking(it.title, it.summary, it.published_at, breaking_max_age_hours):
                filtered.append(it)
            else:
                r = "NOT_BREAKING_KEYWORD_OR_TOO_OLD"
                reasons[r] = reasons.get(r, 0) + 1
        else:
            r = hard_block(it.title, it.summary)
            if r == "":
                filtered.append(it)
            else:
                reasons[r] = reasons.get(r, 0) + 1

    clustered = cluster_items(filtered)
    return clustered, reasons


# ---------------------------------------------------------------------------
# DISCORD HELPERS
# ---------------------------------------------------------------------------

def discord_post_raw(item: Item, webhook_url: str) -> None:
    """Post a single news item as a Discord embed (RAW / breaking mode)."""
    summary   = item.summary or ""
    image_url = item.image_url or ""

    if not summary or not image_url:
        og_desc, og_img = fetch_open_graph(item.url)
        if not summary and og_desc:
            summary = og_desc
        if not image_url and og_img:
            image_url = og_img

    summary = shorten(summary, 320)

    embed: Dict = {
        "title":     item.title,
        "url":       item.url,
        "timestamp": item.published_at.isoformat(),
        "footer":    {"text": f"📰 {item.source}"},
    }
    if summary:
        embed["description"] = summary
    if item.tags:
        embed["fields"] = [{
            "name":   "Tags",
            "value":  "  ".join(item.tags),
            "inline": False,
        }]
    if image_url:
        embed["image"] = {"url": image_url}

    resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=20)
    resp.raise_for_status()


def post_webhook(webhook_url: str, content: str = "", embeds: Optional[List[Dict]] = None) -> None:
    """Generic webhook post used by the digest."""
    payload: Dict = {}
    if content:
        payload["content"] = content
    if embeds:
        payload["embeds"] = embeds[:10]
    resp = requests.post(webhook_url, json=payload, timeout=20)
    resp.raise_for_status()
