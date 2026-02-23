import os
import re
from typing import Any, Dict, List, Optional
import requests


DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

ADILO_PUBLIC_KEY = os.getenv("ADILO_PUBLIC_KEY", "").strip()
ADILO_SECRET_KEY = os.getenv("ADILO_SECRET_KEY", "").strip()
ADILO_PROJECT_SEARCH = os.getenv("ADILO_PROJECT_SEARCH", "Itty Bitty Gaming News").strip()

ADILO_API_BASE = "https://adilo-api.bigcommand.com/v1"

FEATURED_VIDEO_TITLE = os.getenv("FEATURED_VIDEO_TITLE", "Watch today’s Itty Bitty Gaming News").strip()
FEATURED_VIDEO_FALLBACK_URL = os.getenv(
    "FEATURED_VIDEO_FALLBACK_URL",
    "https://adilo.bigcommand.com/c/ittybittygamingnews/home"
).strip()

USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNews/AdiloApiDebug").strip()


def post_to_discord(text: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL is not set")
    r = requests.post(DISCORD_WEBHOOK_URL, json={"content": text}, timeout=25)
    r.raise_for_status()


def adilo_headers() -> Dict[str, str]:
    # Do NOT print these values anywhere.
    return {
        "X-Public-Key": ADILO_PUBLIC_KEY,
        "X-Secret-Key": ADILO_SECRET_KEY,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }


def safe_snippet(obj: Any, max_len: int = 800) -> str:
    s = str(obj)
    s = s.replace(ADILO_PUBLIC_KEY, "[REDACTED_PUBLIC_KEY]") if ADILO_PUBLIC_KEY else s
    s = s.replace(ADILO_SECRET_KEY, "[REDACTED_SECRET_KEY]") if ADILO_SECRET_KEY else s
    return (s[:max_len] + "…") if len(s) > max_len else s


def adilo_get(url: str) -> Any:
    r = requests.get(url, headers=adilo_headers(), timeout=30)
    # Log status (safe)
    print(f"[ADILO] GET {url} -> HTTP {r.status_code}")
    if r.status_code >= 400:
        print("[ADILO] Error body snippet:", safe_snippet(r.text, 1200))
        r.raise_for_status()
    try:
        return r.json()
    except Exception:
        print("[ADILO] Non-JSON response snippet:", safe_snippet(r.text, 1200))
        raise


def extract_watch_id_from_any(obj: Any) -> Optional[str]:
    """
    Find an ID from either:
    - /watch/<id>
    - ?id=<id>
    Or common id fields in JSON
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
        # Try likely keys first
        for k in [
            "watch_id", "watchId", "public_id", "publicId",
            "short_id", "shortId", "code",
            "video_id", "videoId",
            "share_id", "shareId",
            "url", "link", "watch_url", "watchUrl"
        ]:
            if k in obj:
                got = extract_watch_id_from_any(obj.get(k))
                if got:
                    return got
        # Otherwise scan all values
        for v in obj.values():
            got = extract_watch_id_from_any(v)
            if got:
                return got
    return None


def normalize_list_from_response(data: Any) -> List[Dict[str, Any]]:
    """
    Adilo docs don’t guarantee a single fixed response wrapper in the excerpt,
    so we handle common shapes:
      - {"data": [...]}
      - {"data": {"items": [...]}} etc
      - [...] (list root)
    """
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]

    if isinstance(data, dict):
        # direct list in common keys
        for key in ["data", "projects", "files", "results", "items"]:
            val = data.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
        # nested
        d = data.get("data")
        if isinstance(d, dict):
            for key in ["projects", "files", "results", "items"]:
                val = d.get(key)
                if isinstance(val, list):
                    return [x for x in val if isinstance(x, dict)]
    return []


def find_project_id() -> Optional[str]:
    # Endpoint is documented:
    # GET /v1/projects/search/{string}  [oai_citation:2‡developers.adilo.com](https://developers.adilo.com/)
    q = requests.utils.quote(ADILO_PROJECT_SEARCH, safe="")
    url = f"{ADILO_API_BASE}/projects/search/{q}?From=1&To=50"
    data = adilo_get(url)

    projects = normalize_list_from_response(data)
    print(f"[ADILO] Project search returned {len(projects)} item(s) for '{ADILO_PROJECT_SEARCH}'")

    # Print safe project titles/ids to help match the right project
    for p in projects[:10]:
        pid = p.get("id")
        title = p.get("title") or p.get("name") or "(no title field)"
        print(f"[ADILO]  - project id={pid} title={safe_snippet(title, 120)}")

    if not projects:
        return None

    # Prefer exact title match if possible
    for p in projects:
        title = (p.get("title") or p.get("name") or "").strip().lower()
        if title == ADILO_PROJECT_SEARCH.strip().lower() and p.get("id"):
            return str(p["id"])

    # Otherwise take the first result
    if projects[0].get("id"):
        return str(projects[0]["id"])
    return None


def get_latest_watch_url_from_project(project_id: str) -> Optional[str]:
    # Endpoint is documented:
    # GET /v1/projects/{project_id}/files  [oai_citation:3‡developers.adilo.com](https://developers.adilo.com/)
    url = f"{ADILO_API_BASE}/projects/{project_id}/files?From=1&To=50"
    data = adilo_get(url)

    files = normalize_list_from_response(data)
    print(f"[ADILO] Files list returned {len(files)} item(s) for project_id={project_id}")

    # Try to pull a watch id from the first ~10 items
    for i, f in enumerate(files[:10], start=1):
        wid = extract_watch_id_from_any(f)
        fid = f.get("id")
        title = f.get("title") or f.get("name") or "(no title)"
        print(f"[ADILO]  - file #{i}: id={fid} title={safe_snippet(title, 120)} watch_id_found={bool(wid)}")
        if wid:
            return f"https://adilo.bigcommand.com/watch/{wid}"

    return None


def main():
    # Hard stop if secrets aren’t actually flowing
    if not ADILO_PUBLIC_KEY or not ADILO_SECRET_KEY:
        raise RuntimeError(
            "Missing ADILO_PUBLIC_KEY / ADILO_SECRET_KEY. "
            "Add them in GitHub: Settings → Secrets and variables → Actions → Repository secrets."
        )

    project_id = find_project_id()
    if not project_id:
        print("[ADILO] Could not find project id from search. Falling back.")
        watch_url = FEATURED_VIDEO_FALLBACK_URL
    else:
        watch_url = get_latest_watch_url_from_project(project_id) or FEATURED_VIDEO_FALLBACK_URL

    msg = f"📺 [{FEATURED_VIDEO_TITLE}]({watch_url})"
    post_to_discord(msg)
    print("Posted featured video:", watch_url)


if __name__ == "__main__":
    main()
