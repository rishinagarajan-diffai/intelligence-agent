"""Ad ownership filter — two-signal approach.

Signal 1: Landing page domain check (fast, free).
Signal 2: Gemini classification fallback for ads without a landing URL.

Only client ads pass through this filter — competitor ads are never filtered.
"""

import asyncio
import json
import os
from urllib.parse import urlparse

from google import genai
from google.genai import types as genai_types

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# ---------------------------------------------------------------------------
# Known domain map — single source of truth
# ---------------------------------------------------------------------------

_KNOWN_DOMAINS: dict[str, list[str]] = {
    "hubspot": ["hubspot.com"],
    "salesforce": ["salesforce.com", "slack.com", "tableau.com", "mulesoft.com", "heroku.com"],
    "descope": ["descope.com"],
    "thoughtspot": ["thoughtspot.com"],
    "notion": ["notion.so"],
    "zendesk": ["zendesk.com"],
    "monday.com": ["monday.com"],
    "intercom": ["intercom.com"],
}

_FILTER_TIMEOUT = 30  # seconds per Gemini classification call

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


# ---------------------------------------------------------------------------
# Signal 1 — domain check
# ---------------------------------------------------------------------------

def _domain_matches(url: str | None, advertiser: str) -> bool | None:
    """
    Returns:
      True  — URL domain belongs to the advertiser → keep
      False — URL provided but domain doesn't match → drop
      None  — no URL available → needs Gemini fallback
    """
    if not url or not url.strip():
        return None

    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        # Strip www. prefix
        if host.startswith("www."):
            host = host[4:]
        # Root domain: take last two parts (handles subdomains)
        parts = host.split(".")
        root = ".".join(parts[-2:]) if len(parts) >= 2 else host
    except Exception:
        return None

    owned_domains = _KNOWN_DOMAINS.get(advertiser.lower(), [])
    if not owned_domains:
        # Advertiser not in known map — can't make a domain determination
        return None

    return root in owned_domains


# ---------------------------------------------------------------------------
# Signal 2 — Gemini fallback
# ---------------------------------------------------------------------------

_OWNERSHIP_PROMPT = """\
Is this ad promoting {advertiser}'s own product or service?

Sponsor company: {company}
Headline: {headline}
Body copy: {body}

Return JSON only: {{"is_owned": true, "reason": "<10 words>"}} or {{"is_owned": false, "reason": "<10 words>"}}"""


async def _classify_one(ad: dict, advertiser: str) -> bool:
    """Ask Gemini whether this ad is owned by the target advertiser. Defaults to True on error."""
    company = ad.get("advertiser", "") or ad.get("company_name", "")
    headline = (ad.get("headline") or "")[:100]
    body = (ad.get("primary_text") or "")[:150]

    prompt = _OWNERSHIP_PROMPT.format(
        advertiser=advertiser,
        company=company,
        headline=headline,
        body=body,
    )

    client = _get_client()
    try:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0,
                ),
            ),
            timeout=_FILTER_TIMEOUT,
        )
        raw = (response.text or "").strip()
        parsed = json.loads(raw)
        return bool(parsed.get("is_owned", True))
    except Exception:
        # Safe default: keep the ad so we never lose owned ads on error
        return True


# ---------------------------------------------------------------------------
# Main filter function
# ---------------------------------------------------------------------------

async def filter_owned_ads(ads: list[dict], advertiser: str, console=None) -> list[dict]:
    """
    Filter ads to only those that belong to the target advertiser.

    Runs domain check first (free); falls back to Gemini for ads without a URL.
    Never raises — on any exception returns the original unfiltered list.
    """
    try:
        kept_domain: list[dict] = []
        dropped_domain: int = 0
        needs_gemini: list[dict] = []

        for ad in ads:
            result = _domain_matches(ad.get("landing_url"), advertiser)
            if result is True:
                kept_domain.append(ad)
            elif result is False:
                dropped_domain += 1
            else:
                # result is None — no URL, needs Gemini
                needs_gemini.append(ad)

        # Run Gemini batch concurrently for ads without a landing URL
        kept_gemini: list[dict] = []
        dropped_gemini: int = 0

        if needs_gemini:
            ownership_results = await asyncio.gather(
                *[_classify_one(ad, advertiser) for ad in needs_gemini]
            )
            for ad, is_owned in zip(needs_gemini, ownership_results):
                if is_owned:
                    kept_gemini.append(ad)
                else:
                    dropped_gemini += 1

        if console:
            console.print(
                f"  [dim]Ad ownership filter: "
                f"kept {len(kept_domain)} via domain, "
                f"dropped {dropped_domain} via domain, "
                f"kept {len(kept_gemini)} via Gemini, "
                f"dropped {dropped_gemini} via Gemini[/dim]"
            )

        return kept_domain + kept_gemini

    except Exception as exc:
        if console:
            console.print(f"  [yellow]Warning: ad_filter raised {exc!r} — using unfiltered ads[/yellow]")
        return ads
