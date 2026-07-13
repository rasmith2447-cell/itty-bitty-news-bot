#!/usr/bin/env python3
"""
mailchimp_send.py — Itty Bitty Gaming News
Reads digest_latest.json and sends a branded HTML email campaign
via the Mailchimp API to the IBGN audience.
"""

import concurrent.futures
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()

MAILCHIMP_API_KEY   = env("MAILCHIMP_API_KEY")
MAILCHIMP_AUDIENCE_ID = env("MAILCHIMP_AUDIENCE_ID")
DIGEST_EXPORT_FILE  = env("DIGEST_EXPORT_FILE", "digest_latest.json")
YOUTUBE_URL         = env("YOUTUBE_URL", "https://www.youtube.com/@smitty-2447")
PODCAST_URL         = env("PODCAST_URL", "https://podcasts.apple.com/us/podcast/itty-bitty-gaming-news/id1711880008")
LOGO_URL            = env("LOGO_URL", "https://raw.githubusercontent.com/rasmith2447-cell/itty-bitty-news-bot/main/Itty%20Bitty%20Gaming%20News%20Logo%20V.2.png")
TAGLINE             = "And that's your Itty Bitty Gaming News!"

# IGDB config
IGDB_CLIENT_ID      = env("IGDB_CLIENT_ID")
IGDB_CLIENT_SECRET  = env("IGDB_CLIENT_SECRET")
IGDB_DAYS_AHEAD     = int(env("IGDB_DAYS_AHEAD", "14"))
IGDB_PLATFORMS      = [6, 48, 167, 49, 169, 130]  # PC, PS4, PS5, XB1, XSX, Switch

# Mailchimp datacenter is the suffix after the dash in the API key (e.g. us9)
DC = MAILCHIMP_API_KEY.split("-")[-1] if "-" in MAILCHIMP_API_KEY else "us1"
BASE = f"https://{DC}.api.mailchimp.com/3.0"

# ---------------------------------------------------------------------------
# IGDB RELEASES
# ---------------------------------------------------------------------------

def igdb_token() -> str:
    r = requests.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id":     IGDB_CLIENT_ID,
            "client_secret": IGDB_CLIENT_SECRET,
            "grant_type":    "client_credentials",
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def igdb_query(token: str, endpoint: str, query: str) -> list:
    r = requests.post(
        f"https://api.igdb.com/v4/{endpoint}",
        headers={
            "Client-ID":     IGDB_CLIENT_ID,
            "Authorization": f"Bearer {token}",
            "Content-Type":  "text/plain",
        },
        data=query,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def fetch_upcoming_releases() -> list:
    if not IGDB_CLIENT_ID or not IGDB_CLIENT_SECRET:
        print("[IGDB] Credentials not set — skipping releases.")
        return []
    try:
        token = igdb_token()
    except Exception as ex:
        print(f"[IGDB] Token error: {ex}")
        return []

    now      = datetime.now(timezone.utc)
    start    = int(now.timestamp())
    end      = int((now + timedelta(days=30)).timestamp())
    plats    = ",".join(str(p) for p in IGDB_PLATFORMS)
    print(f"[IGDB] Querying releases from {now.strftime('%Y-%m-%d')} to {(now + timedelta(days=30)).strftime('%Y-%m-%d')}")
    query    = f"""
    fields game.name, game.cover.url, date, platform.name, platform.id;
    where date >= {start}
      & date <= {end}
      & platform = ({plats});
    sort date asc;
    limit 50;
    """
    try:
        results = igdb_query(token, "release_dates", query)
    except Exception as ex:
        print(f"[IGDB] Query error: {ex}")
        return []

    seen = {}
    PLATFORM_NAMES = {
        "PC (Microsoft Windows)": "PC",
        "Xbox Series X|S": "Xbox Series X/S",
        "PlayStation 5": "PS5",
        "PlayStation 4": "PS4",
        "Nintendo Switch": "Switch",
        "Xbox One": "Xbox One",
    }
    for item in results:
        game = item.get("game", {})
        if not game:
            continue
        name = game.get("name", "").strip()
        if not name:
            continue
        date_ts  = item.get("date", 0)
        platform = PLATFORM_NAMES.get(
            item.get("platform", {}).get("name", ""),
            item.get("platform", {}).get("name", "")
        )
        cover    = game.get("cover", {})
        cover_url = ""
        if cover and cover.get("url"):
            cover_url = "https:" + cover["url"].replace("t_thumb", "t_cover_big")
        if name not in seen:
            seen[name] = {"name": name, "date": date_ts, "platforms": [platform] if platform else [], "cover_url": cover_url}
        else:
            if platform and platform not in seen[name]["platforms"]:
                seen[name]["platforms"].append(platform)
            if date_ts < seen[name]["date"]:
                seen[name]["date"] = date_ts

    releases = sorted(seen.values(), key=lambda x: x["date"])[:8]
    for r in releases:
        try:
            r["date_str"] = datetime.fromtimestamp(r["date"], tz=timezone.utc).strftime("%B %-d")
        except Exception:
            r["date_str"] = "Coming Soon"

    print(f"[IGDB] Found {len(releases)} upcoming releases.")
    return releases

def headers() -> dict:
    return {
        "Authorization": f"Bearer {MAILCHIMP_API_KEY}",
        "Content-Type": "application/json",
    }

def mc_post(path: str, payload: dict) -> dict:
    r = requests.post(f"{BASE}{path}", headers=headers(), json=payload, timeout=30)
    if not r.ok:
        print(f"[MAILCHIMP] HTTP {r.status_code}: {r.text[:500]}")
        r.raise_for_status()
    return r.json()

def mc_get(path: str) -> dict:
    r = requests.get(f"{BASE}{path}", headers=headers(), timeout=30)
    if not r.ok:
        print(f"[MAILCHIMP] HTTP {r.status_code}: {r.text[:500]}")
        r.raise_for_status()
    return r.json()

# ---------------------------------------------------------------------------
# LOAD STORIES
# ---------------------------------------------------------------------------

def fetch_og_image(url: str) -> str:
    """Fetch the Open Graph image from a story URL."""
    try:
        import re as _re
        r = requests.get(url, timeout=8, headers={
            "User-Agent": "Mozilla/5.0 (compatible; IBGNBot/1.0)"
        })
        if not r.ok:
            return ""
        # Look for og:image or twitter:image meta tags
        for pattern in [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        ]:
            m = _re.search(pattern, r.text, _re.IGNORECASE)
            if m:
                img = m.group(1).strip()
                if img.startswith("http") and not img.endswith(".svg"):
                    return img
    except Exception:
        pass
    return ""


def enrich_stories_with_images(stories: list) -> list:
    """Add OG images to stories that don't have one from the RSS feed."""
    def enrich(story):
        if not story.get("image_url") and story.get("url"):
            img = fetch_og_image(story["url"])
            if img:
                story["image_url"] = img
                print(f"[IMAGE] Found OG image for: {story.get('title', '')[:50]}")
        return story
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        return list(executor.map(enrich, stories))


def load_digest_stories() -> tuple:
    """Returns (should_post: bool, stories: list, youtube_url: str, post_date: str)"""
    if os.path.exists(DIGEST_EXPORT_FILE):
        try:
            with open(DIGEST_EXPORT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    should_post = data.get("should_post", False)
                    stories     = data.get("stories", [])
                    yt_url      = data.get("youtube_url") or YOUTUBE_URL
                    post_date   = data.get("post_date", "")
                    return should_post, stories, yt_url, post_date
                if isinstance(data, list):
                    return len(data) > 0, data, YOUTUBE_URL, ""
        except Exception as ex:
            print(f"[MAILCHIMP] Could not read {DIGEST_EXPORT_FILE}: {ex}")
    print(f"[MAILCHIMP] {DIGEST_EXPORT_FILE} not found — skipping.")
    return False, [], YOUTUBE_URL, ""

# ---------------------------------------------------------------------------
# GAME OF THE WEEK
# ---------------------------------------------------------------------------

GOTW_OVERRIDE = {
    "title":       "Palworld",
    "description": "The phenomenon that took the gaming world by storm is still going strong. Palworld lets you fight, farm, build, and work alongside mysterious creatures called Pals in a massive open world. Capture and breed Pals, put them to work in your factories, ride them across land, sea, and sky — or eat them when times get tough. With regular updates still dropping, now is a great time to jump in.",
    "platform":    "Available on PC (Steam), Xbox Series X|S, Xbox One & Xbox Game Pass",
    "url":         "https://store.steampowered.com/app/1623730/Palworld/",
    "image_url":   "https://cdn.akamai.steamstatic.com/steam/apps/1623730/header.jpg",
}

GOTW_FALLBACK = {
    "title":       "SWAPMEAT",
    "description": "Just graduated out of Early Access on June 17th and it's an absolute blast. You're a shape-shifting operative ripping through alien worlds, stealing enemy body parts mid-combat to gain their abilities. Triple-jump legs, grenade-launching turkey heads, turret-dropping torsos — thousands of wild combos. Play solo or with up to 3 friends in co-op. If Risk of Rain 2 and Helldivers 2 had a chaotic, meaty baby, this is it.",
    "platform":    "Available on PC (Steam)",
    "url":         "https://store.steampowered.com/app/2790700/SWAPMEAT/",
    "image_url":   "https://cdn.akamai.steamstatic.com/steam/apps/2790700/header.jpg",
}


def get_game_of_the_week() -> dict:
    """Auto-generates Game of the Week using Claude + web search on Sundays, cached Mon-Sat."""
    if GOTW_OVERRIDE:
        print("[GOTW] Using manual override.")
        return GOTW_OVERRIDE

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("[GOTW] No API key — using fallback.")
        return GOTW_FALLBACK

    try:
        from zoneinfo import ZoneInfo as _ZI
        now_pt = datetime.now(_ZI("America/Los_Angeles"))
    except Exception:
        now_pt = datetime.now()

    today_str    = now_pt.strftime("%Y-%m-%d")
    current_week = now_pt.strftime("%Y-W%W")
    cache_file   = ".gotw_cache.json"

    # Load cache
    cached = {}
    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r") as f:
                cached = json.load(f)
        except Exception:
            pass

    # Return cached if same week
    if cached.get("week") == current_week and cached.get("gotw"):
        print(f"[GOTW] Using cached: {cached['gotw'].get('title')}")
        return cached["gotw"]

    # Generate via Claude
    print(f"[GOTW] Generating Game of the Week for {today_str}...")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{
                "role": "user",
                "content": (
                    f"Today is {today_str}. Search for the most notable video game released in the past 7 days. "
                    "Pick the single most talked-about new release that gamers would be excited about. "
                    "Respond with ONLY a JSON object, no markdown, no extra text:\n"
                    '{"title": "Game Name", "description": "2-3 sentence engaging description for a gaming newsletter", '
                    '"platform": "Available on X, Y, Z", "steam_app_id": "numeric Steam app ID only if on Steam, otherwise empty string", '
                    '"image_url": "direct URL to official header/key art image if not on Steam, otherwise empty string", '
                    '"url": "https://store.steampowered.com/app/ID/ or official site URL"}'
                )
            }]
        )

        import json as _json
        text = "".join(getattr(b, "text", "") for b in message.content).strip()
        if "```" in text:
            text = text.split("```")[1].replace("json", "").strip()

        data     = _json.loads(text)
        steam_id = data.get("steam_app_id", "").strip()
        # Only use Steam image if steam_id looks valid (numeric, 6-8 digits)
        if steam_id and steam_id.isdigit() and 6 <= len(steam_id) <= 8:
            image_url = f"https://cdn.akamai.steamstatic.com/steam/apps/{steam_id}/header.jpg"
        else:
            image_url = data.get("image_url", "")
        gotw     = {
            "title":       data.get("title", ""),
            "description": data.get("description", ""),
            "platform":    data.get("platform", ""),
            "url":         data.get("url", ""),
            "image_url":   image_url,
        }
        print(f"[GOTW] Generated: {gotw['title']}")

        # Save cache
        try:
            with open(cache_file, "w") as f:
                _json.dump({"date": today_str, "week": current_week, "gotw": gotw}, f, indent=2)
        except Exception as ex:
            print(f"[GOTW] Cache write failed: {ex}")

        return gotw

    except Exception as ex:
        print(f"[GOTW] Generation failed: {ex} — using fallback.")
        return GOTW_FALLBACK


# ---------------------------------------------------------------------------
# HTML EMAIL BUILDER
# ---------------------------------------------------------------------------

STORY_ICONS = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
STORY_COLORS = ["#FFD700", "#C0C0C0", "#CD7F32", "#4A9EFF", "#4A9EFF"]

def build_story_row(index: int, story: dict) -> str:
    icon      = STORY_ICONS[index] if index < len(STORY_ICONS) else f"{index+1}."
    color     = STORY_COLORS[index] if index < len(STORY_COLORS) else "#4A9EFF"
    title     = story.get("title", "").strip()
    url       = story.get("url", "").strip()
    source    = story.get("source", "").strip()
    image_url = story.get("image_url", "").strip()

    link_open  = f'<a href="{url}" style="text-decoration:none;color:inherit;" target="_blank">' if url else ""
    link_close = "</a>" if url else ""

    image_block = f"""
              <tr>
                <td style="padding:0 0 0 0;font-size:0;line-height:0;">
                  {link_open}<img src="{image_url}" alt="" width="100%" style="display:block;border-radius:6px 6px 0 0;max-height:200px;object-fit:cover;" />{link_close}
                </td>
              </tr>""" if image_url else ""

    image_section = f'<table width="100%" cellpadding="0" cellspacing="0" border="0">{image_block}</table>' if image_url else ""

    return f"""
    <tr>
      <td style="padding:0 0 16px 0;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#1a1a2e;border-radius:10px;border-left:4px solid {color};overflow:hidden;">
          <tr>
            <td>{image_section}</td>
          </tr>
          <tr>
            <td style="padding:16px 20px;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td width="40" style="vertical-align:top;padding-right:12px;">
                    <span style="font-size:22px;line-height:1;">{icon}</span>
                  </td>
                  <td style="vertical-align:top;">
                    {link_open}
                    <p style="margin:0 0 4px 0;font-family:'Courier New',monospace;font-size:15px;font-weight:700;color:#ffffff;line-height:1.4;">{title}</p>
                    {link_close}
                    {f'<p style="margin:0;font-family:Arial,sans-serif;font-size:11px;color:#4A9EFF;text-transform:uppercase;letter-spacing:1px;">{source}</p>' if source else ''}
                  </td>
                </tr>
              </table>
            </td>
          </tr>
        </table>
      </td>
    </tr>"""

def generate_trivia() -> tuple:
    """Generate a daily gaming trivia Q&A using the Anthropic API."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("[TRIVIA] ANTHROPIC_API_KEY not set — using fallback.")
        return (
            "Which iconic video game character first appeared in Donkey Kong (1981)?",
            "Jumpman, later known as Mario!"
        )
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        from datetime import date
        try:
            from zoneinfo import ZoneInfo
            from datetime import datetime as _dt
            today_pt = _dt.now(ZoneInfo("America/Los_Angeles")).date()
        except Exception:
            today_pt = date.today()
        import random
        topics = [
            "a specific game release year", "a video game character's origin",
            "a gaming world record", "a console launch detail", "a game developer fact",
            "a classic arcade game", "an RPG milestone", "a sports game fact",
            "a Nintendo franchise moment", "a PlayStation exclusive detail",
            "an Xbox game fact", "a PC gaming milestone", "a game soundtrack",
            "a speedrunning record", "a gaming Easter egg",
        ]
        topic = random.choice(topics)
        day = today_pt.strftime("%B %d, %Y")
        print(f"[TRIVIA] Generating question for {day} (topic: {topic})...")
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system="You are a gaming trivia generator. Always respond with valid JSON only. Never use markdown, code blocks, or any other formatting. Output only the raw JSON object.",
            messages=[{
                "role": "user",
                "content": (
                    f"Generate a fun gaming trivia question specifically about {topic}. "
                    f"Today is {day}. Make it different from common trivia questions about Zelda or Mario. "
                    "IMPORTANT: Only ask questions where you are 100% certain of the correct answer. "
                    "Do not ask trick questions or questions with disputed answers. "
                    "Output only this JSON with no other text: "
                    '{"question": "...", "answer": "..."}'
                )
            }]
        )
        import json as _json
        # Extract text blocks only
        text = ""
        for block in message.content:
            if hasattr(block, "text") and getattr(block, "type", "") == "text":
                text += block.text
        text = text.strip()
        print(f"[TRIVIA] Raw response: {text[:100]}")
        if not text:
            raise ValueError("Empty API response")
        # Strip any accidental markdown
        if "```" in text:
            text = text.split("```")[1].replace("json", "").strip()
        # Find JSON object in case there's surrounding text
        start = text.find("{")
        end   = text.rfind("}") + 1
        if start >= 0 and end > start:
            text = text[start:end]
        data = _json.loads(text)
        print(f"[TRIVIA] Generated: {data.get('question', '')[:60]}...")
        return data.get("question", ""), data.get("answer", "")
    except Exception as ex:
        print(f"[TRIVIA] Generation failed: {ex}")
        return (
            "Which iconic video game character first appeared in Donkey Kong (1981)?",
            "Jumpman, later known as Mario!"
        )


def get_youtube_video_id(url: str) -> str:
    """Extract video ID from a YouTube URL."""
    import re
    patterns = [
        r"youtube\.com/watch\?v=([^&]+)",
        r"youtu\.be/([^?]+)",
        r"youtube\.com/shorts/([^?]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, url or "")
        if m:
            return m.group(1)
    return ""


def build_html_email(stories: list, date_str: str, latest_yt_url: str = None) -> str:
    story_rows  = "".join(build_story_row(i, s) for i, s in enumerate(stories))
    yt_link     = latest_yt_url or YOUTUBE_URL
    video_id    = get_youtube_video_id(yt_link)

    # If no video ID (channel URL passed instead of video URL), fetch latest video
    if not video_id:
        print(f"[MAILCHIMP] No video ID from URL '{yt_link}' — fetching latest video...")
        try:
            channel_id = os.getenv("YOUTUBE_CHANNEL_ID", "UC0SJd4h7GQqoYTVjlDnSzqQ")
            rss = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
            import re as _re
            r = requests.get(rss, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            if r.ok:
                m = _re.search(r"<yt:videoId>([^<]+)</yt:videoId>", r.text)
                if m:
                    video_id = m.group(1).strip()
                    yt_link  = f"https://www.youtube.com/watch?v={video_id}"
                    print(f"[MAILCHIMP] Found latest video: {video_id}")
        except Exception as ex:
            print(f"[MAILCHIMP] YouTube fetch failed: {ex}")

    thumb_url = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg" if video_id else ""
    print(f"[MAILCHIMP] YouTube: video_id={video_id}, thumb={thumb_url[:50] if thumb_url else 'none'}")

    # YouTube video section — only show if we have a specific video URL
    if video_id:
        youtube_section = f"""
          <!-- YOUTUBE VIDEO -->
          <tr>
            <td style="background:#0f0f24;padding:0 30px 28px;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td style="border-top:1px solid #1e3a8a;padding-top:24px;padding-bottom:14px;">
                    <p style="margin:0;font-family:'Courier New',monospace;font-size:12px;color:#4A9EFF;text-align:center;letter-spacing:2px;text-transform:uppercase;">— Latest Video —</p>
                  </td>
                </tr>
                <tr>
                  <td align="center">
                    <a href="{yt_link}" target="_blank" style="display:block;position:relative;text-decoration:none;">
                      <img src="{thumb_url}" alt="Latest Video" width="100%" style="display:block;border-radius:10px;max-width:540px;" />
                      <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:10px;">
                        <tr>
                          <td align="center">
                            <span style="display:inline-block;background:#FF0000;border-radius:6px;padding:8px 20px;font-family:'Courier New',monospace;font-size:13px;color:#ffffff;letter-spacing:1px;">▶ Watch Now on YouTube</span>
                          </td>
                        </tr>
                      </table>
                    </a>
                  </td>
                </tr>
              </table>
            </td>
          </tr>"""
    else:
        youtube_section = ""

    # Generate daily trivia question
    trivia_question, trivia_answer = generate_trivia()

    # ---------------------------------------------------------------------------
    # GAME OF THE WEEK — Auto-generated every Sunday via Claude API
    # Falls back to manual pick if API unavailable
    # ---------------------------------------------------------------------------
    gotw = get_game_of_the_week()

    gotw_section = f"""
          <!-- GAME OF THE WEEK -->
          <tr>
            <td style="background:#0f0f24;padding:0 30px 28px;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td style="border-top:1px solid #1e3a8a;padding-top:24px;padding-bottom:14px;">
                    <p style="margin:0;font-family:'Courier New',monospace;font-size:12px;color:#FFD700;text-align:center;letter-spacing:2px;text-transform:uppercase;">— 🏆 Game of the Week —</p>
                  </td>
                </tr>
                <tr>
                  <td>
                    <a href="{gotw['url']}" target="_blank" style="text-decoration:none;font-size:0;line-height:0;">
                      <img src="{gotw['image_url']}" alt="" width="100%" style="display:block;border-radius:10px 10px 0 0;max-height:215px;object-fit:cover;" />
                    </a>
                    <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#1a1a2e;border-radius:0 0 10px 10px;border-left:4px solid #FFD700;">
                      <tr>
                        <td style="padding:16px 20px;">
                          <a href="{gotw['url']}" target="_blank" style="text-decoration:none;">
                            <p style="margin:0 0 8px;font-family:'Courier New',monospace;font-size:16px;font-weight:900;color:#FFD700;">{gotw['title']}</p>
                          </a>
                          <p style="margin:0 0 10px;font-family:Arial,sans-serif;font-size:13px;color:#c0c0d0;line-height:1.6;">{gotw['description']}</p>
                          <p style="margin:0 0 12px;font-family:'Courier New',monospace;font-size:11px;color:#4A9EFF;text-transform:uppercase;letter-spacing:1px;">🎮 {gotw['platform']}</p>
                          <a href="{gotw['url']}" target="_blank" style="display:inline-block;background:#1e3a8a;border-radius:6px;padding:8px 18px;font-family:'Courier New',monospace;font-size:12px;color:#ffffff;text-decoration:none;letter-spacing:1px;">Check It Out →</a>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>
            </td>
          </tr>"""

    # Fetch upcoming game releases
    try:
        releases = fetch_upcoming_releases()
    except Exception as ex:
        print(f"[MAILCHIMP] Could not fetch releases (non-fatal): {ex}")
        releases = []

    # Build releases section HTML
    if releases:
        release_rows = ""
        for rel in releases:
            platforms = ", ".join(rel.get("platforms", [])[:3])
            cover     = rel.get("cover_url", "")
            name      = rel.get("name", "")
            rel_date  = rel.get("date_str", "")
            cover_html = f'<img src="{cover}" width="40" height="53" style="display:block;border-radius:4px;object-fit:cover;" />' if cover else '<div style="width:40px;height:53px;background:#1e1e3f;border-radius:4px;"></div>'
            release_rows += f"""
              <tr>
                <td style="padding:0 0 10px 0;">
                  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#1a1a2e;border-radius:8px;padding:10px 14px;">
                    <tr>
                      <td width="50" style="vertical-align:middle;padding-right:12px;">{cover_html}</td>
                      <td style="vertical-align:middle;">
                        <p style="margin:0 0 2px;font-family:'Courier New',monospace;font-size:13px;font-weight:700;color:#ffffff;">{name}</p>
                        <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;color:#4A9EFF;">📅 {rel_date} &nbsp;·&nbsp; {platforms}</p>
                      </td>
                    </tr>
                  </table>
                </td>
              </tr>"""

        releases_section = f"""
          <!-- UPCOMING RELEASES -->
          <tr>
            <td style="background:#0f0f24;padding:0 30px 28px;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td style="border-top:1px solid #1e3a8a;padding-top:24px;padding-bottom:14px;">
                    <p style="margin:0;font-family:'Courier New',monospace;font-size:12px;color:#4A9EFF;text-align:center;letter-spacing:2px;text-transform:uppercase;">— Upcoming Releases —</p>
                  </td>
                </tr>
                {release_rows}
              </table>
            </td>
          </tr>"""
    else:
        releases_section = ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Itty Bitty Gaming News — {date_str}</title>
</head>
<body style="margin:0;padding:0;background-color:#0a0a0f;font-family:Arial,sans-serif;">

  <!-- Wrapper -->
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#0a0a0f;">
    <tr>
      <td align="center" style="padding:30px 15px;">

        <!-- Container -->
        <table width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;width:100%;">

          <!-- HEADER -->
          <tr>
            <td align="center" style="background:linear-gradient(135deg,#0d0d1a 0%,#1a1a3e 100%);border-radius:16px 16px 0 0;padding:40px 30px 30px;border-bottom:3px solid #1e3a8a;">
              <img src="{LOGO_URL}" alt="Itty Bitty Gaming News" width="160" style="display:block;margin:0 auto 16px;" />
              <h1 style="margin:0 0 6px;font-family:'Courier New',monospace;font-size:26px;font-weight:900;color:#ffffff;letter-spacing:2px;text-transform:uppercase;">ITTY BITTY GAMING NEWS</h1>
              <p style="margin:0;font-family:Arial,sans-serif;font-size:13px;color:#4A9EFF;letter-spacing:3px;text-transform:uppercase;">Daily Digest — {date_str}</p>
            </td>
          </tr>

          <!-- INTRO BAR -->
          <tr>
            <td style="background:#111130;padding:14px 30px;border-bottom:1px solid #1e3a8a;">
              <p style="margin:0;font-family:'Courier New',monospace;font-size:13px;color:#a0a0c0;text-align:center;">🎮 Today's top snackable gaming stories.</p>
            </td>
          </tr>

          <!-- STORIES -->
          <tr>
            <td style="background:#0f0f24;padding:28px 30px 8px;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0">
                {story_rows}
              </table>
            </td>
          </tr>

          {youtube_section}

          {gotw_section}

          <!-- DAILY TRIVIA -->
          <tr>
            <td style="background:#0f0f24;padding:0 30px 28px;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td style="border-top:1px solid #1e3a8a;padding-top:24px;padding-bottom:14px;">
                    <p style="margin:0;font-family:'Courier New',monospace;font-size:12px;color:#4A9EFF;text-align:center;letter-spacing:2px;text-transform:uppercase;">— 🎮 Daily Trivia —</p>
                  </td>
                </tr>
                <tr>
                  <td style="background:#1a1a2e;border-radius:10px;border-left:4px solid #4A9EFF;padding:16px 20px;">
                    <p style="margin:0 0 10px;font-family:'Courier New',monospace;font-size:14px;color:#4A9EFF;">🕹️ Today's Question:</p>
                    <p style="margin:0 0 16px;font-family:'Courier New',monospace;font-size:14px;color:#ffffff;line-height:1.5;">{trivia_question}</p>
                    <table width="100%" cellpadding="0" cellspacing="0" border="0">
                      <tr>
                        <td style="background:#0a2a0a;border-radius:6px;border:1px solid #1a4a1a;padding:12px 16px;">
                          <p style="margin:0 0 6px;font-family:'Courier New',monospace;font-size:10px;color:#4A9EFF;letter-spacing:2px;text-transform:uppercase;">✅ Answer:</p>
                          <p style="margin:0;font-family:'Courier New',monospace;font-size:13px;color:#a0ffa0;line-height:1.5;">{trivia_answer}</p>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          {releases_section}

          <!-- DIVIDER -->
          <tr>
            <td style="background:#0f0f24;padding:0 30px 28px;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td style="border-top:1px solid #1e3a8a;padding-top:24px;">
                    <p style="margin:0;font-family:'Courier New',monospace;font-size:12px;color:#4A9EFF;text-align:center;letter-spacing:2px;text-transform:uppercase;">— Find Us Online —</p>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- LINKS ROW -->
          <tr>
            <td style="background:#0f0f24;padding:0 30px 32px;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td align="center">
                    <table cellpadding="0" cellspacing="0" border="0">
                      <tr>
                        <td style="padding:0 8px;">
                          <a href="{yt_link}" style="display:inline-block;background:#1e1e3f;border:1px solid #1e3a8a;border-radius:8px;padding:10px 18px;font-family:'Courier New',monospace;font-size:12px;color:#ffffff;text-decoration:none;letter-spacing:1px;" target="_blank">🎬 Latest Video</a>
                        </td>
                        <td style="padding:0 8px;">
                          <a href="{PODCAST_URL}" style="display:inline-block;background:#1e1e3f;border:1px solid #1e3a8a;border-radius:8px;padding:10px 18px;font-family:'Courier New',monospace;font-size:12px;color:#ffffff;text-decoration:none;letter-spacing:1px;" target="_blank">🎙️ Podcast</a>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

          <!-- FOOTER -->
          <tr>
            <td align="center" style="background:#080812;border-radius:0 0 16px 16px;padding:24px 30px;border-top:3px solid #1e3a8a;">
              <p style="margin:0 0 8px;font-family:'Courier New',monospace;font-size:13px;color:#4A9EFF;font-style:italic;">{TAGLINE}</p>
              <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;color:#404060;">
                You're receiving this because you subscribed to Itty Bitty Gaming News.<br>
                <a href="*|UNSUB|*" style="color:#4A9EFF;">Unsubscribe</a>
              </p>
            </td>
          </tr>

        </table>
        <!-- End Container -->

      </td>
    </tr>
  </table>

</body>
</html>"""

# ---------------------------------------------------------------------------
# MAILCHIMP CAMPAIGN
# ---------------------------------------------------------------------------

def send_campaign(stories: list, latest_yt_url: str = None, post_date: str = "") -> None:
    # Use date from digest export (set at digest run time in PT)
    # Fall back to current PT time if not available
    if not post_date:
        try:
            from zoneinfo import ZoneInfo
            today = datetime.now(ZoneInfo("America/Los_Angeles"))
        except Exception:
            today = datetime.now(timezone(timedelta(hours=-7)))
        post_date = today.strftime("%B %-d, %Y")

    date_str  = post_date
    # Extract short date for subject line (e.g. "April 13" from "April 13, 2026")
    short_date = post_date.rsplit(",", 1)[0] if "," in post_date else post_date
    subject   = f"🎮 Itty Bitty Gaming News — {short_date}"
    html_body = build_html_email(stories, date_str, latest_yt_url)
    print(f"[MAILCHIMP] Using date: {date_str}")

    # 1. Create campaign
    print("[MAILCHIMP] Creating campaign...")
    campaign = mc_post("/campaigns", {
        "type": "regular",
        "recipients": {"list_id": MAILCHIMP_AUDIENCE_ID},
        "settings": {
            "subject_line":  subject,
            "preview_text":  f"Today's top {len(stories)} gaming stories — served itty bitty.",
            "title":         f"IBGN Digest {post_date}",
            "from_name":     "Itty Bitty Gaming News",
            "reply_to":      "ittybittygamingnews@gmail.com",
        },
    })
    campaign_id = campaign.get("id")
    if not campaign_id:
        print(f"[MAILCHIMP] Failed to create campaign: {campaign}")
        sys.exit(1)
    print(f"[MAILCHIMP] Campaign created: {campaign_id}")

    # 2. Set HTML content
    print("[MAILCHIMP] Setting email content...")
    r = requests.put(
        f"{BASE}/campaigns/{campaign_id}/content",
        headers=headers(),
        json={"html": html_body},
        timeout=30,
    )
    if not r.ok:
        print(f"[MAILCHIMP] Content error {r.status_code}: {r.text[:500]}")
        r.raise_for_status()

    # 3. Check campaign status before sending
    print("[MAILCHIMP] Checking campaign status...")
    check = requests.get(f"{BASE}/campaigns/{campaign_id}", headers=headers(), timeout=30)
    if check.ok:
        data = check.json()
        status = data.get("status")
        errors = data.get("delivery_status", {})
        print(f"[MAILCHIMP] Campaign status: {status}")
        print(f"[MAILCHIMP] Delivery status: {errors}")

    # 4. Send immediately
    print("[MAILCHIMP] Sending campaign...")
    r = requests.post(
        f"{BASE}/campaigns/{campaign_id}/actions/send",
        headers=headers(),
        timeout=30,
    )
    if not r.ok:
        print(f"[MAILCHIMP] Send error {r.status_code}: {r.text[:1000]}")
        r.raise_for_status()

    print(f"[MAILCHIMP] ✅ Campaign sent to audience {MAILCHIMP_AUDIENCE_ID}!")

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    if not MAILCHIMP_API_KEY:
        print("[MAILCHIMP] MAILCHIMP_API_KEY not set — skipping.")
        sys.exit(0)
    if not MAILCHIMP_AUDIENCE_ID:
        print("[MAILCHIMP] MAILCHIMP_AUDIENCE_ID not set — skipping.")
        sys.exit(0)

    should_post, stories, latest_yt_url, post_date = load_digest_stories()

    if not should_post:
        print("[MAILCHIMP] Digest not ready — skipping email.")
        sys.exit(0)

    if not stories:
        print("[MAILCHIMP] No stories found — skipping email.")
        sys.exit(0)

    print(f"[MAILCHIMP] Loaded {len(stories)} stories. Fetching story images...")
    stories = enrich_stories_with_images(stories)
    print(f"[MAILCHIMP] Sending email digest...")
    send_campaign(stories, latest_yt_url, post_date)

if __name__ == "__main__":
    main()
