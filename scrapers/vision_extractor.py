"""Vision extraction for display ad creatives.

For ads that have an image_url but no text copy (common with Google display ads
where copy is baked into the creative image), fetches each image and uses
gemma4's vision capability via Ollama to read the ad text.

Runs between scraping and analysis. Processes only the top N ads by impressions
(the analysis passes cap at 30-40 ads anyway), sequentially with a per-call
timeout so one slow image never blocks the pipeline.
"""

import asyncio
import base64
import json
import os
import time

import requests
from ollama import AsyncClient

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
# Dedicated small vision model — moondream is 1.6GB and fast at reading image text.
# Falls back to the main model if OLLAMA_VISION_MODEL is not set.
VISION_MODEL = os.environ.get("OLLAMA_VISION_MODEL", "moondream")

_MAX_ADS = 25       # only process top N by impressions — all analysis passes use ≤ 40
_TIMEOUT = 60       # seconds per vision call (moondream is fast; raised from 45 for safety)
_IMG_TIMEOUT = 8    # seconds for image fetch

_VISION_PROMPT = (
    "This is a display advertisement. Extract all text visible in the image. "
    "Return JSON only with exactly these keys:\n"
    '{"headline": "<main headline text or empty string>", '
    '"primary_text": "<body copy or supporting text or empty string>", '
    '"cta": "<button text like Get started or Learn more or empty string>", '
    '"visual_description": "<one sentence: imagery style, color palette, layout>"}'
)


def _parse_impressions(imp: str | None) -> int:
    if not imp:
        return 0
    try:
        return int(imp.split("-")[0].replace(",", "").strip())
    except Exception:
        return 0


def _fetch_image(url: str) -> tuple[str, str] | None:
    """Fetch image bytes and return (base64_data, media_type) or None."""
    try:
        r = requests.get(url, timeout=_IMG_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200 or not r.content:
            return None
        ct = r.headers.get("content-type", "image/png").split(";")[0].strip()
        if ct not in ("image/png", "image/jpeg", "image/webp"):
            ct = "image/png"
        return base64.standard_b64encode(r.content).decode(), ct
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

    img_b64, _ = img
    if console:
        console.print(f"  [dim]{label} running vision...[/dim]", end="\r")

    try:
        client = AsyncClient(host=OLLAMA_HOST)
        response = await asyncio.wait_for(
            client.chat(
                model=VISION_MODEL,
                messages=[{
                    "role": "user",
                    "content": _VISION_PROMPT,
                    "images": [img_b64],
                }],
                format="json",
                options={"temperature": 0.1, "num_ctx": 4096},
            ),
            timeout=_TIMEOUT,
        )
        raw = (response.message.content or "").strip()
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
    except (json.JSONDecodeError, Exception) as e:
        if console:
            console.print(f"  [yellow]⚠[/yellow] {label} parse error — skipped")

    return ad


async def extract_all(ads: list[dict], console=None) -> list[dict]:
    """Vision-extract copy for ads missing headline/primary_text.

    Sorts by impressions, processes top _MAX_ADS sequentially with timeouts.
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

    # Sort by impressions descending — most-seen ads first
    needs_vision.sort(key=lambda a: _parse_impressions(a.get("impressions_range")), reverse=True)
    to_process = needs_vision[:_MAX_ADS]
    skipped = len(needs_vision) - len(to_process)

    if console:
        msg = f"  [dim]Vision: processing top {len(to_process)} ads by impressions via {VISION_MODEL}"
        if skipped:
            msg += f" (skipping {skipped} lower-impression ads)"
        console.print(msg + f" — ~{len(to_process) * 20 // 60}–{len(to_process) * 45 // 60} min[/dim]")

    start = time.time()
    updated_map: dict = {}
    for i, ad in enumerate(to_process, 1):
        updated = await _extract_one(ad, i, len(to_process), console)
        updated_map[ad.get("ad_id")] = updated

    extracted = sum(1 for a in updated_map.values() if (a.get("headline") or "").strip())
    elapsed = int(time.time() - start)
    if console:
        console.print(
            f"  [green]✓[/green] Vision done in {elapsed}s — "
            f"{extracted}/{len(to_process)} ads yielded copy"
        )

    return [updated_map.get(a.get("ad_id"), a) for a in ads]
