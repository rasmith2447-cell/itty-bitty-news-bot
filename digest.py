#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from zoneinfo import ZoneInfo


# =========================
# TIME GUARD
# =========================
def guard_should_post_now() -> bool:
    tz_name = os.getenv("DIGEST_GUARD_TZ", "America/Los_Angeles").strip()
    target_hour = int(os.getenv("DIGEST_GUARD_LOCAL_HOUR", "19").strip())  # 7pm default
    target_minute = int(os.getenv("DIGEST_GUARD_LOCAL_MINUTE", "0").strip())
    window_minutes = int(os.getenv("DIGEST_GUARD_WINDOW_MINUTES", "360").strip())  # default: 6 hours

    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)

    target_today = now_local.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)

    candidates = [
        target_today - timedelta(days=1),
        target_today,
        target_today + timedelta(days=1),
    ]
    closest_target = min(candidates, key=lambda t: abs((now_local - t).total_seconds()))
    delta_min = abs((now_local - closest_target).total_seconds()) / 60.0

    if delta_min <= window_minutes:
        print(
            f"[GUARD] OK. Local now: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            f"Closest target: {closest_target.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
            f"Delta={delta_min:.1f}min <= {window_minutes}min"
        )
        return True

    print(
        f"[GUARD] Not within posting window. Local now: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
        f"Closest target: {closest_target.strftime('%Y-%m-%d %H:%M:%S %Z')}. "
        f"Delta={delta_min:.1f}min > {window_minutes}min."
    )
    return False


# =========================
# SETTINGS / HELPERS
# =========================
def env_truthy(name: str) -> bool:
    v = (os.getenv(name, "") or "").strip().lower()
    return v in ("1", "true", "yes", "y", "on")


DIGEST_FORCE_POST = env_truthy("DIGEST_FORCE_POST")

DISCORD_WEBHOOK_URL = (os.getenv("DISCORD_WEBHOOK_URL", "") or "").strip()
USER_AGENT = (os.getenv("USER_AGENT", "IttyBittyGamingNews/Digest") or "").strip()

NEWSLETTER_NAME = (os.getenv("NEWSLETTER_NAME", "Itty Bitty Gaming News") or "").strip()
NEWSLETTER_TAGLINE = (os.getenv("NEWSLETTER_TAGLINE", "Snackable daily gaming news — five days a week.") or "").strip()

DIGEST_WINDOW_HOURS = int((os.getenv("DIGEST_WINDOW_HOURS", "24") or "24").strip())
DIGEST_TOP_N = int((os.getenv("DIGEST_TOP_N", "5") or "5").strip())
DIGEST_MAX_PER_SOURCE = int((os.getenv("DIGEST_MAX_PER_SOURCE", "1") or "1").strip())

FEATURED_VIDEO_TITLE = (os.getenv("FEATURED_VIDEO_TITLE", "Watch today’s Itty Bitty Gaming News") or "").strip()
FEATURED_VIDEO_FALLBACK_URL = (os.getenv("FEATURED_VIDEO_FALLBACK_URL", "") or "").strip()

FEATURED_VIDEO_FORCE_ID = (os.getenv("FEATURED_VIDEO_FORCE_ID", "") or "").strip()  # Adilo watch id
YOUTUBE_FEATURED_URL = (os.getenv("YOUTUBE_FEATURED_URL", "") or "").strip()
YOUTUBE_FEATURED_TITLE = (os.getenv("YOUTUBE_FEATURED_TITLE", "") or "").strip()

STATE_DIGEST_FILE = "digest_state.json"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def safe_load_json(path: str) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def safe_save_json(path: str, obj: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def shorten(text: str, limit: int) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if len(t) <= limit:
        return t
    return t[: max(0, limit - 1)].rstrip() + "…"


def debug_scan_json_files() -> None:
    print("[DEBUG] Scanning repo for JSON files that might contain items...")
    files = sorted([f for f in os.listdir(".") if f.lower().endswith(".json")])
    if not files:
        print("[DEBUG] No JSON files found in repo root.")
        return

    for f in files[:40]:
        data = safe_load_json(f)
        if data is None:
            print(f"[DEBUG] {f}: (could not read)")
            continue

        if isinstance(data, dict):
            keys = list(data.keys())
            print(f"[DEBUG] {f}: dict keys={keys[:20]}")
        elif isinstance(data, list):
            print(f"[DEBUG] {f}: list len={len(data)} first_type={(type(data[0]).__name__ if data else 'n/a')}")
        else:
            print(f"[DEBUG] {f}: type={type(data).__name__}")


def find_state_file_prefer_nonempty() -> Optional[str]:
    """
    Pick a JSON file that likely contains feed items, preferring non-empty.
    """
    candidates = [
        "state.json",
        "news_state.json",
        "items.json",
        "raw_items.json",
        "content_board_items.json",
        "data/state.json",
        "data/items.json",
        "out/state.json",
        "out/items.json",
    ]

    # 1) Try known candidates first
    for p in candidates:
        if os.path.exists(p):
            data = safe_load_json(p)
            if data is None:
                continue
            if looks_like_has_items(data):
                return p

    # 2) Otherwise scan any json files
    for p in sorted(os.listdir(".")):
        if not p.endswith(".json"):
            continue
        if p == STATE_DIGEST_FILE:
            continue
        data = safe_load_json(p)
        if data is None:
            continue
        if looks_like_has_items(data):
            return p

    return None


def looks_like_has_items(data: Any) -> bool:
    """
    Detect many possible shapes where items might live.
    """
    if isinstance(data, list):
        return len(data) > 0 and isinstance(data[0], dict) and ("title" in data[0] or "url" in data[0] or "link" in data[0])

    if isinstance(data, dict):
        # Common keys
        for k in ("items", "entries", "posts", "articles", "stories", "results"):
            v = data.get(k)
            if isinstance(v, list) and len(v) > 0 and isinstance(v[0], dict):
                return True

        # Nested common patterns
        payload = data.get("payload")
        if isinstance(payload, dict):
            for k in ("items", "entries", "posts", "articles", "stories", "results"):
                v = payload.get(k)
                if isinstance(v, list) and len(v) > 0 and isinstance(v[0], dict):
                    return True
        if isinstance(payload, list) and len(payload) > 0 and isinstance(payload[0], dict):
            return True

    return False


@dataclass
class Item:
    title: str
    url: str
    source: str
    published: Optional[datetime]
    summary: str
    tags: List[str]
    image: str

    @staticmethod
    def from_any(d: Dict[str, Any]) -> "Item":
        title = (d.get("title") or d.get("headline") or d.get("name") or "").strip()
        url = (d.get("url") or d.get("link") or d.get("permalink") or "").strip()
        source = (d.get("source") or d.get("site") or d.get("publisher") or d.get("feed") or "Unknown").strip()

        published = (
            parse_dt(d.get("published"))
            or parse_dt(d.get("published_at"))
            or parse_dt(d.get("pubDate"))
            or parse_dt(d.get("date"))
            or parse_dt(d.get("timestamp"))
            or parse_dt(d.get("created_at"))
        )

        summary = (d.get("summary") or d.get("description") or d.get("excerpt") or d.get("content") or "").strip()

        tags_raw = d.get("tags") or d.get("tag") or []
        tags: List[str] = []
        if isinstance(tags_raw, str):
            tags = [t.strip() for t in re.split(r"[,#/|]+", tags_raw) if t.strip()]
        elif isinstance(tags_raw, list):
            tags = [str(t).strip() for t in tags_raw if str(t).strip()]

        image = (d.get("image") or d.get("thumbnail") or d.get("img") or d.get("image_url") or "").strip()

        return Item(
            title=title,
            url=url,
            source=source,
            published=published,
            summary=summary,
            tags=tags,
            image=image,
        )


def extract_items(data: Any) -> List[Item]:
    raw_list: List[Dict[str, Any]] = []

    if isinstance(data, list):
        raw_list = [x for x in data if isinstance(x, dict)]

    elif isinstance(data, dict):
        # Common top-level arrays
        for k in ("items", "entries", "posts", "articles", "stories", "results"):
            v = data.get(k)
            if isinstance(v, list):
                raw_list = [x for x in v if isinstance(x, dict)]
                break

        # payload variants
        if not raw_list:
            payload = data.get("payload")
            if isinstance(payload, list):
                raw_list = [x for x in payload if isinstance(x, dict)]
            elif isinstance(payload, dict):
                for k in ("items", "entries", "posts", "articles", "stories", "results"):
                    v = payload.get(k)
                    if isinstance(v, list):
                        raw_list = [x for x in v if isinstance(x, dict)]
                        break

        # fallback: single item dict
        if not raw_list and ("title" in data or "url" in data or "link" in data):
            raw_list = [data]

    items: List[Item] = []
    for d in raw_list:
        it = Item.from_any(d)
        if it.title and it.url:
            items.append(it)

    return items


def within_window(it: Item, hours: int) -> bool:
    if it.published is None:
        return True
    return it.published >= (now_utc() - timedelta(hours=hours))


def normalize_source(s: str) -> str:
    s = (s or "").strip()
    return s if s else "Unknown"


def build_tags(items: List[Item], max_tags: int = 6) -> List[str]:
    counts: Dict[str, int] = {}
    for it in items:
        for t in it.tags:
            tag = re.sub(r"[^a-zA-Z0-9_]+", "", t.lower())
            if tag:
                counts[tag] = counts.get(tag, 0) + 1

    if not counts:
        for it in items:
            words = re.findall(r"[A-Za-z0-9]{3,}", it.title.lower())
            for w in words:
                if w in ("with", "from", "that", "this", "your", "will", "game", "games", "video"):
                    continue
                counts[w] = counts.get(w, 0) + 1

    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [f"#{k}" for k, _ in ranked[:max_tags]]


def pick_top(items: List[Item], top_n: int, max_per_source: int) -> List[Item]:
    def key(it: Item) -> Tuple[int, float]:
        if it.published is None:
            return (0, 0.0)
        return (1, it.published.timestamp())

    items_sorted = sorted(items, key=key, reverse=True)

    out: List[Item] = []
    per_source: Dict[str, int] = {}

    for it in items_sorted:
        src = normalize_source(it.source)
        if per_source.get(src, 0) >= max_per_source:
            continue
        out.append(it)
        per_source[src] = per_source.get(src, 0) + 1
        if len(out) >= top_n:
            break

    if len(out) < top_n:
        for it in items_sorted:
            if it in out:
                continue
            out.append(it)
            if len(out) >= top_n:
                break

    return out[:top_n]


def compute_digest_hash(items: List[Item], featured_adilo_url: str, yt_url: str) -> str:
    blob = {
        "items": [{"t": it.title, "u": it.url, "s": it.source} for it in items],
        "adilo": featured_adilo_url,
        "yt": yt_url,
    }
    raw = json.dumps(blob, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def already_posted(digest_hash: str) -> bool:
    st = safe_load_json(STATE_DIGEST_FILE)
    if not isinstance(st, dict):
        return False
    return (st.get("last_hash") == digest_hash)


def mark_posted(digest_hash: str) -> None:
    safe_save_json(STATE_DIGEST_FILE, {"last_hash": digest_hash, "last_posted_utc": now_utc().isoformat()})


def discord_post(payload: Dict[str, Any]) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL")

    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    resp = requests.post(DISCORD_WEBHOOK_URL, headers=headers, data=json.dumps(payload), timeout=30)

    print(f"[DISCORD] POST status={resp.status_code}")
    if resp.text:
        print(f"[DISCORD] response_snippet={resp.text[:400]}")

    if resp.status_code not in (200, 201, 204):
        raise RuntimeError(f"Discord webhook failed: HTTP {resp.status_code} body={resp.text[:800]}")


def youtube_thumbnail(url: str) -> str:
    m = re.search(r"v=([A-Za-z0-9_-]{11})", url)
    if not m:
        return ""
    vid = m.group(1)
    return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"


def build_newsletter_text(top_items: List[Item]) -> str:
    tz = ZoneInfo(os.getenv("DIGEST_GUARD_TZ", "America/Los_Angeles").strip() or "America/Los_Angeles")
    local_now = datetime.now(tz)
    date_line = local_now.strftime("%B %d, %Y")

    bullets = [f"► 🎮 {shorten(it.title, 80)}" for it in top_items[:3]]
    tags = " ".join(build_tags(top_items, max_tags=6))

    lines: List[str] = []
    lines.append(f"{date_line}\n")
    lines.append(f"In Tonight’s Edition of {NEWSLETTER_NAME}…")
    lines.extend(bullets)
    lines.append("")
    lines.append("Tonight’s Top Stories")
    lines.append("")

    for idx, it in enumerate(top_items, start=1):
        lines.append(f"{idx}) {it.title}")
        if it.summary:
            lines.append(shorten(it.summary, 420))
        lines.append(f"Source: {it.source}")
        lines.append(it.url)
        lines.append("")

    if tags:
        lines.append(tags)
        lines.append("")

    lines.append("—")
    lines.append("That’s it for tonight’s Itty Bitty. 😄")
    lines.append(f"Catch the snackable breakdown on {NEWSLETTER_NAME} next time.")

    return "\n".join(lines).strip()


def build_featured_section_embed() -> Optional[Dict[str, Any]]:
    adilo_watch_url = ""
    if FEATURED_VIDEO_FORCE_ID:
        adilo_watch_url = f"https://adilo.bigcommand.com/watch/{FEATURED_VIDEO_FORCE_ID}"
    elif FEATURED_VIDEO_FALLBACK_URL:
        adilo_watch_url = FEATURED_VIDEO_FALLBACK_URL

    if not (adilo_watch_url or YOUTUBE_FEATURED_URL):
        return None

    thumb = youtube_thumbnail(YOUTUBE_FEATURED_URL) if YOUTUBE_FEATURED_URL else ""

    desc_parts: List[str] = []
    if YOUTUBE_FEATURED_URL:
        yt_title = YOUTUBE_FEATURED_TITLE or "YouTube (same episode)"
        desc_parts.append(f"▶️ **{yt_title}**\n{YOUTUBE_FEATURED_URL}")
    if adilo_watch_url:
        desc_parts.append(f"📺 **{FEATURED_VIDEO_TITLE} (Adilo)**\n{adilo_watch_url}")

    embed: Dict[str, Any] = {"title": "Featured Video", "description": "\n\n".join(desc_parts).strip()}
    if thumb:
        embed["thumbnail"] = {"url": thumb}
    return embed


def main() -> None:
    # Guard (unless forced)
    if not DIGEST_FORCE_POST:
        if not guard_should_post_now():
            print("[GUARD] Skipping post due to time window.")
            return
    else:
        print("[GUARD] DIGEST_FORCE_POST enabled — bypassing time guard.")

    # Debug scan (always helps when items are missing)
    debug_scan_json_files()

    state_path = find_state_file_prefer_nonempty()
    if not state_path:
        raise RuntimeError("Could not find any JSON file that looks like it contains items.")

    data = safe_load_json(state_path)
    if data is None:
        raise RuntimeError(f"Could not read JSON from {state_path}")

    items = extract_items(data)
    print(f"[DIGEST] Using file: {state_path}")
    print(f"[DIGEST] Loaded {len(items)} item(s)")

    # Filter window
    items = [it for it in items if within_window(it, DIGEST_WINDOW_HOURS)]
    print(f"[DIGEST] After {DIGEST_WINDOW_HOURS}h window filter: {len(items)} item(s)")

    if not items:
        print("[DIGEST] No items found in window. Exiting without posting.")
        return

    top_items = pick_top(items, DIGEST_TOP_N, DIGEST_MAX_PER_SOURCE)

    # Featured URLs
    adilo_watch_url = f"https://adilo.bigcommand.com/watch/{FEATURED_VIDEO_FORCE_ID}" if FEATURED_VIDEO_FORCE_ID else (FEATURED_VIDEO_FALLBACK_URL or "")
    digest_hash = compute_digest_hash(top_items, adilo_watch_url, YOUTUBE_FEATURED_URL)

    # Dedupe (unless forced)
    if not DIGEST_FORCE_POST and already_posted(digest_hash):
        print("[DIGEST] Same digest hash as last post. Skipping (dedupe).")
        return
    elif DIGEST_FORCE_POST:
        print("[DIGEST] Force post — bypassing dedupe.")

    # Build content
    newsletter_text = build_newsletter_text(top_items)
    embeds: List[Dict[str, Any]] = []
    featured = build_featured_section_embed()
    if featured:
        embeds.append(featured)

    payload: Dict[str, Any] = {"content": newsletter_text}
    if embeds:
        payload["embeds"] = embeds[:10]

    print("[DIGEST] About to post to Discord…")
    discord_post(payload)

    mark_posted(digest_hash)
    print(f"[DIGEST] Posted digest. Items: {len(top_items)}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[ERROR] {e}")
        raise
