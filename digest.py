from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os

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
