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

USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNews/AdiloMultiEndpoint").strip()


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


def safe_snippet(s: Any, max_len: int = 1100) -> str:
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


def normalize_list_from_response(data: Any) -> List[Dict[str, Any]]:
    if data is None:
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ["data", "files", "videos", "results", "items", "contents"]:
            val = data.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
        d = data.get("data")
        if isinstance(d, dict):
            for key in ["files", "videos", "results", "items", "contents"]:
                val = d.get(key)
                if isinstance(val, list):
                    return [x for x in val if isinstance(x, dict)]
    return []


def extract_watch_id_from_any(obj: Any) -> Optional[str]:
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
            "url", "link"
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


def try_endpoints_for_items(project_id: str) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Try multiple likely endpoints for "videos/files under project".
    Returns (endpoint_used, items)
    """
    endpoints = [
        f"{ADILO_API_BASE}/projects/{project_id}/files?From=1&To=50",
        f"{ADILO_API_BASE}/projects/{project_id}/videos?From=1&To=50",
        f"{ADILO_API_BASE}/projects/{project_id}/contents?From=1&To=50",
        f"{ADILO_API_BASE}/projects/{project_id}/media?From=1&To=50",
        # reverse paging attempts (some APIs interpret From/To as offsets)
        f"{ADILO_API_BASE}/projects/{project_id}/files?From=0&To=50",
        f"{ADILO_API_BASE}/projects/{project_id}/videos?From=0&To=50",
    ]

    for url in endpoints:
        _, data = adilo_get_json(url)
        items = normalize_list_from_response(data)
        print(f"[ADILO] Items from endpoint: {len(items)}")
        if items:
            return url, items

    return "(none)", []


def try_project_detail(project_id: str) -> Dict[str, Any]:
    url = f"{ADILO_API_BASE}/projects/{project_id}"
    _, data = adilo_get_json(url)
    if isinstance(data, dict):
        return data
    return {}


def try_file_meta(file_id: str) -> Optional[str]:
    url = f"{ADILO_API_BASE}/files/{file_id}/meta"
    _, data = adilo_get_json(url)
    wid = extract_watch_id_from_any(data)
    if wid:
        return f"https://adilo.bigcommand.com/watch/{wid}"
    return None


def get_latest_watch_url(project_id: str) -> Optional[str]:
    used, items = try_endpoints_for_items(project_id)
    if items:
        print(f"[ADILO] Using endpoint: {used}")

        # Try to find watch id in the first 20 items
        for i, it in enumerate(items[:20], start=1):
            wid = extract_watch_id_from_any(it)
            item_id = it.get("id") if isinstance(it, dict) else None
            title = ""
            if isinstance(it, dict):
                title = it.get("title") or it.get("name") or ""
            print(f"[ADILO]  - item#{i} id={item_id} title={safe_snippet(title, 120)} watch_id_found={bool(wid)}")
            if wid:
                return f"https://adilo.bigcommand.com/watch/{wid}"

        # If no watch id in list objects, try meta calls for first few item IDs
        for it in items[:10]:
            if not isinstance(it, dict):
                continue
            fid = it.get("id")
            if not fid:
                continue
            w = try_file_meta(str(fid))
            if w:
                return w

        return None

    # If list endpoints returned nothing, inspect project detail to discover related links/ids
    detail = try_project_detail(project_id)
    if detail:
        print("[ADILO] Project detail keys:", ", ".join(list(detail.keys())[:40]))
        # Sometimes detail includes a different id for content listing. We'll scan for IDs/URLs.
        wid = extract_watch_id_from_any(detail)
        if wid:
            return f"https://adilo.bigcommand.com/watch/{wid}"

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
