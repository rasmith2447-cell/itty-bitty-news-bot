# =========================
# POSTING TIME GUARD (ROBUST)
# =========================
# GitHub Actions schedules are best-effort and can start late.
# This guard posts if "now" is within +/- WINDOW minutes of the target time.
# That means: if GitHub starts at 8:23pm for a 7:00pm target, it can still post
# (as long as the window is wide enough).
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

def guard_should_post_now() -> bool:
    tz_name = os.getenv("DIGEST_GUARD_TZ", "America/Los_Angeles").strip()
    target_hour = int(os.getenv("DIGEST_GUARD_LOCAL_HOUR", "19").strip())  # 7pm default
    target_minute = int(os.getenv("DIGEST_GUARD_LOCAL_MINUTE", "0").strip())
    window_minutes = int(os.getenv("DIGEST_GUARD_WINDOW_MINUTES", "180").strip())  # default: 3 hours

    tz = ZoneInfo(tz_name)
    now_local = datetime.now(tz)

    # Target time today in local tz
    target_today = now_local.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)

    # Choose the closest target among yesterday/today/tomorrow (handles edge cases)
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
        f"Delta={delta_min:.1f}min > {window_minutes}min. Exiting without posting."
    )
    return False


# Wherever your script currently checks the guard and exits, replace it with this:
if not guard_should_post_now():
    # IMPORTANT: use sys.exit(0) so Actions does NOT show failure.
    import sys
    sys.exit(0)

# =========================
# END POSTING TIME GUARD
# =========================
