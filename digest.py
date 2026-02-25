#!/usr/bin/env python3
import os
import re
import json
import time
import html
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import List, Dict, Any, Optional, Tuple

import requests
import feedparser
from bs4 import BeautifulSoup

# =========================================================
# SETTINGS / ENV
# =========================================================

DEFAULT_FEEDS = [
    "https://www.bluesnews.com/news/news_1_0.rdf",
    "https://www.ign.com/rss/v2/articles/feed?categories=games",
    "https://www.gamespot.com/feeds/mashup/",
    "https://gamerant.com/feed",
    "https://www.polygon.com/rss/index.xml",
    "https://www.videogameschronicle.com/feed/",
    "https://www.gematsu.com/feed",
]

CACHE_PATH = ".digest_cache.json"         # memory cache for featured video + misc
SITE_DIR = "site"
SITE_HTML = os.path.join(SITE_DIR, "latest.html")
SITE_MD = os.path.join(SITE_DIR, "latest.md")

USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNews/Digest").strip()

NEWSLETTER_NAME = os.getenv("NEWSLETTER_NAME", "Itty Bitty Gaming News").strip()
NEWSLETTER_TAGLINE = os.getenv("NEWSLETTER_TAGLINE", "Snackable daily gaming news — five days a week.").strip()

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

DIGEST_WINDOW_HOURS = int(os.getenv("DIGEST_WINDOW_HOURS", "24").strip())
DIGEST_TOP_N = int(os.getenv("DIGEST_TOP_N", "5").strip())
DIGEST_MAX_PER_SOURCE = int(os.getenv("DIGEST_MAX_PER_SOURCE", "1").strip())

DIGEST_FORCE_POST = os.getenv("DIGEST_FORCE_POST", "").strip().lower() in ("1", "true", "yes", "y")

DIGEST_GUARD_TZ = os.getenv("DIGEST_GUARD_TZ", "America/Los_Angeles").strip()
DIGEST_GUARD_LOCAL_HOUR = int(os.getenv("DIGEST_GUARD_LOCAL_HOUR", "19").strip())
DIGEST_GUARD_LOCAL_MINUTE = int(os.getenv("DIGEST_GUARD_LOCAL_MINUTE", "0").strip())
DIGEST_GUARD_WINDOW_MINUTES = int(os.getenv("DIGEST_GUARD_WINDOW_MINUTES", "120").strip())  # 2h

# YouTube
YOUTUBE_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "").strip()
YOUTUBE_RSS_URL = os.getenv("YOUTUBE_RSS_URL", "").strip()  # optional override
YOUTUBE_FEATURED_URL = os.getenv("YOUTUBE_FEATURED_URL", "").strip()  # optional manual override
YOUTUBE_FEATURED_TITLE = os.getenv("YOUTUBE_FEATURED_TITLE", "").strip()  # optional manual override

# Adilo scrape pages
ADILO_PUBLIC_LATEST_PAGE = os.getenv("ADILO_PUBLIC_LATEST_PAGE", "https://adilo.bigcommand.com/c/ittybittygamingnews/video").strip()
ADILO_PUBLIC_HOME_PAGE = os.getenv("ADILO_PUBLIC_HOME_PAGE", "https://adilo.bigcommand.com/c/ittybittygamingnews/home").strip()

# Optional force (DO NOT set if you want dynamic newest)
FEATURED_VIDEO_FORCE_ID = os.getenv("FEATURED_VIDEO_FORCE_ID", "").strip()

# Feeds env
FEED_URLS_ENV = os.getenv("FEED_URLS", "").strip()

# =========================================================
# DATA TYPES
# =========================================================

@dataclass
class FeedItem:
    title: str
    url: str
    source: str
    published: Optional[datetime]
    summary: str


# =========================================================
# HELPERS
# =========================================================

def http_get(url: str, timeout: int = 20) -> requests.Response:
    headers = {"User-Agent": USER_AGENT}
    return requests.get(url, headers=headers, timeout=timeout)

def safe_mkdir(path: str) -> None:
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)

def load_cache() -> Dict[str, Any]:
    if not os.path.exists(CACHE_PATH):
        return {}
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def save_cache(cache: Dict[str, Any]) -> None:
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)

def now_local() -> datetime:
    return datetime.now(ZoneInfo(DIGEST_GUARD_TZ))

def guard_should_post_now() -> bool:
    if DIGEST_FORCE_POST:
        print("[GUARD] DIGEST_FORCE_POST enabled — bypassing time guard.")
        return True

    tz = ZoneInfo(DIGEST_GUARD_TZ)
    n = datetime.now(tz)
    target_today = n.replace(
        hour=DIGEST_GUARD_LOCAL_HOUR,
        minute=DIGEST_GUARD_LOCAL_MINUTE,
        second=0,
        microsecond=0,
    )

    candidates = [target_today - timedelta(days=1), target_today, target_today + timedelta(days=1)]
    closest = min(candidates, key=lambda t: abs((n - t).total_seconds()))
    delta_min = abs((n - closest).total_seconds()) / 60.0

    if delta_min <= DIGEST_GUARD_WINDOW_MINUTES:
        print(
            f"[GUARD] OK. Local now: {n.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            f"Closest target: {closest.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            f"Delta={delta_min:.1f}min <= {DIGEST_GUARD_WINDOW_MINUTES}min"
        )
        return True

    print(
        f"[GUARD] Not within posting window. Local now: {n.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
        f"Closest target: {closest.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
        f"Delta={delta_min:.1f}min > {DIGEST_GUARD_WINDOW_MINUTES}min. Exiting without posting."
    )
    return False

def parse_dt(entry: Any) -> Optional[datetime]:
    # feedparser gives a struct_time in several places; we normalize to UTC datetime.
    for key in ("published_parsed", "updated_parsed"):
        st = getattr(entry, key, None)
        if st:
            try:
                return datetime(*st[:6], tzinfo=ZoneInfo("UTC"))
            except Exception:
                pass
    return None

def normalize_source(url: str) -> str:
    try:
        m = re.search(r"https?://([^/]+)/", url)
        if not m:
            return url
        host = m.group(1).lower()
        host = host.replace("www.", "")
        return host
    except Exception:
        return url

def clean_text(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s

def shorten(s: str, max_len: int) -> str:
    s = clean_text(s)
    if len(s) <= max_len:
        return s
    return s[: max_len - 1].rstrip() + "…"

def story_key(item: FeedItem) -> str:
    # stable hash key for dedupe
    base = (item.title.strip().lower() + "|" + item.url.strip().lower()).encode("utf-8", errors="ignore")
    return hashlib.sha1(base).hexdigest()

# =========================================================
# RSS FETCH
# =========================================================

def get_feed_urls() -> List[str]:
    if FEED_URLS_ENV:
        # allow newline or comma separated
        parts = []
        for line in FEED_URLS_ENV.replace(",", "\n").splitlines():
            line = line.strip()
            if line:
                parts.append(line)
        return parts
    return DEFAULT_FEEDS

def fetch_feed_items() -> List[FeedItem]:
    feeds = get_feed_urls()
    window_start = datetime.now(ZoneInfo("UTC")) - timedelta(hours=DIGEST_WINDOW_HOURS)

    items: List[FeedItem] = []
    for url in feeds:
        try:
            print(f"[RSS] GET {url}")
            fp = feedparser.parse(url)
            if getattr(fp, "bozo", 0) == 1:
                # do not fail hard; many feeds set bad encodings
                print(f"[RSS] bozo=1 for {url}: {getattr(fp, 'bozo_exception', '')}")

            for e in fp.entries or []:
                link = getattr(e, "link", "") or ""
                title = clean_text(getattr(e, "title", "") or "")
                if not link or not title:
                    continue

                published = parse_dt(e)
                if published and published < window_start:
                    continue

                source = normalize_source(link)

                # Summary: use feed summary if available; keep it short
                raw_sum = getattr(e, "summary", "") or getattr(e, "description", "") or ""
                raw_sum = BeautifulSoup(str(raw_sum), "html.parser").get_text(" ")
                summary = shorten(raw_sum, 260)

                items.append(FeedItem(title=title, url=link, source=source, published=published, summary=summary))
        except Exception as ex:
            print(f"[RSS] Feed failed: {url} ({ex})")

    # Deduplicate by title+url
    seen = set()
    out: List[FeedItem] = []
    for it in items:
        k = story_key(it)
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out

def select_top_items(items: List[FeedItem]) -> List[FeedItem]:
    # Sort by published desc (unknown at end), then title
    def sort_key(it: FeedItem):
        t = it.published or datetime(1970, 1, 1, tzinfo=ZoneInfo("UTC"))
        return (t, it.title.lower())

    items = sorted(items, key=sort_key, reverse=True)

    # Limit per source for variety
    per_source: Dict[str, int] = {}
    selected: List[FeedItem] = []
    for it in items:
        if per_source.get(it.source, 0) >= DIGEST_MAX_PER_SOURCE:
            continue
        selected.append(it)
        per_source[it.source] = per_source.get(it.source, 0) + 1
        if len(selected) >= DIGEST_TOP_N:
            break

    return selected

# =========================================================
# YOUTUBE LATEST
# =========================================================

def youtube_latest_from_rss() -> Tuple[str, str]:
    """
    Returns (url, title) or ("","") on failure.
    """
    if YOUTUBE_FEATURED_URL and YOUTUBE_FEATURED_TITLE:
        return (YOUTUBE_FEATURED_URL, YOUTUBE_FEATURED_TITLE)

    rss = YOUTUBE_RSS_URL
    if not rss and YOUTUBE_CHANNEL_ID:
        rss = f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}"

    if not rss:
        return ("", "")

    try:
        print(f"[YT] Fetch RSS: {rss}")
        fp = feedparser.parse(rss)
        if not fp.entries:
            return ("", "")
        e = fp.entries[0]
        url = getattr(e, "link", "") or ""
        title = clean_text(getattr(e, "title", "") or "")
        return (url, title)
    except Exception as ex:
        print(f"[YT] RSS failed: {ex}")
        return ("", "")

# =========================================================
# ADILO LATEST (PUBLIC SCRAPE)
# =========================================================

ADILO_ID_RE = re.compile(r"(?:/watch/|video\?id=|/stage/videos/)([A-Za-z0-9_\-]{6,})")

def adilo_watch_url_from_id(vid: str) -> str:
    return f"https://adilo.bigcommand.com/watch/{vid}"

def adilo_scrape_candidates(html_text: str) -> List[str]:
    ids = []
    for m in ADILO_ID_RE.finditer(html_text or ""):
        ids.append(m.group(1))
    # keep order, unique
    seen = set()
    out = []
    for x in ids:
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out

def adilo_best_latest_watch_url() -> str:
    # If forced, use it
    if FEATURED_VIDEO_FORCE_ID:
        forced = adilo_watch_url_from_id(FEATURED_VIDEO_FORCE_ID)
        print(f"[ADILO] Using FEATURED_VIDEO_FORCE_ID: {forced}")
        return forced

    # Try multiple URL variants to survive caching / redirects / slow pages
    base = ADILO_PUBLIC_LATEST_PAGE.rstrip("/")
    cb = str(int(time.time() * 1000))
    probe_urls = [
        base,
        f"{base}?cb={cb}",
        f"{base}/?cb={cb}",
        f"{base}?id=&cb={cb}",
        f"{base}?video=latest&cb={cb}",
    ]

    html_text = ""
    for u in probe_urls:
        try:
            print(f"[ADILO] SCRAPE attempt=1 timeout=25 url={u}")
            r = http_get(u, timeout=25)
            if r.status_code == 200 and r.text:
                html_text = r.text
                break
        except requests.exceptions.Timeout:
            print(f"[ADILO] Timeout url={u} (timeout=25)")
        except Exception as ex:
            print(f"[ADILO] SCRAPE error url={u}: {ex}")

    if not html_text:
        print(f"[ADILO] Falling back: {ADILO_PUBLIC_HOME_PAGE}")
        return ADILO_PUBLIC_HOME_PAGE

    # Extract candidates from the page HTML
    ids = adilo_scrape_candidates(html_text)
    if not ids:
        print("[ADILO] No IDs found on latest page; falling back to home.")
        return ADILO_PUBLIC_HOME_PAGE

    # Heuristic: newest usually appears early. We’ll test a handful for “newest” by parsing watch-page JSON/metadata.
    candidates = ids[:12]

    best_url = ""
    best_dt = None

    for vid in candidates:
        wurl = adilo_watch_url_from_id(vid)
        try:
            rr = http_get(wurl, timeout=20)
            if rr.status_code != 200 or not rr.text:
                continue

            # Many sites embed timestamps in JSON blobs; we try to find ISO-ish timestamps.
            # We’ll accept the newest timestamp found on-page as the “upload” proxy.
            ts_matches = re.findall(
                r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+\-]\d{2}:\d{2})?)",
                rr.text,
            )
            dt_local_best = None
            for ts in ts_matches[:50]:
                try:
                    # normalize
                    ts_norm = ts.replace(" ", "T")
                    if ts_norm.endswith("Z"):
                        dt = datetime.fromisoformat(ts_norm.replace("Z", "+00:00"))
                    else:
                        dt = datetime.fromisoformat(ts_norm)
                    if (dt_local_best is None) or (dt > dt_local_best):
                        dt_local_best = dt
                except Exception:
                    pass

            if dt_local_best:
                if (best_dt is None) or (dt_local_best > best_dt):
                    best_dt = dt_local_best
                    best_url = wurl

        except Exception:
            continue

    if best_url:
        print(f"[ADILO] Picked newest by watch-page timestamp: {best_url} dt={best_dt}")
        return best_url

    # If we couldn't parse timestamps, fallback to the first candidate (usually newest)
    fallback = adilo_watch_url_from_id(ids[0])
    print(f"[ADILO] No timestamps parsed; using first candidate: {fallback}")
    return fallback

# =========================================================
# DISCORD POST
# =========================================================

def discord_post(content: str, embeds: Optional[List[Dict[str, Any]]] = None) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL")

    payload: Dict[str, Any] = {"content": content}

    if embeds:
        # Discord hard limit: 10 embeds
        payload["embeds"] = embeds[:10]

    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, headers={"User-Agent": USER_AGENT}, timeout=25)
    r.raise_for_status()

def build_story_embed(i: int, it: FeedItem, tags: List[str]) -> Dict[str, Any]:
    # title links directly; url shows under the story via embed itself
    # Keep embed description short to avoid Discord 400
    desc = shorten(it.summary or "", 320)
    tag_line = ""
    if tags:
        tag_line = "\n" + " ".join([f"`{t}`" for t in tags[:6]])

    return {
        "title": f"{i}) {it.title}",
        "url": it.url,
        "description": desc + tag_line,
        "footer": {"text": f"Source: {it.source}"},
    }

def tags_for_story(it: FeedItem) -> List[str]:
    # Lightweight tagger: feel free to expand later
    t = (it.title + " " + it.summary).lower()
    tags = []
    if any(x in t for x in ["release", "launch", "out now", "available now", "drops", "coming", "dated", "release date"]):
        tags.append("Release")
    if any(x in t for x in ["announced", "reveal", "revealed", "trailer", "first look", "unveils"]):
        tags.append("Announcement")
    if any(x in t for x in ["update", "patch", "hotfix", "season", "expansion", "dlc"]):
        tags.append("Update")
    if any(x in t for x in ["layoff", "laid off", "studio", "shutdown", "closed", "closure"]):
        tags.append("Studio")
    if any(x in t for x in ["delay", "delayed", "postponed"]):
        tags.append("Delay")
    if any(x in t for x in ["lawsuit", "sued", "court", "settlement"]):
        tags.append("Legal")
    if any(x in t for x in ["retire", "retirement", "steps down", "leaves", "resigns"]):
        tags.append("Leadership")
    return tags

# =========================================================
# PUBLIC SITE OUTPUT (GitHub Pages)
# =========================================================

def render_public_html(date_str: str, bullets: List[str], items: List[FeedItem], yt: Tuple[str, str], adilo_url: str) -> str:
    yt_url, yt_title = yt
    def esc(s: str) -> str:
        return html.escape(s or "")

    story_cards = []
    for idx, it in enumerate(items, start=1):
        story_cards.append(f"""
        <div class="card">
          <div class="kicker">{idx}</div>
          <h3><a href="{esc(it.url)}" target="_blank" rel="noopener">{esc(it.title)}</a></h3>
          <p>{esc(it.summary)}</p>
          <div class="meta">Source: {esc(it.source)}</div>
        </div>
        """)

    bullet_html = "".join([f"<li>{esc(b)}</li>" for b in bullets])

    yt_block = ""
    if yt_url:
        # YouTube embed expects /embed/<id>
        vid = ""
        m = re.search(r"v=([A-Za-z0-9_\-]+)", yt_url)
        if m:
            vid = m.group(1)
        if vid:
            yt_block = f"""
            <div class="card">
              <h3>▶️ YouTube (latest)</h3>
              <div class="embed">
                <iframe width="560" height="315"
                  src="https://www.youtube.com/embed/{esc(vid)}"
                  title="{esc(yt_title)}"
                  frameborder="0"
                  allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
                  allowfullscreen></iframe>
              </div>
              <div class="meta"><a href="{esc(yt_url)}" target="_blank" rel="noopener">{esc(yt_title or yt_url)}</a></div>
            </div>
            """

    adilo_block = ""
    if adilo_url:
        adilo_block = f"""
        <div class="card">
          <h3>📺 Adilo (latest)</h3>
          <div class="meta"><a href="{esc(adilo_url)}" target="_blank" rel="noopener">{esc(adilo_url)}</a></div>
        </div>
        """

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>{esc(NEWSLETTER_NAME)} — Latest Digest</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 0; background: #0b0f14; color: #e6edf3; }}
    .wrap {{ max-width: 860px; margin: 0 auto; padding: 24px; }}
    .head {{ margin-bottom: 18px; }}
    .brand {{ font-size: 28px; font-weight: 800; margin: 0; }}
    .tagline {{ opacity: 0.85; margin-top: 6px; }}
    .date {{ margin-top: 10px; opacity: 0.8; }}
    .section {{ margin-top: 22px; }}
    .section h2 {{ font-size: 18px; margin: 0 0 12px 0; }}
    .card {{ background: #111826; border: 1px solid rgba(255,255,255,.08); border-radius: 14px; padding: 14px 14px; margin-bottom: 12px; }}
    .card h3 {{ margin: 0 0 8px 0; font-size: 16px; }}
    .card a {{ color: #7dd3fc; text-decoration: none; }}
    .card a:hover {{ text-decoration: underline; }}
    .meta {{ opacity: 0.8; font-size: 13px; margin-top: 8px; }}
    .kicker {{ display: inline-block; font-weight: 700; opacity: 0.8; margin-bottom: 8px; }}
    ul {{ margin: 0; padding-left: 18px; }}
    .embed {{ position: relative; padding-bottom: 56.25%; height: 0; overflow: hidden; border-radius: 12px; }}
    .embed iframe {{ position: absolute; top:0; left:0; width:100%; height:100%; }}
    .footer {{ margin-top: 24px; opacity: .7; font-size: 13px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="head">
      <h1 class="brand">{esc(NEWSLETTER_NAME)}</h1>
      <div class="tagline">{esc(NEWSLETTER_TAGLINE)}</div>
      <div class="date">{esc(date_str)}</div>
    </div>

    <div class="section">
      <h2>Tonight’s Edition</h2>
      <ul>{bullet_html}</ul>
    </div>

    <div class="section">
      <h2>Featured Video</h2>
      {yt_block}
      {adilo_block}
    </div>

    <div class="section">
      <h2>Tonight’s Top Stories</h2>
      {''.join(story_cards)}
    </div>

    <div class="footer">
      — That’s it for tonight. Catch the snackable breakdown on {esc(NEWSLETTER_NAME)} tomorrow.
    </div>
  </div>
</body>
</html>
"""

def write_public_outputs(date_str: str, bullets: List[str], items: List[FeedItem], yt: Tuple[str, str], adilo_url: str) -> None:
    safe_mkdir(SITE_DIR)

    # Markdown (simple)
    yt_url, yt_title = yt
    md = []
    md.append(f"# {NEWSLETTER_NAME}\n")
    md.append(f"{NEWSLETTER_TAGLINE}\n")
    md.append(f"**{date_str}**\n")
    md.append("## In Tonight’s Edition…\n")
    for b in bullets:
        md.append(f"- {b}")
    md.append("\n## Featured Video\n")
    if yt_url:
        md.append(f"- YouTube: [{yt_title or yt_url}]({yt_url})")
    if adilo_url:
        md.append(f"- Adilo: [{adilo_url}]({adilo_url})")
    md.append("\n## Tonight’s Top Stories\n")
    for idx, it in enumerate(items, start=1):
        md.append(f"### {idx}) {it.title}\n")
        md.append(f"{it.summary}\n")
        md.append(f"- Source: {it.source}\n")
        md.append(f"- Link: {it.url}\n")
    with open(SITE_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(md).strip() + "\n")

    html_out = render_public_html(date_str, bullets, items, yt, adilo_url)
    with open(SITE_HTML, "w", encoding="utf-8") as f:
        f.write(html_out)

    # Also create a tiny index.html that forwards to latest.html (nice for Pages root)
    with open(os.path.join(SITE_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(f"""<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0; url=latest.html">""")

# =========================================================
# MAIN DIGEST BUILDER
# =========================================================

def build_discord_message(date_str: str, bullets: List[str]) -> str:
    # Keep forward-facing and clean; no "cards below" etc.
    lines = []
    lines.append(NEWSLETTER_TAGLINE)
    lines.append("")
    lines.append(date_str)
    lines.append("")
    lines.append(f"In Tonight’s Edition of {NEWSLETTER_NAME}…")
    for b in bullets[:3]:
        lines.append(f"► 🎮 {b}")
    lines.append("")
    lines.append("Tonight’s Top Stories")
    lines.append("")
    return "\n".join(lines)

def main() -> None:
    if not guard_should_post_now():
        # exit clean (no failure)
        return

    cache = load_cache()
    last_adilo_url = (cache.get("last_adilo_url") or "").strip()
    last_yt_url = (cache.get("last_youtube_url") or "").strip()

    # 1) Fetch feed items
    items_all = fetch_feed_items()
    if not items_all:
        print("[DIGEST] No feed items fetched. Exiting without posting.")
        return

    items = select_top_items(items_all)
    if not items:
        print("[DIGEST] No items selected. Exiting without posting.")
        return

    # Bullet headlines (top 3)
    bullets = [it.title for it in items[:3]]

    # 2) Featured videos
    yt_url, yt_title = youtube_latest_from_rss()
    adilo_url = adilo_best_latest_watch_url()

    # 3) Memory cache behavior: avoid spamming same embeds repeatedly
    yt_changed = bool(yt_url and yt_url != last_yt_url)
    adilo_changed = bool(adilo_url and adilo_url != last_adilo_url)

    # 4) Build Discord content + embeds
    date_str = now_local().strftime("%B %d, %Y")

    content = build_discord_message(date_str, bullets)

    embeds: List[Dict[str, Any]] = []

    # Story embeds FIRST (under their correlating story number)
    # Each embed has title+url, so the "card" appears directly with story
    for idx, it in enumerate(items, start=1):
        tags = tags_for_story(it)
        embeds.append(build_story_embed(idx, it, tags))

    # Then featured video blocks (YouTube first, then Adilo)
    # NOTE: Discord will auto-embed the YouTube link, but we also include it in content for clarity.
    video_lines = []
    video_lines.append("")
    if yt_url:
        if yt_changed:
            video_lines.append("▶️ YouTube (latest)")
        else:
            video_lines.append("▶️ YouTube (latest — unchanged)")
        video_lines.append(yt_url)

    if adilo_url:
        if adilo_changed:
            video_lines.append("")
            video_lines.append("📺 Adilo (latest)")
        else:
            video_lines.append("")
            video_lines.append("📺 Adilo (latest — unchanged)")
        video_lines.append(adilo_url)

    # If we want to reduce spam even more, we can suppress adding repeated links;
    # but you asked to keep the link + thumbnail, so we keep the URLs always.
    content = content + "\n" + "\n".join(video_lines).strip() + "\n"

    # Discord hard limit: content <= 2000 chars
    if len(content) > 2000:
        # shrink: reduce summaries by limiting embed description already;
        # here we just trim content tail if needed.
        content = content[:1990] + "…\n"

    # 5) Post to Discord
    discord_post(content, embeds)

    # 6) Write public outputs (for GitHub Pages)
    write_public_outputs(date_str, bullets, items, (yt_url, yt_title), adilo_url)

    # 7) Update cache
    cache["last_youtube_url"] = yt_url
    cache["last_adilo_url"] = adilo_url
    cache["last_run_local"] = now_local().isoformat()
    save_cache(cache)

    print("[DONE] Digest posted.")
    print(f"[DONE] YouTube: {yt_url}")
    print(f"[DONE] Featured Adilo video: {adilo_url}")


if __name__ == "__main__":
    main()
