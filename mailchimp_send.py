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
TAGLINE             = "Your daily dose of Itty Bitty Gaming News."

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
    """Returns (should_post: bool, stories: list, youtube_url: str)"""
    if os.path.exists(DIGEST_EXPORT_FILE):
        try:
            with open(DIGEST_EXPORT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    should_post = data.get("should_post", False)
                    stories     = data.get("stories", [])
                    yt_url      = data.get("youtube_url") or YOUTUBE_URL
                    return should_post, stories, yt_url
                if isinstance(data, list):
                    return len(data) > 0, data, YOUTUBE_URL
        except Exception as ex:
            print(f"[MAILCHIMP] Could not read {DIGEST_EXPORT_FILE}: {ex}")
    print(f"[MAILCHIMP] {DIGEST_EXPORT_FILE} not found — skipping.")
    return False, [], YOUTUBE_URL

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
                <td style="padding:0 0 0 0;">
                  {link_open}<img src="{image_url}" alt="{title}" width="100%" style="display:block;border-radius:6px 6px 0 0;max-height:200px;object-fit:cover;" />{link_close}
                </td>
              </tr>""" if image_url else ""

    return f"""
    <tr>
      <td style="padding:0 0 16px 0;">
        <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#1a1a2e;border-radius:10px;border-left:4px solid {color};overflow:hidden;">
          <tr>
            <td>{image_block and f'<table width="100%" cellpadding="0" cellspacing="0" border="0">{image_block}</table>'}</td>
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
        day = today_pt.strftime("%B %d, %Y")
        print(f"[TRIVIA] Generating question for {day}...")
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": (
                    f"Generate a fun gaming trivia question for {day}. "
                    "It should be about video game history, characters, or notable moments. "
                    "Make it challenging but not obscure. "
                    "Respond with ONLY a JSON object in this exact format with no other text: "
                    '{"question": "...", "answer": "..."}'
                )
            }]
        )
        import json as _json
        data = _json.loads(message.content[0].text.strip())
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
    thumb_url   = f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg" if video_id else ""

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

    # Generate trivia using Claude API
    # trivia_question, trivia_answer = generate_trivia()  # Disabled until API credits added

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
            date_str  = rel.get("date_str", "")
            cover_html = f'<img src="{cover}" width="40" height="53" style="display:block;border-radius:4px;object-fit:cover;" />' if cover else '<div style="width:40px;height:53px;background:#1e1e3f;border-radius:4px;"></div>'
            release_rows += f"""
              <tr>
                <td style="padding:0 0 10px 0;">
                  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#1a1a2e;border-radius:8px;padding:10px 14px;">
                    <tr>
                      <td width="50" style="vertical-align:middle;padding-right:12px;">{cover_html}</td>
                      <td style="vertical-align:middle;">
                        <p style="margin:0 0 2px;font-family:'Courier New',monospace;font-size:13px;font-weight:700;color:#ffffff;">{name}</p>
                        <p style="margin:0;font-family:Arial,sans-serif;font-size:11px;color:#4A9EFF;">📅 {date_str} &nbsp;·&nbsp; {platforms}</p>
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

def send_campaign(stories: list, latest_yt_url: str = None) -> None:
    # Use PT timezone so date matches the actual day the digest runs
    try:
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo("America/Los_Angeles"))
    except Exception:
        from datetime import timezone, timedelta
        today = datetime.now(timezone(timedelta(hours=-7)))  # PDT fallback
    date_str  = today.strftime("%B %-d, %Y")
    subject   = f"🎮 Itty Bitty Gaming News — {today.strftime('%B %-d')}"
    html_body = build_html_email(stories, date_str, latest_yt_url)

    # 1. Create campaign
    print("[MAILCHIMP] Creating campaign...")
    campaign = mc_post("/campaigns", {
        "type": "regular",
        "recipients": {"list_id": MAILCHIMP_AUDIENCE_ID},
        "settings": {
            "subject_line":  subject,
            "preview_text":  f"Today's top {len(stories)} gaming stories — served itty bitty.",
            "title":         f"IBGN Digest {today.strftime('%Y-%m-%d')}",
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

    should_post, stories, latest_yt_url = load_digest_stories()

    if not should_post:
        print("[MAILCHIMP] Digest not ready — skipping email.")
        sys.exit(0)

    if not stories:
        print("[MAILCHIMP] No stories found — skipping email.")
        sys.exit(0)

    print(f"[MAILCHIMP] Loaded {len(stories)} stories. Fetching story images...")
    stories = enrich_stories_with_images(stories)
    print(f"[MAILCHIMP] Sending email digest...")
    send_campaign(stories, latest_yt_url)

if __name__ == "__main__":
    main()
