import os
import re
from typing import Any, Dict, List, Optional
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

USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNews/AdiloProjectIdMode").strip()


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


def adilo_get_json(url: str) -> Any:
    r = requests.get(url, headers=adilo_headers(), timeout=30)
    print(f"[ADILO] GET {url} -> HTTP {r.status_code}")
    if r.status_code >= 400:
        print("[ADILO] Error body snippet:", safe_snippet(r.text, 1200))
        r.raise_for_status()
    try:
        return r.json()
    except Exception:
        print("[ADILO] Non-JSON response snippet:", safe_snippet(r.text, 1200))
        raise


def normalize_list_from_response(data: Any) -> List[Dict[str, Any]]:
    """
    Accept common shapes:
      - {"data": [...]}
      - {"files": [...]}
      - {"results": [...]}
      - {"data": {"items": [...]}}, etc
      - [...] (list root)
    """
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]

    if isinstance(data, dict):
        for key in ["data", "files", "results", "items"]:
            val = data.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
        d = data.get("data")
        if isinstance(d, dict):
            for key in ["files", "results", "items"]:
                val = d.get(key)
                if isinstance(val, list):
                    return [x for x in val if isinstance(x, dict)]
    return []


def extract_watch_id_from_any(obj: Any) -> Optional[str]:
    """
    Find a watch id from:
      - /watch/<id>
      - ?id=<id>
    Or scan common fields.
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


def get_latest_watch_url_from_project(project_id: str) -> Optional[str]:
    # This endpoint is documented: GET /v1/projects/{project_id}/files
    url = f"{ADILO_API_BASE}/projects/{project_id}/files?From=1&To=50"
    data = adilo_get_json(url)

    files = normalize_list_from_response(data)
    print(f"[ADILO] Files returned: {len(files)} for project_id={project_id}")

    # Try the first 15 items for a watch id
    for i, f in enumerate(files[:15], start=1):
        wid = extract_watch_id_from_any(f)
        fid = f.get("id")
        title = f.get("title") or f.get("name") or "(no title)"
        print(f"[ADILO]  - file#{i} id={fid} title={safe_snippet(title, 140)} watch_id_found={bool(wid)}")
        if wid:
            return f"https://adilo.bigcommand.com/watch/{wid}"

    # If watch id wasn’t present in list items, try meta endpoint on the first few
    for f in files[:10]:
        fid = f.get("id")
        if not fid:
            continue
        meta_url = f"{ADILO_API_BASE}/files/{fid}/meta"
        try:
            meta = adilo_get_json(meta_url)
            wid2 = extract_watch_id_from_any(meta)
            if wid2:
                return f"https://adilo.bigcommand.com/watch/{wid2}"
        except Exception as e:
            print(f"[ADILO] meta fetch failed for file_id={fid}: {e}")

    return None


def main():
    if not ADILO_PUBLIC_KEY or not ADILO_SECRET_KEY:
        raise RuntimeError("Missing ADILO_PUBLIC_KEY / ADILO_SECRET_KEY (GitHub repo secrets).")

    if not ADILO_PROJECT_ID:
        raise RuntimeError("Missing ADILO_PROJECT_ID. Set it in the workflow env to the project id (e.g., yz8kq5M8).")

    watch_url = None
    try:
        watch_url = get_latest_watch_url_from_project(ADILO_PROJECT_ID)
    except Exception as e:
        print("[ADILO] Failed to load project files:", e)

    final_url = watch_url or FEATURED_VIDEO_FALLBACK_URL
    post_to_discord(f"📺 [{FEATURED_VIDEO_TITLE}]({final_url})")
    print("Posted featured video:", final_url)


if __name__ == "__main__":
    main()
