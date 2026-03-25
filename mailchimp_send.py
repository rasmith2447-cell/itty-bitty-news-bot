#!/usr/bin/env python3
"""
mailchimp_send.py — Itty Bitty Gaming News
Reads digest_latest.json and sends a branded HTML email campaign
via the Mailchimp API to the IBGN audience.
"""

import json
import os
import sys
from datetime import datetime, timezone

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

# Mailchimp datacenter is the suffix after the dash in the API key (e.g. us9)
DC = MAILCHIMP_API_KEY.split("-")[-1] if "-" in MAILCHIMP_API_KEY else "us1"
BASE = f"https://{DC}.api.mailchimp.com/3.0"

# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

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
    """Generate a weekly gaming trivia Q&A using the Anthropic API."""
    try:
        import anthropic
        client = anthropic.Anthropic()
        # Use ISO week number so the same question is generated all week
        from datetime import date
        try:
            from zoneinfo import ZoneInfo
            from datetime import datetime as _dt
            today_pt = _dt.now(ZoneInfo("America/Los_Angeles")).date()
        except Exception:
            today_pt = date.today()
        week = today_pt.isocalendar()[1]
        year = today_pt.year
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{
                "role": "user",
                "content": (
                    f"Generate a fun gaming trivia question for week {week} of {year}. "
                    "It should be about video game history, characters, or notable moments. "
                    "Make it challenging but not obscure. "
                    "Respond with ONLY a JSON object in this exact format with no other text: "
                    '{"question": "...", "answer": "..."}'
                )
            }]
        )
        import json as _json
        data = _json.loads(message.content[0].text.strip())
        return data.get("question", ""), data.get("answer", "")
    except Exception as ex:
        print(f"[MAILCHIMP] Trivia generation failed (non-fatal): {ex}")
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
    trivia_question, trivia_answer = generate_trivia()

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

          <!-- TRIVIA -->
          <tr>
            <td style="background:#0f0f24;padding:0 30px 28px;">
              <table width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr>
                  <td style="border-top:1px solid #1e3a8a;padding-top:24px;padding-bottom:14px;">
                    <p style="margin:0;font-family:'Courier New',monospace;font-size:12px;color:#4A9EFF;text-align:center;letter-spacing:2px;text-transform:uppercase;">— Weekly Trivia —</p>
                  </td>
                </tr>
                <tr>
                  <td style="background:#1a1a2e;border-radius:10px;border-left:4px solid #FFD700;padding:16px 20px;">
                    <p style="margin:0 0 10px;font-family:'Courier New',monospace;font-size:14px;color:#FFD700;">🏆 This Week's Question:</p>
                    <p style="margin:0 0 16px;font-family:'Courier New',monospace;font-size:14px;color:#ffffff;line-height:1.5;">{trivia_question}</p>
                    <table width="100%" cellpadding="0" cellspacing="0" border="0">
                      <tr>
                        <td style="background:#0d0d1a;border-radius:6px;border:1px dashed #4A9EFF;padding:12px 16px;">
                          <p style="margin:0 0 4px;font-family:'Courier New',monospace;font-size:10px;color:#4A9EFF;letter-spacing:2px;text-transform:uppercase;">✅ Answer:</p>
                          <p style="margin:0;font-family:'Courier New',monospace;font-size:13px;color:#a0ffa0;line-height:1.5;">{trivia_answer}</p>
                        </td>
                      </tr>
                    </table>
                  </td>
                </tr>
              </table>
            </td>
          </tr>

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

    print(f"[MAILCHIMP] Loaded {len(stories)} stories. Sending email digest...")
    send_campaign(stories, latest_yt_url)

if __name__ == "__main__":
    main()
