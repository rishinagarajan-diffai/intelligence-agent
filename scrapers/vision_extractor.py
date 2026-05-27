"""Vision extraction for display ad creatives via Gemini API.

For ads that have an image_url but no text copy (common with Google display ads
where copy is baked into the creative image), fetches each image and uses
Gemini's vision capability to read the ad text.

Runs between scraping and analysis. Processes only the top N ads by impressions
(the analysis passes cap at 25 ads anyway), with concurrent requests since
Gemini handles parallel calls without resource constraints.
"""

import asyncio
import json
import os
import time

import requests
from google import genai
from google.genai import types as genai_types

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

_MAX_ADS = 50       # top N by impressions — bumped from 25 to widen DB coverage of image-only ads
_TIMEOUT = 60       # seconds per vision call
_IMG_TIMEOUT = 8    # seconds for image fetch

_VISION_PROMPT = (
    "This is a display advertisement. Extract all text visible in the image. "
    "Return JSON only with exactly these keys:\n"
    '{"headline": "<main headline text or empty string>", '
    '"primary_text": "<body copy or supporting text or empty string>", '
    '"cta": "<single CTA button text only — one short phrase like \'Get started\' or \'Learn more\', NOT a list of sitelinks — or empty string if no button>", '
    '"visual_description": "<one sentence: imagery style, color palette, layout>"}'
)

_gemini_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client


def _parse_impressions(imp: str | None) -> int:
    if not imp:
        return 0
    try:
        return int(imp.split("-")[0].replace(",", "").strip())
    except Exception:
        return 0


def _fetch_image(url: str) -> tuple[bytes, str] | None:
    """Fetch image bytes and return (raw_bytes, media_type) or None."""
    try:
        r = requests.get(url, timeout=_IMG_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200 or not r.content:
            return None
        ct = r.headers.get("content-type", "image/png").split(";")[0].strip()
        if ct not in ("image/png", "image/jpeg", "image/webp"):
            ct = "image/png"
        return r.content, ct
    except Exception:
        return None


async def _extract_one(ad: dict, idx: int, total: int, console=None) -> dict:
    """Vision-extract a single ad. Returns the ad dict (updated in-place)."""
    ad_id = ad.get("ad_id", "?")
    image_url = ad.get("image_url", "")

    label = f"[{idx}/{total}] ad {ad_id[:12]}..."
    if console:
        console.print(f"  [dim]{label} fetching image...[/dim]", end="\r")

    img = await asyncio.get_event_loop().run_in_executor(None, _fetch_image, image_url)
    if not img:
        if console:
            console.print(f"  [dim]{label} image fetch failed — skipped[/dim]")
        return ad

    img_bytes, media_type = img
    if console:
        console.print(f"  [dim]{label} running vision...[/dim]", end="\r")

    try:
        client = _get_client()
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=[
                    genai_types.Part.from_bytes(data=img_bytes, mime_type=media_type),
                    genai_types.Part.from_text(text=_VISION_PROMPT),
                ],
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            ),
            timeout=_TIMEOUT,
        )
        raw = (response.text or "").strip()
        result = json.loads(raw)
        ad = {**ad}
        if result.get("headline"):
            ad["headline"] = result["headline"]
        if result.get("primary_text"):
            ad["primary_text"] = result["primary_text"]
        if result.get("cta"):
            ad["cta"] = result["cta"]
        ad["visual_description"] = result.get("visual_description", "")
        headline_preview = (ad.get("headline") or "")[:50]
        if console:
            console.print(f"  [green]✓[/green] {label} \"{headline_preview}\"")
    except asyncio.TimeoutError:
        if console:
            console.print(f"  [yellow]⚠[/yellow] {label} timed out after {_TIMEOUT}s — skipped")
    except (json.JSONDecodeError, Exception):
        if console:
            console.print(f"  [yellow]⚠[/yellow] {label} parse error — skipped")

    return ad


async def extract_all(ads: list[dict], console=None) -> list[dict]:
    """Vision-extract copy for ads missing headline/primary_text.

    Sorts by impressions, processes top _MAX_ADS concurrently via Gemini.
    Returns updated list with copy fields filled in where extraction succeeded.
    """
    needs_vision = [
        a for a in ads
        if a.get("image_url")
        and not (a.get("headline") or "").strip()
        and not (a.get("primary_text") or "").strip()
    ]

    if not needs_vision:
        return ads

    needs_vision.sort(key=lambda a: _parse_impressions(a.get("impressions_range")), reverse=True)
    to_process = needs_vision[:_MAX_ADS]
    skipped = len(needs_vision) - len(to_process)

    if console:
        msg = f"  [dim]Vision: processing top {len(to_process)} ads by impressions via {GEMINI_MODEL}"
        if skipped:
            msg += f" (skipping {skipped} lower-impression ads)"
        console.print(msg + "[/dim]")

    start = time.time()
    tasks = [_extract_one(ad, i + 1, len(to_process), console) for i, ad in enumerate(to_process)]
    updated_list = await asyncio.gather(*tasks)

    updated_map = {a.get("ad_id"): a for a in updated_list}
    extracted = sum(1 for a in updated_map.values() if (a.get("headline") or "").strip())
    elapsed = int(time.time() - start)
    if console:
        console.print(
            f"  [green]✓[/green] Vision done in {elapsed}s — "
            f"{extracted}/{len(to_process)} ads yielded copy"
        )

    return [updated_map.get(a.get("ad_id"), a) for a in ads]
