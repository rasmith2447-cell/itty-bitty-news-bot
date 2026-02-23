import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Tuple
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from zoneinfo import ZoneInfo

# ----------------------------
# DIGEST CONFIG
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
USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNews/Digest2.8")

WINDOW_HOURS = int(os.getenv("DIGEST_WINDOW_HOURS", "24"))
TOP_N = int(os.getenv("DIGEST_TOP_N", "5"))
MAX_PER_SOURCE = int(os.getenv("DIGEST_MAX_PER_SOURCE", "2"))

# Optional Featured Video (set these in workflow env when you're ready)
FEATURED_VIDEO_URL = os.getenv("FEATURED_VIDEO_URL", "").strip()
FEATURED_VIDEO_TITLE = os.getenv("FEATURED_VIDEO_TITLE", "Featured Video").strip()

# Discord safety
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

# NOTE: We intentionally removed the generic word "game" from the keyword list
# to avoid false positives (e.g., "endgame", "mind game", "game changer", etc.)
# We'll treat gaming coverage as platform/publisher/studio/game-title signals instead.
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
    "playstation studios", "bluepoint",
    "ubisoft", "ea", "activision", "blizzard", "bethesda", "capcom", "bandai namco",
    "square enix", "sega", "take-two", "2k", "rockstar", "valve",
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
    "i only needed", "my go-to", "when i can't", "i can't get",
    "goat", "goats", "favorite", "favourite", "most popular to cosplay", "cosplay?",
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

# Strong non-gaming entertainment / theme park / animation stuff: block ALWAYS.
NON_GAMING_ENTERTAINMENT_BLOCK = [
    "walt disney world", "disney world", "disneyland", "disney's hollywood studios",
    "audio-animatronics", "animation academy", "olaf", "frozen",
    "theme park", "theme-park", "ride", "attraction",
    "movie", "film", "tv", "television", "series", "episode",
    "netflix", "hulu", "disney", "disney+", "paramount", "max", "hbo",
    "comic", "comics", "dc ", "marvel", "green arrow", "catwoman",
    "anime",
]

NEWS_HINTS = [
    "announced", "announcement", "revealed", "reveal",
    "launch", "release date", "out now", "available now", "live now",
    "delay", "delayed", "layoff", "layoffs",
    "shutdown", "closed", "acquisition", "acquired", "merger",
    "lawsuit", "sued",
    "patch", "hotfix", "update",
]

# ----------------------------
# UTIL
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
    if "<" not in text and ">" not in text and "&" not in text:
        return re.sub(r"\s+", " ", text).strip()
    soup = BeautifulSoup(text, "html.parser")
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
    """
    Lightweight heuristic:
    - Titles that include a colon or dash often indicate a game title/subtitle
    - e.g., "Elden Ring: Shadow of the Erdtree" or "Hades II - Patch Notes"
    """
    t = title.strip()
    if len(t) < 12:
        return False
    return (":" in t) or (" - " in t)

def game_or_adjacent(title: str, summary: str) -> bool:
    hay = f"{title} {summary}".lower()

    # hard-kill obvious non-gaming entertainment/theme-park content
    if contains_any(hay, NON_GAMING_ENTERTAINMENT_BLOCK):
        return False

    # match stronger gaming terms OR adjacent tech terms
    if contains_any(hay, GAME_TERMS) or contains_any(hay, ADJACENT_TERMS):
        return True

    # allow some titles that look like real game-news headings
    if looks_like_a_specific_game_title(title):
        # but still must not be entertainment/theme park
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

        out.append({
            "source": feed_name,
            "title": title,
            "url": url,
            "published_at": published_at,
            "summary": summary,
            "image_url": image_url,
        })
    return out

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
        return f"{source} dropped an update on this — hit the source link for full details."
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

def story_emoji(title: str, summary: str) -> str:
    hay = f"{title} {summary}".lower()
    if contains_any(hay, ["security", "breach", "hack", "bomb threat", "threat", "evacuated", "evacuation"]):
        return "🚨"
    if contains_any(hay, ["lawsuit", "sued", "court", "supreme court", "judge", "ruling", "tariff"]):
        return "⚖️"
    if contains_any(hay, ["retire", "retirement", "steps down", "stepping down", "resigns", "resignation", "president", "ceo"]):
        return "🧑‍💼"
    if contains_any(hay, ["delay", "delayed"]):
        return "⏳"
    if contains_any(hay, ["patch", "hotfix", "update"]):
        return "🛠️"
    if contains_any(hay, ["announced", "announcement", "revealed", "reveal", "debut", "premiere"]):
        return "🎬"
    if contains_any(hay, ["out now", "available now", "live now", "drops", "shadow drop", "shadowdrop"]):
        return "🟢"
    if contains_any(hay, ["xbox", "game pass"]):
        return "🟩"
    if contains_any(hay, ["playstation", "ps5", "ps4"]):
        return "🟦"
    if contains_any(hay, ["nintendo", "switch"]):
        return "🔴"
    if contains_any(hay, ["pc", "steam"]):
        return "💻"
    return "🎮"

# ----------------------------
# SCORING + VARIETY SELECTION
# ----------------------------

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

def choose_with_variety(candidates: List[Dict], top_n: int, max_per_source: int) -> List[Dict]:
    by_source: Dict[str, List[Dict]] = {}
    for it in candidates:
        by_source.setdefault(it["source"], []).append(it)

    for s in by_source:
        by_source[s].sort(key=score_item, reverse=True)

    picked: List[Dict] = []
    used_title_keys = set()
    counts: Dict[str, int] = {}

    # pass 1: one per source in priority order
    for s in SOURCE_PRIORITY:
        if len(picked) >= top_n:
            break
        if s not in by_source or not by_source[s]:
            continue
        it = by_source[s][0]
        k = normalize_title_key(it["title"])
        if k in used_title_keys:
            continue
        picked.append(it)
        used_title_keys.add(k)
        counts[s] = counts.get(s, 0) + 1

    # pass 2: fill remaining by score, respecting caps
    if len(picked) < top_n:
        remaining = sorted(candidates, key=score_item, reverse=True)
        for it in remaining:
            if len(picked) >= top_n:
                break
            s = it["source"]
            if counts.get(s, 0) >= max_per_source:
                continue
            k = normalize_title_key(it["title"])
            if k in used_title_keys:
                continue
            picked.append(it)
            used_title_keys.add(k)
            counts[s] = counts.get(s, 0) + 1

    return sorted(picked, key=score_item, reverse=True)

# ----------------------------
# DISCORD POSTING (safe split)
# ----------------------------

def post_to_discord(content: str, embeds: List[Dict]) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL is not set")

    content = (content or "").strip()
    parts = []
    remaining = content

    while remaining:
        if len(remaining) <= DISCORD_SAFE_CONTENT:
            parts.append(remaining)
            break
        cut = remaining.rfind("\n\n", 0, DISCORD_SAFE_CONTENT)
        if cut == -1 or cut < 200:
            cut = DISCORD_SAFE_CONTENT
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()

    payload1 = {"content": parts[0] if parts else ""}
    if embeds:
        payload1["embeds"] = embeds

    r1 = requests.post(DISCORD_WEBHOOK_URL, json=payload1, timeout=20)
    r1.raise_for_status()

    for p in parts[1:]:
        r = requests.post(DISCORD_WEBHOOK_URL, json={"content": p}, timeout=20)
        r.raise_for_status()

# ----------------------------
# MAIN
# ----------------------------

def main():
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL is not set")

    cutoff = utcnow() - timedelta(hours=WINDOW_HOURS)

    items: List[Dict] = []
    for f in FEEDS:
        try:
            items.extend(fetch_feed(f["name"], f["url"]))
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

    ranked = choose_with_variety(deduped, TOP_N, MAX_PER_SOURCE)

    for idx, it in enumerate(ranked):
        if not it["summary"] or not it["image_url"]:
            desc, img = fetch_open_graph(it["url"])
            if not it["summary"] and desc:
                it["summary"] = desc
            if not it["image_url"] and img:
                it["image_url"] = img
        it["summary"] = build_story_summary(strip_html(it["summary"]), it["source"], featured=(idx == 0))

    pn = pacific_now()
    date_line = pn.strftime("%B %d, %Y")

    header = f"{date_line}\n\n**In Tonight’s Edition of Itty Bitty Gaming News…**\n"

    if not ranked:
        content = header + "\n► 🎮 Quiet night — nothing cleared the news-only filter.\n\nThat’s it for tonight’s Itty Bitty. 🫡"
        post_to_discord(content, [])
        print("Newsletter digest posted. Items: 0")
        return

    teaser = []
    for it in ranked[:3]:
        emoji = story_emoji(it["title"], it["summary"])
        teaser.append(f"► {emoji} {it['title']}")

    hook = (
        "\n\nOkay, gamers… today did *not* chill. Here are the headlines worth your attention.\n"
    )

    featured = ranked[0]
    featured_block = (
        "\n\n**FEATURED STORY**\n"
        f"**{featured['title']}**\n"
        f"{featured['summary']}\n"
        f"Source: {md_link(featured['source'], featured['url'])}\n"
    )

    top_stories_block = "\n**Tonight’s Top Stories**\n"
    for i, it in enumerate(ranked[1:], start=2):
        top_stories_block += (
            f"\n**{i}) {it['title']}**\n"
            f"{it['summary']}\n"
            f"Source: {md_link(it['source'], it['url'])}\n"
        )

    featured_video_block = ""
    if FEATURED_VIDEO_URL:
        featured_video_block = (
            "\n**📺 Featured Video**\n"
            f"{md_link(FEATURED_VIDEO_TITLE, FEATURED_VIDEO_URL)}\n"
        )

    outro = (
        "\n—\n"
        "That’s it for tonight’s Itty Bitty. 😄\n"
        "Catch the snackable breakdown on **Itty Bitty Gaming News** tomorrow.\n"
    )

    content = header + "\n".join(teaser) + hook + featured_block + top_stories_block + featured_video_block + "\n" + outro

    embeds = []
    for i, it in enumerate(ranked, start=1):
        embed = {
            "title": f"{i}) {it['title']}",
            "url": it["url"],
            "description": shorten(it["summary"], EMBED_DESC_LIMIT),
            "footer": {"text": f"Source: {it['source']}"},
            "timestamp": it["published_at"].isoformat(),
        }
        if it.get("image_url"):
            embed["image"] = {"url": it["image_url"]}
        embeds.append(embed)

    post_to_discord(content, embeds)
    print(f"Newsletter digest posted. Items: {len(embeds)}")

if __name__ == "__main__":
    main()
