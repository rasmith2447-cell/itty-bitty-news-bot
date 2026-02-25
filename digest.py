# =============================
# ADILO (ROBUST PUBLIC SCRAPE)
# =============================

import time
import random

def _adilo_http_get(url: str, timeout: int = 20) -> str:
    """
    Fetch a URL with conservative headers and retries handled at a higher layer.
    Returns response text (HTML).
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    r = SESSION.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text


def _adilo_extract_ids_from_html(html: str) -> list[str]:
    """
    Extract candidate Adilo IDs from a page using multiple patterns.
    Returns a list of IDs in the order they were discovered.
    """
    if not html:
        return []

    # Normalize for regex scanning
    text = html

    patterns = [
        r"https?://adilo\.bigcommand\.com/watch/([A-Za-z0-9_-]{6,})",
        r"/watch/([A-Za-z0-9_-]{6,})",
        r"video\?id=([A-Za-z0-9_-]{6,})",
        r"/stage/videos/([A-Za-z0-9_-]{6,})",
        r"https?://adilo\.bigcommand\.com/stage/videos/([A-Za-z0-9_-]{6,})",
    ]

    found: list[str] = []
    seen = set()

    for pat in patterns:
        for m in re.findall(pat, text):
            vid = m.strip()
            if not vid or vid in seen:
                continue
            seen.add(vid)
            found.append(vid)

    # Also parse DOM for href/src attributes (sometimes the ID is only in attributes)
    try:
        soup = BeautifulSoup(str(html), "html.parser")

        attrs = []
        # Any iframe src
        for iframe in soup.find_all("iframe"):
            src = iframe.get("src") or ""
            attrs.append(src)

        # Any link href
        for a in soup.find_all("a"):
            href = a.get("href") or ""
            attrs.append(href)

        # Any meta content (rare but happens)
        for meta in soup.find_all("meta"):
            content = meta.get("content") or ""
            attrs.append(content)

        # Scan attributes for IDs
        blob = "\n".join(attrs)
        for pat in patterns:
            for m in re.findall(pat, blob):
                vid = m.strip()
                if not vid or vid in seen:
                    continue
                seen.add(vid)
                found.append(vid)

    except Exception:
        # If BeautifulSoup chokes, regex extraction already ran.
        pass

    return found


def _adilo_pick_best_id(candidate_ids: list[str]) -> str:
    """
    Heuristic: prefer IDs that look like newer 'watch' IDs but we don't have timestamps.
    We pick the *first* discovered from the "latest" page scan order.
    """
    if not candidate_ids:
        return ""

    # If any candidate looks like it came from /watch/, keep order but prioritize those IDs
    # (candidate list already in discovery order; we just stabilize preference)
    return candidate_ids[0]


def scrape_latest_adilo_watch_url() -> str:
    """
    Robust public scrape for latest Adilo video.
    Returns a watch URL if possible, else falls back to home hub.
    """
    # Try multiple public URLs/variants to survive slow/cached responses.
    base = ADILO_PUBLIC_LATEST_PAGE.rstrip("/")

    # Cache busters reduce stale HTML
    cb = f"cb={int(time.time())}{random.randint(100,999)}"

    candidates = [
        base,  # https://adilo.bigcommand.com/c/ittybittygamingnews/video
        f"{base}?{cb}",
        f"{base}/?{cb}",
        f"{base}?id=&{cb}",     # sometimes template routes behave differently
        f"{base}?video=latest&{cb}",
        # If your "home" is faster sometimes, we scan it too, but we never prefer it unless it yields an ID:
        ADILO_PUBLIC_HOME_PAGE.rstrip("/"),
        f"{ADILO_PUBLIC_HOME_PAGE.rstrip('/')}?{cb}",
    ]

    # Retry strategy: more than one pass, with decreasing timeouts to avoid hanging Actions.
    attempts = [
        {"timeout": 25, "sleep": 0.0},
        {"timeout": 18, "sleep": 1.0},
        {"timeout": 12, "sleep": 1.5},
    ]

    best_id = ""

    for i, att in enumerate(attempts, start=1):
        timeout = att["timeout"]
        sleep_s = att["sleep"]

        if sleep_s:
            time.sleep(sleep_s)

        for url in candidates:
            try:
                print(f"[ADILO] SCRAPE attempt={i} timeout={timeout} url={url}")
                html = _adilo_http_get(url, timeout=timeout)

                ids = _adilo_extract_ids_from_html(html)
                if ids:
                    picked = _adilo_pick_best_id(ids)
                    if picked:
                        best_id = picked
                        watch_url = f"https://adilo.bigcommand.com/watch/{best_id}"
                        print(f"[ADILO] Found candidate id={best_id} -> {watch_url}")
                        return watch_url

            except requests.exceptions.ReadTimeout:
                print(f"[ADILO] Timeout url={url} (timeout={timeout})")
                continue
            except Exception as ex:
                print(f"[ADILO] Error url={url}: {ex}")
                continue

    # If we got here, no ID could be extracted
    print(f"[ADILO] Falling back: {ADILO_PUBLIC_HOME_PAGE}")
    return ADILO_PUBLIC_HOME_PAGE
