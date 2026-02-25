# =========================
# POSTING TIME GUARD (ROBUST)
# =========================
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import sys
import os

def guard_should_post_now() -> bool:
    tz_name = os.getenv("DIGEST_GUARD_TZ", "America/Los_Angeles").strip()
    target_hour = int(os.getenv("DIGEST_GUARD_LOCAL_HOUR", "19").strip())
    target_minute = int(os.getenv("DIGEST_GUARD_LOCAL_MINUTE", "0").strip())
    window_minutes = int(os.getenv("DIGEST_GUARD_WINDOW_MINUTES", "180").strip())

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
# MAIN EXECUTION WRAPPER
# =========================

def main():
    # Check guard before doing ANY work
    if not guard_should_post_now():
        print("[GUARD] Skipping post due to time window.")
        return

    # --- EVERYTHING BELOW THIS LINE SHOULD BE YOUR EXISTING DIGEST LOGIC ---
    # Example:
    # build_news_items()
    # fetch_latest_adilo()
    # build_embed_layout()
    # post_to_discord()
    #
    # Do NOT remove your digest logic.
    # Just ensure it happens inside this function.
    #
    run_digest_logic()  # ← Replace this with your actual existing entry call


if __name__ == "__main__":
    main()
