"""Google Ads Transparency Center scraper.

Uses Playwright to navigate the ATC, intercept the internal SearchCreatives
RPC responses, and normalize to the unified ad schema.

Protobuf-JSON field map (field numbers are ATC's internal proto schema):
  Creative top-level:
    "1" = advertiser_id (AR...)
    "2" = creative_id (CR...)
    "3" = creative_content (nested)
    "4" = format: 1=display_image, 2=video, 3=responsive_display
    "6" = start_date {1: unix_seconds, 2: nanos}
    "7" = end_date  {1: unix_seconds, 2: nanos}
    "12" = advertiser_name
    "13" = impression_rank (higher = more impressions)

  Creative content ("3"):
    "3"."2" = raw HTML (img tag for format 1)
    "1"."4" = preview JS URL (format 3 responsive display)
"""

import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Advertiser names that appear in ATC search results for common advertisers.
# We prefer verified US advertisers when multiple matches exist.
_PREFERRED_TERMS = ["Inc", "LLC", "Ltd", "Labs", "Corp"]


def scrape(advertiser: str, limit: int = 100) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
        raise RuntimeError(
            "Playwright is not installed. Run: python -m playwright install chromium"
        )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(user_agent=_UA, viewport={"width": 1280, "height": 900})

        creatives: list[dict] = []
        target_advertiser_id: str | None = None

        def on_response(resp):
            if resp.status == 200 and "SearchCreatives" in resp.url:
                try:
                    data = json.loads(resp.body().decode("utf-8", errors="ignore"))
                    creatives.extend(data.get("1", []))
                except Exception:
                    pass

        page.on("response", on_response)

        try:
            page.goto(
                "https://adstransparency.google.com/",
                timeout=30_000,
                wait_until="domcontentloaded",
            )
            page.wait_for_timeout(4000)

            # Type into the search box
            search_box = page.query_selector("input")
            if not search_box:
                browser.close()
                return []

            search_box.fill(advertiser)
            page.wait_for_timeout(1000)
            page.keyboard.press("Enter")
            page.wait_for_timeout(4000)

            # Click the best matching advertiser result
            _click_best_match(page, advertiser)
            page.wait_for_timeout(6000)

            # Capture the canonical advertiser_id from the ATC URL
            parsed = urlparse(page.url)
            qs = parse_qs(parsed.query)
            target_advertiser_id = (qs.get("advertiserId") or qs.get("advertiser_id") or [None])[0]

            # Scroll to load more creatives
            seen = set()
            for _ in range(6):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(2000)
                if len(creatives) >= limit:
                    break
                new_ids = {c.get("2") for c in creatives}
                if new_ids == seen:
                    break
                seen = new_ids

        except PwTimeout:
            pass
        except Exception:
            pass
        finally:
            browser.close()

    if target_advertiser_id:
        creatives = [c for c in creatives if c.get("1") == target_advertiser_id]
    elif creatives:
        # No advertiser_id captured (ATC navigation didn't land on an advertiser page).
        # Fall back to proto field "12" (advertiser_name) to filter out unrelated ads.
        name_filtered = [c for c in creatives if advertiser.lower() in str(c.get("12", "")).lower()]
        if name_filtered:
            creatives = name_filtered

    return [_normalize(c, advertiser) for c in creatives[:limit]]


def _click_best_match(page, advertiser: str) -> None:
    """Click the advertiser result that best matches the search term."""
    # Try to find a result containing the advertiser name + common suffixes
    for suffix in ["Labs, Inc", "Inc", "LLC", "Ltd", ""]:
        query = f"{advertiser} {suffix}".strip() if suffix else advertiser
        try:
            el = page.query_selector(f"text={query}")
            if el:
                el.click()
                return
        except Exception:
            continue

    # Fall back to clicking the first non-header result
    try:
        # The results list shows items after "Advertisers" header
        items = page.query_selector_all(".advertisers-list li, [role='listitem']")
        if items:
            items[0].click()
            return
    except Exception:
        pass

    # Last resort: find anything that looks like an advertiser card and click first
    try:
        page.keyboard.press("Enter")
    except Exception:
        pass


def _normalize(creative: dict, advertiser: str) -> dict:
    fmt_raw = creative.get("4", 1)
    if fmt_raw == 2:
        fmt = "video"
    else:
        fmt = "single_image"

    content = creative.get("3", {})
    image_url = None
    video_url = None
    headline = ""
    primary_text = ""

    # Format 1 — display image: HTML img tag in content["3"]["2"]
    html_content = content.get("3", {}).get("2", "")
    if html_content:
        m = re.search(r'src\s*=\s*["\']([^"\']+)["\']', html_content)
        if m:
            image_url = m.group(1)

    # Format 2 — video
    if fmt_raw == 2:
        video_url = content.get("2", {}).get("1", "") or None

    # Format 3 — responsive display: preview URL lives in content["1"]["4"]
    # Extract any readable text from the embedded asset parameter
    preview_url = content.get("1", {}).get("4", "")
    if preview_url and not headline:
        headline, primary_text = _extract_responsive_copy(preview_url)

    # Final URL from proto field "5" (best-effort — not always present)
    final_url = creative.get("5") or None
    if isinstance(final_url, dict):
        final_url = final_url.get("1") or None

    start_ts = _parse_ts(creative.get("6", {}))
    end_ts = _parse_ts(creative.get("7", {}))

    imp_rank = creative.get("13")
    imp_range = f"{imp_rank * 100}-{imp_rank * 150}" if imp_rank else None

    return {
        "platform": "google",
        "advertiser": advertiser,
        "advertiser_id": creative.get("1", ""),
        "ad_id": creative.get("2", ""),
        "format": fmt,
        "headline": headline,
        "primary_text": primary_text,
        "description": None,
        "cta": None,
        "image_url": image_url,
        "video_url": video_url,
        "start_date": start_ts,
        "end_date": end_ts,
        "impressions_range": imp_range,
        "scraped_at": datetime.utcnow().isoformat(),
        "landing_url": final_url,
    }


def _parse_ts(ts_dict: dict) -> str | None:
    secs = ts_dict.get("1")
    if not secs:
        return None
    try:
        return datetime.fromtimestamp(int(secs), tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return None


_JS_NOISE = {
    "Cannot find global object", "No nullish arg", "Object.assign",
    "Array.prototype", "String.prototype", "Symbol.dispose",
    "Expected number", "Expected object", "Expected array",
    "native code", "CustomError",
}

def _extract_responsive_copy(preview_url: str) -> tuple[str, str]:
    """Best-effort extraction of headline/description from a responsive display JS URL."""
    try:
        import requests
        resp = requests.get(preview_url, timeout=10)
        text = resp.text

        # Look for strings that look like human-readable ad copy:
        #   - start with capital, contain spaces, plausible length
        #   - not JS keywords, not error messages, not URLs
        candidates = re.findall(r'"([A-Z][a-zA-Z0-9 \'\-,!?.]{8,100})"', text)
        clean = [
            s for s in candidates
            if " " in s
            and not s.startswith("http")
            and not s.startswith("Sponsored")
            and "www." not in s
            and not any(s.startswith(noise) for noise in _JS_NOISE)
            and not re.search(r'\b(function|return|typeof|instanceof|prototype|undefined|null)\b', s)
        ]
        headline = clean[0] if clean else ""
        body = clean[1] if len(clean) > 1 else ""
        return headline, body
    except Exception:
        return "", ""
