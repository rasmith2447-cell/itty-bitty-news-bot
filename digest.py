import os
import re
from typing import Any, Dict, List, Optional

import requests


DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

ADILO_PUBLIC_KEY = os.getenv("ADILO_PUBLIC_KEY", "").strip()
ADILO_SECRET_KEY = os.getenv("ADILO_SECRET_KEY", "").strip()

ADILO_PROJECT_SEARCH = os.getenv("ADILO_PROJECT_SEARCH", "Itty Bitty Gaming News").strip()
ADILO_API_BASE = "https://adilo-api.bigcommand.com/v1"

FEATURED_VIDEO_FALLBACK_URL = os.getenv(
    "FEATURED_VIDEO_FALLBACK_URL",
    "https://adilo.bigcommand.com/c/ittybittygamingnews/home"
).strip()

USER_AGENT = os.getenv("USER_AGENT", "IttyBittyGamingNews/AdiloProjectDiscovery").strip()


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


def safe_snippet(s: Any, max_len: int = 180) -> str:
    out = str(s)
    out = out.replace(ADILO_PUBLIC_KEY, "[REDACTED_PUBLIC_KEY]") if ADILO_PUBLIC_KEY else out
    out = out.replace(ADILO_SECRET_KEY, "[REDACTED_SECRET_KEY]") if ADILO_SECRET_KEY else out
    out = re.sub(r"\s+", " ", out).strip()
    if len(out) > max_len:
        out = out[:max_len] + "..."
    return out


def adilo_get(url: str) -> Any:
    r = requests.get(url, headers=adilo_headers(), timeout=30)
    print(f"[ADILO] GET {url} -> HTTP {r.status_code}")
    if r.status_code >= 400:
        print("[ADILO] Error body:", safe_snippet(r.text, 1200))
        r.raise_for_status()
    try:
        return r.json()
    except Exception:
        print("[ADILO] Non-JSON response:", safe_snippet(r.text, 1200))
        raise


def normalize_list_from_response(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ["data", "projects", "results", "items"]:
            val = data.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
        d = data.get("data")
        if isinstance(d, dict):
            for key in ["projects", "results", "items"]:
                val = d.get(key)
                if isinstance(val, list):
                    return [x for x in val if isinstance(x, dict)]
    return []


def print_projects(label: str, projects: List[Dict[str, Any]]) -> None:
    print(f"[ADILO] {label}: {len(projects)} project(s)")
    for p in projects[:25]:
        pid = p.get("id")
        title = p.get("title") or p.get("name") or "(no title/name field)"
        print(f"[ADILO]  - id={pid} title={safe_snippet(title, 220)}")


def try_project_search() -> List[Dict[str, Any]]:
    q = requests.utils.quote(ADILO_PROJECT_SEARCH, safe="")
    url = f"{ADILO_API_BASE}/projects/search/{q}?From=1&To=50"
    data = adilo_get(url)
    return normalize_list_from_response(data)


def try_project_list_variants() -> List[Dict[str, Any]]:
    """
    Different APIs sometimes expose list endpoints slightly differently.
    We'll try a few common variants and accept whichever returns a non-empty list.
    """
    candidates = [
        f"{ADILO_API_BASE}/projects?From=1&To=50",
        f"{ADILO_API_BASE}/projects?from=1&to=50",
        f"{ADILO_API_BASE}/projects",
    ]
    for url in candidates:
        try:
            data = adilo_get(url)
            projects = normalize_list_from_response(data)
            if projects:
                return projects
        except Exception as e:
            print(f"[ADILO] Projects list attempt failed for {url}: {e}")
    return []


def main():
    if not ADILO_PUBLIC_KEY or not ADILO_SECRET_KEY:
        raise RuntimeError(
            "Missing ADILO_PUBLIC_KEY / ADILO_SECRET_KEY. Add them as GitHub repository secrets."
        )

    # 1) Try search (already known to be returning 0, but we log it anyway)
    try:
        searched = try_project_search()
        print_projects(f"Project search results for '{ADILO_PROJECT_SEARCH}'", searched)
    except Exception as e:
        print(f"[ADILO] Project search request failed: {e}")

    # 2) Try list endpoint(s)
    projects = try_project_list_variants()
    print_projects("Project list results", projects)

    # Post a short note so you know it ran; the IDs are in Actions logs.
    post_to_discord(
        "IBGN: Adilo project discovery ran. Check the GitHub Actions logs for project IDs/titles."
        f"\nFallback link (not final): {FEATURED_VIDEO_FALLBACK_URL}"
    )
    print("[DONE] Project discovery complete.")


if __name__ == "__main__":
    main()
