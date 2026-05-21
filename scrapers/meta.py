"""Meta Ad Library scraper.

Priority order:
  1. Graph API with META_ACCESS_TOKEN (most reliable, paginated)
  2. facebook.com/ads/library/api/ unauthenticated (limited)
  3. Web scrape + __NEXT_DATA__ parse (fallback)
"""

import json
import os
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

GRAPH_API = "https://graph.facebook.com/v21.0/ads_archive"
AD_LIBRARY_API = "https://www.facebook.com/ads/library/api/"
AD_LIBRARY_WEB = "https://www.facebook.com/ads/library/"

_FIELDS = ",".join([
    "id",
    "ad_creative_bodies",
    "ad_creative_link_captions",
    "ad_creative_link_descriptions",
    "ad_creative_link_titles",
    "ad_delivery_start_time",
    "ad_delivery_stop_time",
    "impressions",
    "publisher_platforms",
    "ad_snapshot_url",
])

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def scrape(advertiser: str, limit: int = 100) -> list[dict]:
    token = os.environ.get("META_ACCESS_TOKEN", "").strip()

    if token:
        ads = _graph_api(advertiser, token, limit)
        if ads:
            return ads

    ads = _library_api(advertiser, limit)
    if ads:
        return ads

    ads = _web_scrape(advertiser, limit)
    if ads:
        return ads

    return _playwright_scrape(advertiser, limit)


# ---------------------------------------------------------------------------
# Strategy 1 — Graph API
# ---------------------------------------------------------------------------

def _graph_api(advertiser: str, token: str, limit: int) -> list[dict]:
    params = {
        "access_token": token,
        "ad_type": "ALL",
        "ad_reached_countries": "US",
        "search_terms": advertiser,
        "fields": _FIELDS,
        "limit": min(limit, 100),
    }
    try:
        resp = requests.get(GRAPH_API, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            return []
        raw = data.get("data", [])
        ads = [_normalize(ad, advertiser) for ad in raw]

        # Follow pagination up to limit
        while len(ads) < limit:
            next_url = data.get("paging", {}).get("next")
            if not next_url:
                break
            resp = requests.get(next_url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("data", [])
            if not batch:
                break
            ads.extend([_normalize(ad, advertiser) for ad in batch])

        return ads[:limit]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Strategy 2 — facebook.com/ads/library/api/ (unauthenticated)
# ---------------------------------------------------------------------------

def _library_api(advertiser: str, limit: int) -> list[dict]:
    params = {
        "ad_type": "ALL",
        "country": "US",
        "search_terms": advertiser,
        "fields": _FIELDS,
        "limit": min(limit, 100),
    }
    token = os.environ.get("META_ACCESS_TOKEN", "").strip()
    if token:
        params["access_token"] = token

    try:
        resp = requests.get(AD_LIBRARY_API, params=params, headers=_HEADERS, timeout=30)
        if resp.status_code != 200:
            return []
        data = resp.json()
        raw = data.get("data", [])
        return [_normalize(ad, advertiser) for ad in raw][:limit]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Strategy 3 — Web scrape + __NEXT_DATA__
# ---------------------------------------------------------------------------

def _web_scrape(advertiser: str, limit: int) -> list[dict]:
    params = {
        "active_status": "all",
        "ad_type": "all",
        "country": "US",
        "q": advertiser,
        "search_type": "keyword_unordered",
    }
    try:
        resp = requests.get(AD_LIBRARY_WEB, params=params, headers=_HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Try __NEXT_DATA__
        tag = soup.find("script", {"id": "__NEXT_DATA__"})
        if tag and tag.string:
            data = json.loads(tag.string)
            ads = _extract_from_next_data(data, advertiser, limit)
            if ads:
                return ads

        # Try embedded serialized data blobs
        for script in soup.find_all("script"):
            text = script.string or ""
            for pattern in [
                r'"ads"\s*:\s*(\[.*?\])\s*[,}]',
                r'adCards\s*=\s*(\[.*?\])',
            ]:
                m = re.search(pattern, text, re.DOTALL)
                if m:
                    try:
                        raw = json.loads(m.group(1))
                        ads = [_normalize(a, advertiser) for a in raw if isinstance(a, dict)]
                        if ads:
                            return ads[:limit]
                    except Exception:
                        continue

        return []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Strategy 4 — Playwright (handles JS bot challenge)
# ---------------------------------------------------------------------------

def _playwright_scrape(advertiser: str, limit: int) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
        return []

    url = (
        f"{AD_LIBRARY_WEB}?active_status=all&ad_type=all"
        f"&country=US&q={advertiser}&search_type=keyword_unordered"
    )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(user_agent=_HEADERS["User-Agent"])

        captured_ads: list[dict] = []

        def on_response(resp):
            if "ads/library" in resp.url and resp.status == 200:
                try:
                    ct = resp.headers.get("content-type", "")
                    if "json" in ct:
                        data = resp.json()
                        _walk_meta_json(data, advertiser, captured_ads, limit)
                except Exception:
                    pass

        page.on("response", on_response)

        try:
            # Let Playwright execute the JS challenge automatically
            page.goto(url, timeout=30_000, wait_until="networkidle")
        except PwTimeout:
            # Partial load is fine
            pass

        # If XHR gave nothing, parse __NEXT_DATA__ from final DOM
        if not captured_ads:
            html = page.content()
            soup_tag = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
            if soup_tag:
                try:
                    data = json.loads(soup_tag.group(1))
                    _extract_from_next_data_into(data, advertiser, captured_ads, limit)
                except Exception:
                    pass

        browser.close()

    return captured_ads[:limit]


def _walk_meta_json(obj, advertiser: str, out: list[dict], limit: int) -> None:
    if len(out) >= limit:
        return
    if isinstance(obj, list):
        for item in obj:
            _walk_meta_json(item, advertiser, out, limit)
    elif isinstance(obj, dict):
        if "ad_creative_bodies" in obj or "ad_snapshot_url" in obj:
            out.append(_normalize(obj, advertiser))
        else:
            for v in obj.values():
                _walk_meta_json(v, advertiser, out, limit)


def _extract_from_next_data_into(obj, advertiser: str, out: list[dict], limit: int) -> None:
    if len(out) >= limit:
        return
    if isinstance(obj, list):
        for item in obj:
            _extract_from_next_data_into(item, advertiser, out, limit)
    elif isinstance(obj, dict):
        if "ad_creative_bodies" in obj or "ad_snapshot_url" in obj:
            out.append(_normalize(obj, advertiser))
        else:
            for v in obj.values():
                _extract_from_next_data_into(v, advertiser, out, limit)


def _extract_from_next_data(obj, advertiser: str, limit: int) -> list[dict]:
    ads: list[dict] = []

    def walk(node):
        if len(ads) >= limit:
            return
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            if "ad_creative_bodies" in node or "ad_snapshot_url" in node:
                ads.append(_normalize(node, advertiser))
            else:
                for v in node.values():
                    walk(v)

    walk(obj)
    return ads[:limit]


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _normalize(ad: dict, advertiser: str) -> dict:
    bodies = ad.get("ad_creative_bodies") or []
    titles = ad.get("ad_creative_link_titles") or []
    captions = ad.get("ad_creative_link_captions") or []
    descriptions = ad.get("ad_creative_link_descriptions") or []

    imp = ad.get("impressions") or {}
    imp_range = None
    if isinstance(imp, dict):
        lo = imp.get("lower_bound", "")
        hi = imp.get("upper_bound", "")
        if lo and hi:
            imp_range = f"{lo}-{hi}"

    return {
        "platform": "meta",
        "advertiser": advertiser,
        "ad_id": str(ad.get("id", "")),
        "format": _infer_format(ad),
        "headline": titles[0] if titles else "",
        "primary_text": bodies[0] if bodies else "",
        "description": descriptions[0] if descriptions else None,
        "cta": captions[0] if captions else None,
        "image_url": None,
        "video_url": None,
        "start_date": ad.get("ad_delivery_start_time"),
        "end_date": ad.get("ad_delivery_stop_time"),
        "impressions_range": imp_range,
        "scraped_at": datetime.utcnow().isoformat(),
    }


def _infer_format(ad: dict) -> str:
    snapshot = ad.get("ad_snapshot_url", "")
    if "video" in snapshot.lower():
        return "video"
    bodies = ad.get("ad_creative_bodies") or []
    if len(bodies) > 1:
        return "carousel"
    return "single_image"
