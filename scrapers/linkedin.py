"""LinkedIn Ad Library scraper — form-based Playwright scraper.

LinkedIn's Ad Library at linkedin.com/ad-library is publicly accessible
without authentication. Results are server-rendered after form submission;
the companyIds URL param alone returns empty results — the accountOwner
text param triggers the actual search.
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
    "hubspot": "1467",
    "salesforce": "1049",
    "zendesk": "1116",
    "intercom": "697989",
    "drift": "3911126",
}

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_BASE_URL = "https://www.linkedin.com/ad-library"


def resolve_company_id(advertiser: str) -> str | None:
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
        raise RuntimeError(
            "Playwright is not installed. Run: python -m playwright install chromium"
        )

    ads: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=_UA,
            viewport={"width": 1280, "height": 800},
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.new_page()

        try:
            page.goto(f"{_BASE_URL}/search", timeout=30_000, wait_until="networkidle")
            page.wait_for_timeout(1500)

            # Fill the company/advertiser name field and submit
            inp = page.query_selector('[name="accountOwner"]')
            if not inp:
                browser.close()
                return []

            inp.focus()
            page.keyboard.type(advertiser, delay=100)
            page.keyboard.press("Enter")
            page.wait_for_timeout(5000)

            # Collect ads from current page and scroll for more
            seen_ids: set[str] = set()
            scroll_attempts = 0
            max_scrolls = max(1, limit // 24)

            while len(ads) < limit and scroll_attempts <= max_scrolls:
                cards = page.query_selector_all(".ad-preview")

                # Scroll each card into view to trigger lazy image loading
                for card in cards:
                    card.scroll_into_view_if_needed()
                page.wait_for_timeout(1000)

                for card in cards:
                    ad = _parse_card(card, advertiser)
                    if ad and ad["ad_id"] not in seen_ids:
                        seen_ids.add(ad["ad_id"])
                        ads.append(ad)

                if len(ads) >= limit:
                    break

                # Scroll to load more
                prev_count = len(seen_ids)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(2500)
                scroll_attempts += 1

                # Stop if no new ads loaded
                if len(seen_ids) == prev_count and scroll_attempts > 1:
                    break

        except PwTimeout:
            pass
        except Exception:
            pass
        finally:
            browser.close()

    return ads[:limit]


def _parse_card(card, advertiser: str) -> dict | None:
    try:
        container = card.query_selector(".base-ad-preview-card")
        if not container:
            return None

        # Ad ID from detail link
        detail_link = container.query_selector("a[href*='/ad-library/detail/']")
        ad_id = ""
        if detail_link:
            href = detail_link.get_attribute("href") or ""
            m = re.search(r"/ad-library/detail/(\d+)", href)
            if m:
                ad_id = m.group(1)
        if not ad_id:
            return None

        # Format from aria-label: "HubSpot, Single Image Ad, View details"
        aria = container.get_attribute("aria-label") or ""
        fmt = _parse_format(aria)

        # Filter: only keep ads from the target advertiser's company page
        company_name = aria.split(",")[0].strip()
        if company_name.lower() != advertiser.lower():
            return None

        # Body copy from commentary
        commentary = container.query_selector(".commentary__content")
        primary_text = (commentary.inner_text() or "").strip() if commentary else ""
        # Strip leading LinkedIn attribution lines ("Sponsored Advertiser www.domain.com/path")
        if primary_text:
            lines = primary_text.splitlines()
            while lines and (lines[0].startswith("Sponsored ") or "www." in lines[0]):
                lines.pop(0)
            primary_text = "\n".join(lines).strip()

        # Headline — LinkedIn doesn't expose it directly in the card;
        # for thought-leadership ads it's the commenter's description.
        # We use the creative's alt text or leave blank for vision extraction.
        headline = _extract_headline(container)

        # Image / video URL — search from the full card, not just the inner container
        img = card.query_selector(".ad-preview__dynamic-dimensions-image")
        image_url = None
        img_alt = ""
        if img:
            image_url = img.get_attribute("src") or img.get_attribute("data-src") or None
            img_alt = (img.get_attribute("alt") or "").strip()
            if not image_url:
                image_url = None  # genuinely not loaded

        # Use image alt text as headline supplement when no other headline
        if img_alt and not headline:
            headline = img_alt

        # Video indicator
        video_url = None
        if "video" in fmt.lower():
            video_url = image_url
            image_url = None

        # Landing page URL from CTA button
        landing_url = None
        cta_link = container.query_selector("a.ad-preview__cta-link, a[data-tracking-control-name*='cta'], a.base-button")
        if not cta_link:
            # Fallback: any external anchor that isn't the detail link
            all_links = container.query_selector_all("a[href^='http']")
            for link in all_links:
                href = link.get_attribute("href") or ""
                if "linkedin.com" not in href and "ad-library" not in href:
                    landing_url = href
                    break
        else:
            landing_url = cta_link.get_attribute("href") or None

        return {
            "platform": "linkedin",
            "advertiser": advertiser,
            "ad_id": f"LI{ad_id}",
            "format": fmt,
            "headline": headline,
            "primary_text": primary_text,
            "description": None,
            "cta": None,
            "image_url": image_url,
            "video_url": video_url,
            "start_date": None,
            "end_date": None,
            "impressions_range": None,
            "scraped_at": datetime.utcnow().isoformat(),
            "landing_url": landing_url,
        }
    except Exception:
        return None


def _extract_headline(container) -> str:
    # For thought-leadership ads the "headline" is the person's name + title
    name_el = container.query_selector(
        "[aria-label*='View member page'], "
        ".block.text-md.text-color-text.font-bold"
    )
    if name_el:
        name = (name_el.inner_text() or "").strip()
        title_el = container.query_selector("p.text-xs.text-color-text-secondary.line-clamp-2")
        title = (title_el.inner_text() or "").strip() if title_el else ""
        if name and title:
            return f"{name} — {title}"
        return name
    return ""


def _parse_format(aria_label: str) -> str:
    label = aria_label.lower()
    if "video" in label:
        return "video"
    if "carousel" in label:
        return "carousel"
    if "text" in label:
        return "text"
    if "document" in label:
        return "document"
    return "single_image"
