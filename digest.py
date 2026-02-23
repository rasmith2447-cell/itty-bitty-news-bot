import os
import re
from typing import Any, Dict, List, Optional, Tuple
import requests

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

ADILO_PUBLIC_KEY = os.getenv("ADILO_PUBLIC_KEY", "").strip()
ADILO_SECRET_KEY = os.getenv("ADILO_SECRET_KEY", "").strip()
ADILO_PROJECT_ID = os.getenv("ADILO_PROJECT_ID", "").strip()

ADILO_API_BASE = "https://adilo-api.bigcommand.com/v1"

FEATURED_VIDEO_TITLE = os.getenv("FEATURED_VIDEO_TITLE", "Watch today’s Itty Bitty Gaming News").strip()
FEATURED_VIDEO_FALLBACK_URL = os.getenv(
    "FEATURED_VIDEO_FALLBACK_URL",
    "https://adilo.bigcommand.com/c/ittybittygamingnews/home"
).strip()

USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNews/AdiloPayloadAware").strip()


def post_to_discord(text: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL is not set")
    r = requests.post(DISCORD_WEBHOOK_URL, json={"content": text}, timeout=25)
    r.raise_for_status()


def adilo_headers() -> Dict[str, str]:
    return {
        "X-Public-Key": ADILO_PUBLIC_KEY,
        "X-Secret-Key": ADILO_SECRET_KEY,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }


def safe_snippet(s: Any, max_len: int = 900) -> str:
    out = str(s)
    if ADILO_PUBLIC_KEY:
        out = out.replace(ADILO_PUBLIC_KEY, "[REDACTED_PUBLIC_KEY]")
    if ADILO_SECRET_KEY:
        out = out.replace(ADILO_SECRET_KEY, "[REDACTED_SECRET_KEY]")
    out = re.sub(r"\s+", " ", out).strip()
    if len(out) > max_len:
        out = out[:max_len] + "..."
    return out


def adilo_get_json(url: str) -> Tuple[int, Any]:
    r = requests.get(url, headers=adilo_headers(), timeout=30)
    print(f"[ADILO] GET {url} -> HTTP {r.status_code}")
    if r.status_code >= 400:
        print("[ADILO] Error body snippet:", safe_snippet(r.text, 1200))
        return r.status_code, None
    try:
        return r.status_code, r.json()
    except Exception:
        print("[ADILO] Non-JSON response snippet:", safe_snippet(r.text, 1200))
        return r.status_code, None


def extract_watch_id_from_any(obj: Any) -> Optional[str]:
    """
    Find a watch id from:
      - /watch/<id>
      - ?id=<id>
    Also scan common fields.
    """
    if obj is None:
        return None

    if isinstance(obj, str):
        m = re.search(r"/watch/([A-Za-z0-9_-]{6,})", obj)
        if m:
            return m.group(1)
        m = re.search(r"[?&]id=([A-Za-z0-9_-]{6,})", obj)
        if m:
            return m.group(1)
        return None

    if isinstance(obj, list):
        for x in obj:
            got = extract_watch_id_from_any(x)
            if got:
                return got
        return None

    if isinstance(obj, dict):
        for k in [
            "watch_id", "watchId", "public_id", "publicId",
            "short_id", "shortId", "code",
            "video_id", "videoId",
            "share_id", "shareId",
            "watch_url", "watchUrl",
            "url", "link",
            "embed", "embed_url", "embedUrl",
        ]:
            if k in obj:
                got = extract_watch_id_from_any(obj.get(k))
                if got:
                    return got

        for v in obj.values():
            got = extract_watch_id_from_any(v)
            if got:
                return got

    return None


def find_list_anywhere(obj: Any) -> List[Dict[str, Any]]:
    """
    Recursively find the FIRST list-of-dicts that looks like a file list.
    This handles Adilo wrapping inside: {status, message, payload:{...}}
    """
    if obj is None:
        return []

    if isinstance(obj, list):
        # If it's already a list of dicts, return it
        if obj and all(isinstance(x, dict) for x in obj):
            return obj  # type: ignore
        # Otherwise search within
        for x in obj:
            got = find_list_anywhere(x)
            if got:
                return got
        return []

    if isinstance(obj, dict):
        # Most likely keys
        for key in ["files", "items", "results", "data", "videos", "contents"]:
            val = obj.get(key)
            if isinstance(val, list) and val and all(isinstance(x, dict) for x in val):
                return val  # type: ignore

        # Common Adilo wrapper
        if "payload" in obj:
            got = find_list_anywhere(obj.get("payload"))
            if got:
                return got

        # Search all values
        for v in obj.values():
            got = find_list_anywhere(v)
            if got:
                return got

    return []


def debug_payload_shape(label: str, data: Any) -> None:
    if isinstance(data, dict):
        print(f"[ADILO] {label} top-level keys:", ", ".join(list(data.keys())[:40]))
        payload = data.get("payload")
        if isinstance(payload, dict):
            print(f"[ADILO] {label} payload keys:", ", ".join(list(payload.keys())[:40]))
        elif payload is not None:
            print(f"[ADILO] {label} payload type:", type(payload).__name__)
    else:
        print(f"[ADILO] {label} type:", type(data).__name__)


def get_latest_watch_url(project_id: str) -> Optional[str]:
    # This endpoint is documented and is the one returning JSON for you
    url = f"{ADILO_API_BASE}/projects/{project_id}/files?From=1&To=50"
    _, data = adilo_get_json(url)
    debug_payload_shape("FILES", data)

    # First: try to extract a watch id anywhere in the response
    wid = extract_watch_id_from_any(data)
    if wid:
        print("[ADILO] Found watch id directly in files response.")
        return f"https://adilo.bigcommand.com/watch/{wid}"

    # Second: try to locate a list of file dicts inside payload
    items = find_list_anywhere(data)
    print(f"[ADILO] Items found by recursive search: {len(items)}")

    # Log a few items safely + attempt to extract watch id from each
    for i, it in enumerate(items[:15], start=1):
        fid = it.get("id")
        title = it.get("title") or it.get("name") or ""
        wid2 = extract_watch_id_from_any(it)
        print(f"[ADILO]  - item#{i} id={fid} title={safe_snippet(title, 120)} watch_id_found={bool(wid2)}")
        if wid2:
            return f"https://adilo.bigcommand.com/watch/{wid2}"

    # Third: if items exist but no watch id is present, try meta endpoint on first few ids
    for it in items[:10]:
        fid = it.get("id")
        if not fid:
            continue
        meta_url = f"{ADILO_API_BASE}/files/{fid}/meta"
        _, meta = adilo_get_json(meta_url)
        debug_payload_shape(f"META file_id={fid}", meta)
        wid3 = extract_watch_id_from_any(meta)
        if wid3:
            print("[ADILO] Found watch id via file meta.")
            return f"https://adilo.bigcommand.com/watch/{wid3}"

    return None


def main():
    if not ADILO_PUBLIC_KEY or not ADILO_SECRET_KEY:
        raise RuntimeError("Missing ADILO_PUBLIC_KEY / ADILO_SECRET_KEY (GitHub repo secrets).")
    if not ADILO_PROJECT_ID:
        raise RuntimeError("Missing ADILO_PROJECT_ID (set it in workflow env).")

    watch_url = None
    try:
        watch_url = get_latest_watch_url(ADILO_PROJECT_ID)
    except Exception as e:
        print("[ADILO] Unexpected error:", e)

    final_url = watch_url or FEATURED_VIDEO_FALLBACK_URL
    post_to_discord(f"📺 [{FEATURED_VIDEO_TITLE}]({final_url})")
    print("Posted featured video:", final_url)


if __name__ == "__main__":
    main()
