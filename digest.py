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

# Featured video (Adilo)
FEATURED_VIDEO_TITLE = os.getenv("FEATURED_VIDEO_TITLE", "Watch today’s Itty Bitty Gaming News").strip()
FEATURED_VIDEO_FALLBACK_URL = os.getenv(
    "FEATURED_VIDEO_FALLBACK_URL",
    "https://adilo.bigcommand.com/c/ittybittygamingnews/home"
).strip()

ADILO_PUBLIC_KEY = os.getenv("ADILO_PUBLIC_KEY", "").strip()
ADILO_SECRET_KEY = os.getenv("ADILO_SECRET_KEY", "").strip()
ADILO_PROJECT_ID = os.getenv("ADILO_PROJECT_ID", "").strip()
ADILO_API_BASE = "https://adilo-api.bigcommand.com/v1"

# Discord
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
    "favorite", "favourite", "most popular to cosplay",
    "i only needed", "my go-to",
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
# SCRAPE OG IMAGE/DESCRIPTION (optional)
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
# FEEDS
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


# ----------------------------
# SCORING + VARIETY
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

    # First pass: one per source in priority order
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

    # Fill remaining
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
# DISCORD POSTING
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
# ADILO FEATURED VIDEO (FINAL)
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

def url_ok(url: str) -> bool:
    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20, allow_redirects=True)
        return 200 <= r.status_code < 300
    except Exception:
        return False

def resolve_featured_adilo_watch_url() -> str:
    """
    Uses your working discovery:
      - /projects/{project_id}/files -> { status, message, payload(list), meta }
      - /files/{id}/meta -> { status, message, payload{..., upload_date...} }

    Picks newest by upload_date among the first N items.
    Builds: https://adilo.bigcommand.com/watch/<file_id>
    """
    if not (ADILO_PUBLIC_KEY and ADILO_SECRET_KEY and ADILO_PROJECT_ID):
        return FEATURED_VIDEO_FALLBACK_URL

    try:
        files_url = f"{ADILO_API_BASE}/projects/{ADILO_PROJECT_ID}/files?From=1&To=50"
        data = adilo_get_json(files_url)
        payload = data.get("payload") if isinstance(data, dict) else None
        if not isinstance(payload, list) or not payload:
            print("[ADILO] No payload list in files response; fallback.")
            return FEATURED_VIDEO_FALLBACK_URL

        # Check first 15 items (keeps API calls low)
        candidates = []
        for it in payload[:15]:
            if not isinstance(it, dict):
                continue
            fid = it.get("id")
            title = it.get("title") or it.get("name") or ""
            if not fid:
                continue
            candidates.append((str(fid), str(title)))

        if not candidates:
            print("[ADILO] No file ids found; fallback.")
            return FEATURED_VIDEO_FALLBACK_URL

        newest_id = None
        newest_dt = None

        for fid, title in candidates:
            meta_url = f"{ADILO_API_BASE}/files/{fid}/meta"
            meta = adilo_get_json(meta_url)
            mp = meta.get("payload") if isinstance(meta, dict) else None
            upload_date = mp.get("upload_date") if isinstance(mp, dict) else None
            dt = parse_dt_any(upload_date)
            print(f"[ADILO] meta file_id={fid} title={title[:60]} upload_date={upload_date}")

            if dt and (newest_dt is None or dt > newest_dt):
                newest_dt = dt
                newest_id = fid

        if not newest_id:
            print("[ADILO] Could not determine newest by upload_date; fallback.")
            return FEATURED_VIDEO_FALLBACK_URL

        watch_url = f"https://adilo.bigcommand.com/watch/{newest_id}"

        # sanity check so we never post a 404
        if url_ok(watch_url):
            return watch_url

        print("[ADILO] Watch URL not reachable; fallback.")
        return FEATURED_VIDEO_FALLBACK_URL

    except Exception as e:
        print("[ADILO] Featured video resolver failed:", e)
        return FEATURED_VIDEO_FALLBACK_URL


# ----------------------------
# MAIN
# ----------------------------

def main():
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

    # Dedup by normalized title
    seen = set()
    deduped = []
    for it in sorted(kept, key=lambda x: x["published_at"], reverse=True):
        key = normalize_title_key(it["title"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)

    ranked = choose_with_variety(deduped, TOP_N, MAX_PER_SOURCE)

    # Fill missing summary/image via OpenGraph, then generate clean summaries
    for idx, it in enumerate(ranked):
        if not it["summary"] or not it["image_url"]:
            desc, img = fetch_open_graph(it["url"])
            if not it["summary"] and desc:
                it["summary"] = desc
            if not it["image_url"] and img:
                it["image_url"] = img
        it["summary"] = build_story_summary(strip_html(it["summary"]), it["source"], featured=(idx == 0))

    # Featured video: API-driven
    featured_video_url = resolve_featured_adilo_watch_url()

    pn = pacific_now()
    date_line = pn.strftime("%B %d, %Y")

    header = f"{date_line}\n\n**In Tonight’s Edition of Itty Bitty Gaming News…**\n"

    if not ranked:
        content = (
            header
            + "\n► 🎮 Quiet night — nothing cleared the news-only filter.\n\n"
            + "**📺 Featured Video**\n"
            + f"{md_link(FEATURED_VIDEO_TITLE, featured_video_url)}\n\n"
            + "That’s it for tonight’s Itty Bitty. 🫡"
        )
        post_to_discord(content, [])
        print("Digest posted. Items: 0")
        return

    teaser = []
    for it in ranked[:3]:
        teaser.append(f"► 🎮 {it['title']}")

    hook = "\n\nHere are the 5 biggest stories from the last 24 hours — each with a quick summary and the original source.\n"

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

    featured_video_block = (
        "\n**📺 Featured Video**\n"
        f"{md_link(FEATURED_VIDEO_TITLE, featured_video_url)}\n"
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
    print(f"Digest posted. Items: {len(embeds)}")
    print("Featured video:", featured_video_url)


if __name__ == "__main__":
    main()
