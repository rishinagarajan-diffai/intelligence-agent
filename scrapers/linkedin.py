"""LinkedIn Ad Library scraper — Playwright-only (no public API).

LinkedIn blocks unauthenticated API calls, so we use headless browsing.
Company IDs are required; pass them via LINKEDIN_COMPANY_IDS env var or the
hardcoded lookup table below.
"""

import os
import re
from datetime import datetime

_KNOWN_IDS: dict[str, str] = {
    "notion": "10257271",
    "coda": "18480454",
    "confluence": "1117",
    "atlassian": "1117",
    "monday.com": "10902633",
    "monday": "10902633",
    "airtable": "3949382",
    "asana": "1419599",
    "clickup": "18416836",
}

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def resolve_company_id(advertiser: str) -> str | None:
    """Return LinkedIn company ID from env override or built-in table."""
    env_map = _parse_env_ids()
    key = advertiser.lower().strip()
    return env_map.get(key) or _KNOWN_IDS.get(key)


def _parse_env_ids() -> dict[str, str]:
    raw = os.environ.get("LINKEDIN_COMPANY_IDS", "")
    result: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if ":" in pair:
            name, cid = pair.split(":", 1)
            result[name.strip().lower()] = cid.strip()
    return result


def scrape(company_id: str, advertiser: str, limit: int = 50) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
        return []

    url = f"https://www.linkedin.com/ad-library/search?companyIds={company_id}"
    ads: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(user_agent=_UA)
        page = context.new_page()

        intercepted: list[dict] = []

        def handle_response(response):
            if "ad-library" in response.url and response.status == 200:
                try:
                    body = response.json()
                    _walk_json(body, intercepted)
                except Exception:
                    pass

        page.on("response", handle_response)

        try:
            page.goto(url, timeout=30_000, wait_until="networkidle")
            page.wait_for_selector(".ad-library-card, [data-test-id='ad-card']", timeout=15_000)
        except PwTimeout:
            pass

        if intercepted:
            ads = [_normalize_api(a, advertiser) for a in intercepted][:limit]
        else:
            ads = _parse_dom(page, advertiser, limit)

        # Scroll to load more if needed
        if len(ads) < limit:
            try:
                for _ in range(3):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1500)
                    new_cards = _parse_dom(page, advertiser, limit)
                    if len(new_cards) > len(ads):
                        ads = new_cards
                    else:
                        break
            except Exception:
                pass

        browser.close()

    return ads[:limit]


def _walk_json(obj, out: list) -> None:
    """Recursively find ad objects in JSON response."""
    if isinstance(obj, list):
        for item in obj:
            _walk_json(item, out)
    elif isinstance(obj, dict):
        if any(k in obj for k in ("headline", "introductoryText", "adCardType")):
            out.append(obj)
        else:
            for v in obj.values():
                _walk_json(v, out)


def _parse_dom(page, advertiser: str, limit: int) -> list[dict]:
    selectors = [".ad-library-card", "[data-test-id='ad-card']", "[class*='AdCard']"]
    cards = []
    for sel in selectors:
        cards = page.query_selector_all(sel)
        if cards:
            break

    ads = []
    for card in cards[:limit]:
        try:
            headline = _text(card, [
                ".ad-library-card__headline",
                "[data-test-id='headline']",
                "h3", ".headline",
            ])
            body = _text(card, [
                ".ad-library-card__introductory-text",
                "[data-test-id='introductory-text']",
                ".body-copy", "p",
            ])
            cta = _text(card, [
                ".ad-library-card__cta",
                "[data-test-id='cta']",
                "button", ".cta",
            ])
            date_text = _text(card, [
                ".ad-library-card__date-range",
                "[data-test-id='date-range']",
                ".date-range",
            ])
            fmt = _infer_format(card)

            start, end = _parse_dates(date_text)
            ads.append({
                "platform": "linkedin",
                "advertiser": advertiser,
                "ad_id": _extract_id(card),
                "format": fmt,
                "headline": headline,
                "primary_text": body,
                "description": None,
                "cta": cta or None,
                "image_url": _src(card, "img"),
                "video_url": _src(card, "video source, video"),
                "start_date": start,
                "end_date": end,
                "impressions_range": None,
                "scraped_at": datetime.utcnow().isoformat(),
            })
        except Exception:
            continue
    return ads


def _normalize_api(ad: dict, advertiser: str) -> dict:
    return {
        "platform": "linkedin",
        "advertiser": advertiser,
        "ad_id": str(ad.get("id") or ad.get("adId") or ""),
        "format": _map_format(ad.get("adCardType") or ad.get("format") or ""),
        "headline": ad.get("headline") or ad.get("title") or "",
        "primary_text": ad.get("introductoryText") or ad.get("body") or "",
        "description": None,
        "cta": ad.get("ctaLabel") or ad.get("cta") or None,
        "image_url": _deep(ad, ["imageUrl", "media", "url"]),
        "video_url": _deep(ad, ["videoUrl", "media", "streamingLocations", 0, "url"]),
        "start_date": ad.get("startDate") or ad.get("firstRunDate"),
        "end_date": ad.get("endDate"),
        "impressions_range": None,
        "scraped_at": datetime.utcnow().isoformat(),
    }


def _text(element, selectors: list[str]) -> str:
    for sel in selectors:
        try:
            el = element.query_selector(sel)
            if el:
                return (el.inner_text() or "").strip()
        except Exception:
            continue
    return ""


def _src(element, selector: str) -> str | None:
    try:
        el = element.query_selector(selector)
        if el:
            return el.get_attribute("src") or el.get_attribute("href") or None
    except Exception:
        pass
    return None


def _extract_id(card) -> str:
    for attr in ["data-ad-id", "data-id", "id"]:
        try:
            val = card.get_attribute(attr)
            if val:
                return val
        except Exception:
            pass
    return ""


def _infer_format(card) -> str:
    try:
        badge = card.query_selector("[class*='format'], [class*='badge'], [class*='type']")
        if badge:
            text = (badge.inner_text() or "").lower()
            if "video" in text:
                return "video"
            if "carousel" in text:
                return "carousel"
            if "single" in text or "image" in text:
                return "single_image"
        if card.query_selector("video"):
            return "video"
        imgs = card.query_selector_all("img")
        if len(imgs) > 1:
            return "carousel"
    except Exception:
        pass
    return "single_image"


def _map_format(raw: str) -> str:
    raw = raw.upper()
    if "VIDEO" in raw:
        return "video"
    if "CAROUSEL" in raw:
        return "carousel"
    if "TEXT" in raw:
        return "text"
    return "single_image"


def _parse_dates(text: str) -> tuple[str | None, str | None]:
    if not text:
        return None, None
    parts = re.split(r"[-–—]|\bto\b", text)
    parts = [p.strip() for p in parts if p.strip()]
    start = parts[0] if parts else None
    end = parts[1] if len(parts) > 1 else None
    return start, end


def _deep(obj: dict, path: list) -> str | None:
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and isinstance(key, int) and key < len(cur):
            cur = cur[key]
        else:
            return None
        if cur is None:
            return None
    return str(cur) if cur else None
