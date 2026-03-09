#!/usr/bin/env python3
"""
main.py — Itty Bitty Gaming News
RAW and BREAKING modes: fetches feeds, filters, and posts to Discord.
All heavy logic lives in shared.py.
"""

import os
from typing import Dict, List

from shared import (
    FEEDS,
    Item,
    fetch_all_feeds,
    getenv,
    is_breaking,
    is_duplicate_or_allowed_update,
    load_state,
    remember,
    save_state,
    discord_post_raw,
    hard_block,
    utcnow,
)

# ---------------------------------------------------------------------------
# CONFIG  (from environment)
# ---------------------------------------------------------------------------

DISCORD_WEBHOOK_URL  = getenv("DISCORD_WEBHOOK_URL")
MODE                 = getenv("MODE", "RAW").upper()          # RAW | DIGEST
SKIP_STATE_UPDATE    = getenv("SKIP_STATE_UPDATE", "0") == "1"
MAX_POSTS_PER_RUN    = int(getenv("MAX_POSTS_PER_RUN", "12"))
BREAKING_MODE        = getenv("BREAKING_MODE", "0") == "1"
BREAKING_MAX_AGE_HOURS = int(getenv("BREAKING_MAX_AGE_HOURS", "72"))
DEBUG                = getenv("DEBUG", "0") == "1"


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL is not set.")

    state = load_state()

    # --- Fetch + filter + cluster ---
    all_items, reasons = fetch_all_feeds(FEEDS)

    # In BREAKING mode re-filter by breaking signal after clustering
    if BREAKING_MODE:
        eligible: List[Item] = []
        for it in all_items:
            if is_breaking(it.title, it.summary, it.published_at, BREAKING_MAX_AGE_HOURS):
                eligible.append(it)
            else:
                r = hard_block(it.title, it.summary) or "NOT_BREAKING_KEYWORD_OR_TOO_OLD"
                reasons[r] = reasons.get(r, 0) + 1
        all_items = eligible

    # --- Post loop ---
    posted       = 0
    skipped_dupe = 0

    for item in all_items:
        if posted >= MAX_POSTS_PER_RUN:
            break

        if MODE != "DIGEST":
            if is_duplicate_or_allowed_update(item, state):
                skipped_dupe += 1
                continue

        try:
            discord_post_raw(item, DISCORD_WEBHOOK_URL)
            posted += 1
            print(f"[POSTED] {item.source}: {item.title}")

            if MODE != "DIGEST" and not SKIP_STATE_UPDATE:
                remember(item, state)

        except Exception as e:
            print(f"[ERROR] Post failed: {item.title} -> {e}")

    if MODE != "DIGEST" and not SKIP_STATE_UPDATE:
        save_state(state)

    # --- Summary ---
    print("\n════════════════════════════════")
    print(f"  MODE={MODE}  BREAKING_MODE={BREAKING_MODE}")
    print(f"  Eligible after filters : {len(all_items)}")
    print(f"  Skipped duplicates     : {skipped_dupe}")
    print(f"  Posted                 : {posted}")
    if reasons:
        top = sorted(reasons.items(), key=lambda x: x[1], reverse=True)[:10]
        print("  Top filter reasons:")
        for k, v in top:
            print(f"    • {k}: {v}")
    print("════════════════════════════════\n")


if __name__ == "__main__":
    main()
