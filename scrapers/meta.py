"""Meta Ad Library scraper.

Strategies (in order):
  1. Graph API with META_ACCESS_TOKEN — requires Ad Library API approval
  2. Playwright — scrapes facebook.com/ads/library directly
"""

import json
import os
import re
import urllib.parse
from datetime import datetime

import requests

GRAPH_API = "https://graph.facebook.com/v21.0/ads_archive"

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

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def scrape(advertiser: str, limit: int = 100) -> list[dict]:
    token = os.environ.get("META_ACCESS_TOKEN", "").strip()
    if token:
        ads = _graph_api(advertiser, token, limit)
        if ads:
            return ads
    return _playwright_scrape(advertiser, limit)


# ---------------------------------------------------------------------------
# Strategy 1 — Graph API (requires Ad Library API permission approval)
# ---------------------------------------------------------------------------

def _graph_api(advertiser: str, token: str, limit: int) -> list[dict]:
    params = {
        "access_token": token,
        "ad_type": "ALL",
        "ad_reached_countries": "['GB']",
        "search_terms": advertiser,
        "fields": _FIELDS,
        "limit": min(limit, 100),
    }
    try:
        resp = requests.get(GRAPH_API, params=params, timeout=30)
        data = resp.json()
        if "error" in data:
            return []
        raw = data.get("data", [])
        ads = [_normalize_api(ad, advertiser) for ad in raw]
        while len(ads) < limit:
            next_url = data.get("paging", {}).get("next")
            if not next_url:
                break
            resp = requests.get(next_url, timeout=30)
            data = resp.json()
            batch = data.get("data", [])
            if not batch:
                break
            ads.extend([_normalize_api(ad, advertiser) for ad in batch])
        return ads[:limit]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Strategy 2 — Playwright
# ---------------------------------------------------------------------------

def _playwright_scrape(advertiser: str, limit: int) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
        return []

    search_url = (
        "https://www.facebook.com/ads/library/"
        f"?active_status=all&ad_type=all&country=GB"
        f"&q={urllib.parse.quote(advertiser)}&search_type=keyword_unordered"
    )
    captured: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=_UA,
            viewport={"width": 1280, "height": 900},
            locale="en-GB",
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.new_page()

        def handle_response(resp):
            if len(captured) >= limit:
                return
            if "facebook.com" not in resp.url:
                return
            if "graphql" not in resp.url and "ads_archive" not in resp.url:
                return
            try:
                _walk_json(resp.json(), advertiser, captured, limit)
            except Exception:
                # Streaming / newline-delimited JSON
                try:
                    for line in resp.text().splitlines():
                        if len(captured) >= limit:
                            break
                        try:
                            _walk_json(json.loads(line), advertiser, captured, limit)
                        except Exception:
                            pass
                except Exception:
                    pass

        page.on("response", handle_response)

        try:
            page.goto(search_url, timeout=40_000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            _dismiss_cookies(page)
            page.wait_for_timeout(3000)
        except PwTimeout:
            pass

        # Scroll to load more ads
        prev = -1
        for _ in range(max(3, limit // 8)):
            if len(captured) >= limit or len(captured) == prev:
                break
            prev = len(captured)
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(2500)

        # DOM fallback when XHR interception yielded nothing
        if not captured:
            captured = _parse_dom(page, advertiser, limit)

        browser.close()

    return captured[:limit]


def _dismiss_cookies(page) -> None:
    for sel in [
        'button[title="Allow all cookies"]',
        'button:has-text("Allow all cookies")',
        'button:has-text("Accept all")',
        '[data-testid="cookie-policy-manage-dialog-accept-button"]',
    ]:
        try:
            btn = page.query_selector(sel)
            if btn:
                btn.click()
                page.wait_for_timeout(800)
                return
        except Exception:
            continue


def _walk_json(obj, advertiser: str, out: list[dict], limit: int) -> None:
    """Recursively search a JSON object for ad nodes."""
    if len(out) >= limit:
        return
    if isinstance(obj, list):
        for item in obj:
            _walk_json(item, advertiser, out, limit)
    elif isinstance(obj, dict):
        # Graph API response format
        if "ad_creative_bodies" in obj or "ad_snapshot_url" in obj:
            out.append(_normalize_api(obj, advertiser))
        # Web UI GraphQL response format
        elif "ad_creative_body" in obj and ("page" in obj or "id" in obj):
            page_name = (obj.get("page") or {}).get("name", "")
            if page_name and advertiser.lower() not in page_name.lower():
                return  # keyword search noise — ad from a different page
            out.append(_normalize_web(obj, advertiser))
        else:
            for v in obj.values():
                _walk_json(v, advertiser, out, limit)


def _parse_dom(page, advertiser: str, limit: int) -> list[dict]:
    """Extract ad cards from rendered DOM using 'Library ID:' as anchor."""
    ads = []
    try:
        cards = page.evaluate("""() => {
            const results = [];
            const seenCards = new WeakSet();
            for (const el of document.querySelectorAll('*')) {
                if (el.children.length > 0) continue;
                if (!(el.textContent || '').includes('Library ID:')) continue;
                // Walk up to find a card-sized container
                let card = el.parentElement;
                for (let i = 0; i < 15; i++) {
                    if (!card) break;
                    const rect = card.getBoundingClientRect();
                    if (rect.height > 200 && rect.width > 300) break;
                    card = card.parentElement;
                }
                if (!card || seenCards.has(card)) continue;
                seenCards.add(card);
                const imgs = Array.from(card.querySelectorAll('img'))
                    .map(img => img.src || img.getAttribute('data-src') || '')
                    .filter(s => s && s.startsWith('http') && !s.includes('emoji'));
                results.push({ text: card.innerText || '', images: imgs });
                if (results.length >= 60) break;
            }
            return results;
        }""")

        for item in (cards or []):
            if len(ads) >= limit:
                break
            ad = _parse_card_text(item.get("text", ""), item.get("images", []), advertiser)
            if ad:
                ads.append(ad)
    except Exception:
        pass
    return ads


def _parse_card_text(text: str, images: list, advertiser: str) -> dict | None:
    if not text or len(text) < 15:
        return None
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # The page name is always the line immediately before "Sponsored" in the card DOM.
    # Drop cards where it doesn't match the target advertiser — those are keyword-search
    # noise from other pages whose copy happens to contain the advertiser name.
    page_name = ""
    for i, line in enumerate(lines):
        if line.lower().startswith("sponsored"):
            if i > 0:
                page_name = lines[i - 1]
            break
    if page_name and advertiser.lower() not in page_name.lower():
        return None

    ad_id = ""
    for line in lines:
        m = re.search(r"Library ID[:\s]+(\d+)", line)
        if m:
            ad_id = m.group(1)
            break
    if not ad_id:
        return None

    start_date = None
    for line in lines:
        m = re.search(r"Started running on (.+)", line)
        if m:
            start_date = m.group(1).strip()
            break

    _skip = {
        "library id", "started running", "active", "inactive",
        "about this ad", "why am i seeing this", "see ad details", "sponsored",
        "this ad has multiple versions", "ads use this creative", "see summary details",
    }
    _date_range = re.compile(r"^\d{1,2}\s+\w+\s+\d{4}\s*[-–]\s*\d{1,2}\s+\w+\s+\d{4}$")
    body = [
        l for l in lines
        if len(l) > 15
        and not any(s in l.lower() for s in _skip)
        and l.lower() != advertiser.lower()
        and not _date_range.match(l)
    ]
    primary_text = "\n".join(body[:6]) if body else ""
    image_url = next(
        (img for img in images if "fbcdn" in img or "cdninstagram" in img),
        images[0] if images else None,
    )

    return {
        "platform": "meta",
        "advertiser": advertiser,
        "ad_id": f"META{ad_id}",
        "format": "single_image",
        "headline": "",
        "primary_text": primary_text,
        "description": None,
        "cta": None,
        "image_url": image_url,
        "video_url": None,
        "start_date": start_date,
        "end_date": None,
        "impressions_range": None,
        "scraped_at": datetime.utcnow().isoformat(),
        "landing_url": None,
    }


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _normalize_api(ad: dict, advertiser: str) -> dict:
    bodies = ad.get("ad_creative_bodies") or []
    titles = ad.get("ad_creative_link_titles") or []
    captions = ad.get("ad_creative_link_captions") or []
    descriptions = ad.get("ad_creative_link_descriptions") or []
    imp = ad.get("impressions") or {}
    imp_range = None
    if isinstance(imp, dict):
        lo, hi = imp.get("lower_bound", ""), imp.get("upper_bound", "")
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
        "landing_url": None,
    }


def _normalize_web(ad: dict, advertiser: str) -> dict:
    return {
        "platform": "meta",
        "advertiser": advertiser,
        "ad_id": str(ad.get("id") or ad.get("ad_archive_id", "")),
        "format": "single_image",
        "headline": ad.get("ad_creative_link_title", ""),
        "primary_text": ad.get("ad_creative_body", ""),
        "description": ad.get("ad_creative_link_description", ""),
        "cta": ad.get("ad_creative_link_caption", ""),
        "image_url": None,
        "video_url": None,
        "start_date": ad.get("start_date") or ad.get("ad_delivery_start_time"),
        "end_date": ad.get("end_date"),
        "impressions_range": None,
        "scraped_at": datetime.utcnow().isoformat(),
        "landing_url": None,
    }


def _infer_format(ad: dict) -> str:
    if "video" in (ad.get("ad_snapshot_url") or "").lower():
        return "video"
    if len(ad.get("ad_creative_bodies") or []) > 1:
        return "carousel"
    return "single_image"
