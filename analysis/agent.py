"""Six-pass analysis agent backed by Gemini API.

Passes 1-4 run concurrently (no inter-dependencies).
Pass 5 requires the aggregate of 1-4.
Pass 6 requires competitor analyses from passes run in parallel with 1-4.

Set GEMINI_API_KEY and GEMINI_MODEL env vars to control which model is used.
"""

import asyncio
import json
import os
import re
from typing import Any

import requests

from google import genai
from google.genai import types as genai_types

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

CLASSIFIER_PROMPT = """\
Classify each ad below by its primary goal. Use only these four labels:
  form_lead_gen   — lead capture: "Download", "Get the guide", "Start free", form fills
  engagement_brand — brand/awareness: storytelling, authority, no immediate conversion ask
  webinar_event   — time-bound event: "Register", "Join us", "Watch now", date references
  unknown         — cannot be determined from available copy

Return JSON only: {{"classifications": [{{"ad_id": "<str>", "type": "<str>"}}]}}

Ads:
{ads_json}"""

VOICE_PROMPT = """\
You are learning {advertiser}'s brand voice well enough to write new ads \
indistinguishable from their real ones.

Study these {total_ads} ads and return a JSON object that answers: \
"What do I need to know to write a new {advertiser} ad?"

{{
  "headline_formulas": [
    {{"formula": "<pattern, e.g. Free [Product] — [Benefit]>", "frequency": "<e.g. ~40% of headlines>", "example": "<verbatim headline from the data>", "sub_brand_note": "optional — omit if not applicable"}},
    {{"formula": "<second most common pattern>", "frequency": "<e.g. ~30% of headlines>", "example": "<verbatim>"}},
    {{"formula": "<third pattern if present, omit if fewer than 3 distinct patterns>", "frequency": "<e.g. ~20%>", "example": "<verbatim>"}}
  ],
  "_headline_formulas_instruction": "For each formula above, if the frequency count is primarily driven by ads for a specific sub-brand or product name (e.g. 'Slack' ads within a Salesforce dataset), add a 'sub_brand_note' field with a short string like 'frequency driven primarily by Slack ads'. Omit the field entirely when there is no sub-brand concentration.",
  "avg_sentence_length": <int>,
  "tone_descriptors": [<3-5 adjectives>],
  "vocabulary_level": "<technical|plain|mixed>",
  "signature_phrases": [<words/phrases that feel distinctly on-brand, 5-10 items>],
  "cta_patterns": [<exact CTA constructions from the ads>],
  "what_they_never_say": [<marketing clichés, jargon, or styles they avoid>],
  "opening_hook_pattern": "<how they open — benefit claim, feature name, free offer, question>",
  "by_type": {{
    "form_lead_gen": {{
      "headline_pattern": "<formula for this type>",
      "example_headline": "<verbatim from the data, or empty string if no examples>"
    }},
    "engagement_brand": {{
      "headline_pattern": "<formula for this type>",
      "example_headline": "<verbatim from the data, or empty string if no examples>"
    }},
    "webinar_event": {{
      "headline_pattern": "<formula for this type>",
      "example_headline": "<verbatim from the data, or empty string if no examples>"
    }}
  }}
}}

Ad copy data:
{all_copy_json}"""

ANGLE_TAXONOMY = [
    "pain_point", "outcome_led", "social_proof", "founder_story",
    "competitive_displacement", "feature_highlight", "how_it_works",
    "customer_quote", "free_trial", "webinar_event", "product_demo",
    "thought_leadership", "urgency_scarcity",
]

ANGLE_PROMPT = """\
Classify each ad below by primary messaging angle. Assign exactly one angle per ad — the single most dominant angle. Do not assign multiple angles to the same ad. \
Only use angles from this list: {taxonomy}

Return a JSON object with key "classifications" containing an array. Each element:
{{"ad_id": "<str>", "angle": "<str>", "confidence": <0.0-1.0>, "reasoning": "<one sentence>"}}

Example: {{"classifications": [{{"ad_id": "1", "angle": "pain_point", "confidence": 0.9, "reasoning": "..."}}]}}

Ads to classify:
{ads_json}"""

FUNNEL_PROMPT = """\
For each ad below, infer its funnel stage.
Stages: TOFU (awareness/education), MOFU (consideration/nurture), BOFU (conversion/signup)

Return only a JSON object:
{{
  "format_distribution": {{"single_image": <int>, "video": <int>, "carousel": <int>, "lead_form": <int>, "text": <int>}},
  "funnel_distribution": {{"TOFU": <int>, "MOFU": <int>, "BOFU": <int>}},
  "format_funnel_matrix": {{
    "single_image": {{"TOFU": <int>, "MOFU": <int>, "BOFU": <int>}},
    "video": {{"TOFU": <int>, "MOFU": <int>, "BOFU": <int>}},
    "carousel": {{"TOFU": <int>, "MOFU": <int>, "BOFU": <int>}},
    "text": {{"TOFU": <int>, "MOFU": <int>, "BOFU": <int>}}
  }},
  "primary_ctas_by_stage": {{"TOFU": [<str>], "MOFU": [<str>], "BOFU": [<str>]}}
}}

Ads:
{ads_json}"""

VISUAL_PROMPT = """\
You are being shown actual images from {advertiser}'s ads. Describe ONLY what you \
can visually observe in these images — colors, layouts, imagery style, logo treatments, \
text overlay usage, background patterns. Do NOT infer from ad copy text. If you see a \
workflow diagram, say so. If you see a specific color, name it. If an image is purely \
text on white, say that.

Also included is structured ad metadata (image descriptions and copy) for additional context, \
but your visual assessment must be grounded in what is actually visible in the images above.

Return only a JSON object:
{{
  "dominant_visual_style": "<str>",
  "image_content_patterns": [<str>],
  "text_overlay_usage": "<heavy|moderate|minimal|none>",
  "color_palette_signals": [<str>],
  "video_style": "<str or null>",
  "visual_consistency_score": <float 0.0-1.0>
}}

Ad data:
{ads_with_visual_context}"""

STRUCTURE_PROMPT = """\
Based on the ad distribution data below, infer how {advertiser} structures their campaigns.

Return only a JSON object:
{{
  "funnel_approach": "<str>",
  "campaign_types_observed": [<str>],
  "typical_campaign_structure": "<str>",
  "testing_behavior": "<str>",
  "platform_strategy": {{
    "meta": "<str>",
    "google": "<str>",
    "linkedin": "<str>"
  }},
  "budget_signals": "<str>"
}}

Analysis data:
{aggregated_analysis}"""

GAP_PROMPT = """\
Compare the advertising strategy of {advertiser} against their competitors.

Return only a JSON object:
{{
  "angles_client_owns": [<str>],
  "angles_competitors_own": [<str>],
  "white_space_angles": [<str>],
  "format_gaps": [<str>],
  "strategic_observations": [<3-5 str>],
  "recommended_next_angles": [<top 3 str>]
}}

{advertiser} angle distribution:
{client_angles}

Competitor angle distributions:
{competitor_angles}

{advertiser} format distribution:
{client_formats}

Competitor format distributions:
{competitor_formats}"""

SYNTHETIC_PROMPT = """\
You have analyzed {advertiser}'s advertising and know their brand voice. \
Now write {n} complete ads that are indistinguishable from real {advertiser} ads.

What you know about their voice:
{voice_summary}

Real ad examples from the data:
{sample_copy}

Scenario for the new ads: {scenario}

Return JSON only:
{{
  "synthetic_ads": [
    {{
      "type": "form_lead_gen or engagement_brand or webinar_event",
      "headline": "<headline in their exact style>",
      "primary_text": "<1-3 sentences of body copy in their voice>",
      "cta": "<button text they would use>",
      "visual_description": "<one sentence: what the image would look like>",
      "voice_pattern_used": "<quote the specific headline_formula or signature phrase this follows>"
    }}
  ]
}}

Rules:
- Generate {n} ads total
- Include at least one form_lead_gen and one engagement_brand
- CTA must match ad type: engagement_brand → awareness CTAs only (Download, Learn more, Watch video, Read more); form_lead_gen → conversion CTAs only (Start free trial, Request demo, Sign up free, Get started); webinar_event → registration CTAs (Register now, Save your seat, Join us)
- Only include webinar_event if the voice data shows they run that type
- Every word must match their observed vocabulary and style
- Do not invent claims that aren't supported by their patterns"""

MARKET_CONTEXT_PROMPT = """\
Search the web for current information about {advertiser}'s competitive landscape.

Step 1 — Search for: "{advertiser} top competitors alternatives" and "{advertiser} vs comparison"
Step 2 — Identify the 3-6 main companies that compete with {advertiser} based on your search results.{explicit_note}
Step 3 — For each competitor you found: describe what they are known for, their dominant \
advertising angle, and how they position vs the category.
Step 4 — Synthesize: which advertising strategies are saturated in this market (multiple \
players do this), and where is the whitespace (nobody visibly owns this angle).

Base your response on what you find in search results. Where search results are thin, \
say so explicitly rather than filling from training knowledge.

Return a JSON object (no markdown fences):
{{
  "market_summary": "<2-3 sentences about the competitive landscape based on search results>",
  "competitors": [
    {{
      "name": "<competitor name>",
      "known_for": "<what they are primarily known for, from search results>",
      "ad_angle": "<their dominant advertising message or angle>",
      "positioning_vs_category": "<how they position against the category>"
    }}
  ],
  "saturated_strategies": [
    "<angle or message that multiple players use — differentiate or avoid>"
  ],
  "whitespace": [
    "<angle or approach not visibly claimed by any competitor>"
  ],
  "strategic_implications": [
    "<specific recommendation for {advertiser} based on the competitive gaps above>"
  ]
}}"""

SYSTEM_ANALYST = (
    "You are an expert advertising analyst. "
    "Always respond with valid JSON only. "
    "No prose, no markdown fences, no explanation before or after the JSON."
)


# ---------------------------------------------------------------------------
# Core call helper
# ---------------------------------------------------------------------------

_CALL_TIMEOUT = 120  # seconds per analysis pass (Gemini is fast; was 300 for Ollama)


async def _call(prompt: str, pass_name: str, console=None) -> dict | list:
    """Single Gemini API call returning parsed JSON."""
    client = _get_client()

    try:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    system_instruction=SYSTEM_ANALYST,
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            ),
            timeout=_CALL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        if console:
            console.print(
                f"  [yellow]Warning: {pass_name} timed out after {_CALL_TIMEOUT}s — using empty result. "
                f"Check Gemini quota at console.cloud.google.com[/yellow]"
            )
        return {}

    raw = (response.text or "").strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", raw)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        if console:
            console.print(f"  [yellow]Warning: {pass_name} returned unparseable JSON — using empty result[/yellow]")
        return {}


# ---------------------------------------------------------------------------
# Individual passes
# ---------------------------------------------------------------------------

_JS_PATTERNS = re.compile(
    r'\b(function|return|typeof|instanceof|prototype|undefined|null|var |let |const )\b'
    r'|^(Cannot find|No nullish|Symbol is not|Assertion failed|Object\.|Array\.|String\.)',
    re.IGNORECASE,
)


def _is_real_copy(text: str) -> bool:
    if not text or len(text) < 8 or " " not in text:
        return False
    if _JS_PATTERNS.search(text):
        return False
    return True


async def _pass_classify(advertiser: str, ads: list[dict], console=None) -> dict[str, str]:
    """Classify each ad by type. Returns {ad_id: type} mapping."""
    slim = [
        {
            "ad_id": a.get("ad_id", ""),
            "headline": a.get("headline", ""),
            "primary_text": (a.get("primary_text") or "")[:200],
            "cta": a.get("cta", ""),
        }
        for a in ads
        if _is_real_copy(a.get("headline", "")) or _is_real_copy(a.get("primary_text", ""))
    ][:40]
    if not slim:
        return {}
    prompt = CLASSIFIER_PROMPT.format(ads_json=json.dumps(slim, indent=2))
    result = await _call(prompt, "classifier", console)
    classifications = []
    if isinstance(result, list):
        classifications = result
    elif isinstance(result, dict):
        for key in ("classifications", "ads", "results"):
            if isinstance(result.get(key), list):
                classifications = result[key]
                break
    return {c.get("ad_id", ""): c.get("type", "unknown") for c in classifications if c.get("ad_id")}


async def _pass_voice(advertiser: str, ads: list[dict], console=None) -> dict:
    all_copy = [
        {
            "ad_id": a.get("ad_id", ""),
            "headline": a.get("headline", ""),
            "primary_text": a.get("primary_text", ""),
            "cta": a.get("cta", ""),
            "visual_description": a.get("visual_description", ""),
        }
        for a in ads
        if _is_real_copy(a.get("headline", "")) or _is_real_copy(a.get("primary_text", ""))
    ][:40]
    prompt = VOICE_PROMPT.format(
        advertiser=advertiser,
        total_ads=len(ads),
        all_copy_json=json.dumps(all_copy, indent=2),
    )
    if console:
        console.print(f"  [dim]Pass 1/6 — Voice fingerprint ({len(all_copy)} copy samples)[/dim]")
    result = await _call(prompt, "voice_fingerprint", console)
    return result if isinstance(result, dict) else {}


async def _pass_synthetic(
    advertiser: str, voice: dict, ads: list[dict], console=None, scenario: str | None = None
) -> dict:
    """Generate synthetic example ads in the brand's voice."""
    sample_copy = [
        {"headline": a.get("headline", ""), "primary_text": (a.get("primary_text") or "")[:200], "cta": a.get("cta", "")}
        for a in ads
        if _is_real_copy(a.get("headline", ""))
    ][:10]
    voice_summary = {
        "headline_formula": voice.get("headline_formula", ""),
        "signature_phrases": voice.get("signature_phrases", []),
        "cta_patterns": voice.get("cta_patterns", []),
        "what_they_never_say": voice.get("what_they_never_say", []),
        "tone_descriptors": voice.get("tone_descriptors", []),
        "by_type": voice.get("by_type", {}),
    }
    if not (scenario or "").strip():
        # Derive scenario from their observed voice — what they actually sell and to whom
        opening_hook = voice.get("opening_hook_pattern", "")
        tone = ", ".join(voice.get("tone_descriptors", [])[:2]) or "professional"
        scenario = (
            f"promoting {advertiser}'s core product using their {tone} voice "
            f"and {opening_hook or 'benefit-led'} opening style"
        )
    # Escape curly braces so the scenario string doesn't break SYNTHETIC_PROMPT.format()
    safe_scenario = scenario.replace("{", "{{").replace("}", "}}")
    prompt = SYNTHETIC_PROMPT.format(
        advertiser=advertiser,
        n=3,
        voice_summary=json.dumps(voice_summary, indent=2),
        sample_copy=json.dumps(sample_copy, indent=2),
        scenario=safe_scenario,
    )
    if console:
        console.print(f"  [dim]Pass 7/7 — Synthetic ad generator[/dim]")
    result = await _call(prompt, "synthetic_ads", console)
    if isinstance(result, dict) and isinstance(result.get("synthetic_ads"), list):
        return result
    return {"synthetic_ads": []}


async def _pass_angles(advertiser: str, ads: list[dict], console=None) -> list[dict]:
    slim = [
        {
            "ad_id": a.get("ad_id", f"ad_{i}"),
            "headline": a.get("headline", ""),
            "primary_text": a.get("primary_text", ""),
            "cta": a.get("cta", ""),
            "format": a.get("format", ""),
        }
        for i, a in enumerate(ads)
        if _is_real_copy(a.get("headline", "")) or _is_real_copy(a.get("primary_text", ""))
    ][:40]
    prompt = ANGLE_PROMPT.format(
        taxonomy=", ".join(ANGLE_TAXONOMY),
        ads_json=json.dumps(slim, indent=2),
    )
    if console:
        console.print(f"  [dim]Pass 2/6 — Angle classification ({len(slim)} ads)[/dim]")
    result = await _call(prompt, "angle_classification", console)
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        # gemma4 wraps arrays in an object key
        for key in ("classifications", "ads", "results", "angles"):
            if isinstance(result.get(key), list):
                return result[key]
    return []


async def _pass_funnel(advertiser: str, ads: list[dict], console=None) -> dict:
    slim = [
        {
            "ad_id": a.get("ad_id", f"ad_{i}"),
            "headline": a.get("headline", ""),
            "primary_text": a.get("primary_text", ""),
            "cta": a.get("cta", ""),
            "format": a.get("format", ""),
            "platform": a.get("platform", ""),
        }
        for i, a in enumerate(ads)
        if _is_real_copy(a.get("headline", "")) or _is_real_copy(a.get("primary_text", ""))
    ][:40]
    prompt = FUNNEL_PROMPT.format(ads_json=json.dumps(slim, indent=2))
    if console:
        console.print(f"  [dim]Pass 3/6 — Format & funnel mapping[/dim]")
    result = await _call(prompt, "funnel_mapping", console)
    return result if isinstance(result, dict) else {}


_IMG_FETCH_TIMEOUT = 8  # seconds for image fetch in visual pass


def _fetch_image_for_visual(url: str) -> tuple[bytes, str] | None:
    """Fetch image bytes and return (raw_bytes, media_type) or None."""
    try:
        r = requests.get(url, timeout=_IMG_FETCH_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200 or not r.content:
            return None
        ct = r.headers.get("content-type", "image/png").split(";")[0].strip()
        if ct not in ("image/png", "image/jpeg", "image/webp"):
            ct = "image/png"
        return r.content, ct
    except Exception:
        return None


async def _pass_visual(advertiser: str, ads: list[dict], console=None) -> dict:
    visual_data = [
        {
            "ad_id": a.get("ad_id", ""),
            "format": a.get("format", ""),
            "headline": a.get("headline", ""),
            "visual_description": a.get("visual_description", ""),
            "image_url": a.get("image_url"),
            "video_url": a.get("video_url"),
            "platform": a.get("platform", ""),
        }
        for a in ads
        if _is_real_copy(a.get("headline", "")) or a.get("image_url") or a.get("video_url")
    ][:40]

    prompt_text = VISUAL_PROMPT.format(
        advertiser=advertiser,
        ads_with_visual_context=json.dumps(visual_data, indent=2),
    )

    if console:
        console.print(f"  [dim]Pass 4/6 — Visual pattern analysis[/dim]")

    # Collect up to 10 ads that have an image_url to fetch real image bytes.
    ads_with_images = [d for d in visual_data if d.get("image_url")][:10]

    image_parts: list[genai_types.Part] = []
    if ads_with_images:
        loop = asyncio.get_event_loop()
        fetch_results = await asyncio.gather(
            *[
                loop.run_in_executor(None, _fetch_image_for_visual, d["image_url"])
                for d in ads_with_images
            ]
        )
        for fetch_result in fetch_results:
            if fetch_result is not None:
                img_bytes, media_type = fetch_result
                image_parts.append(
                    genai_types.Part.from_bytes(data=img_bytes, mime_type=media_type)
                )

    if image_parts:
        # Multimodal call: real image bytes + text prompt
        contents = image_parts + [genai_types.Part.from_text(text=prompt_text)]
        client = _get_client()
        try:
            response = await asyncio.wait_for(
                client.aio.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=contents,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=SYSTEM_ANALYST,
                        response_mime_type="application/json",
                        temperature=0.1,
                    ),
                ),
                timeout=_CALL_TIMEOUT,
            )
        except asyncio.TimeoutError:
            if console:
                console.print(
                    f"  [yellow]Warning: visual_patterns timed out after {_CALL_TIMEOUT}s — using empty result. "
                    f"Check Gemini quota at console.cloud.google.com[/yellow]"
                )
            return {}
        raw = (response.text or "").strip()
        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            import re as _re
            m = _re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", raw)
            if m:
                try:
                    result = json.loads(m.group(1))
                except json.JSONDecodeError:
                    result = {}
            else:
                result = {}
        return result if isinstance(result, dict) else {}
    else:
        # Fall back to text-only path when no images could be fetched
        result = await _call(prompt_text, "visual_patterns", console)
        return result if isinstance(result, dict) else {}


async def _pass_structure(advertiser: str, aggregated: dict, console=None) -> dict:
    prompt = STRUCTURE_PROMPT.format(
        advertiser=advertiser,
        aggregated_analysis=json.dumps(aggregated, indent=2),
    )
    if console:
        console.print(f"  [dim]Pass 5/6 — Campaign structure inference[/dim]")
    result = await _call(prompt, "campaign_structure", console)
    return result if isinstance(result, dict) else {}


async def _pass_gaps(
    advertiser: str,
    client_angles: list[dict],
    client_funnel: dict,
    competitor_analyses: dict[str, dict],
    console=None,
) -> dict:
    def angle_dist(angle_list: list[dict]) -> dict[str, int]:
        dist: dict[str, int] = {}
        for item in angle_list:
            angle = item.get("angle", "unknown")
            dist[angle] = dist.get(angle, 0) + 1
        return dist

    competitor_angles = {
        name: angle_dist(data.get("angles", []))
        for name, data in competitor_analyses.items()
        if data.get("angles")
    }
    competitor_formats = {
        name: data.get("funnel", {}).get("format_distribution", {})
        for name, data in competitor_analyses.items()
        if data.get("funnel")
    }

    prompt = GAP_PROMPT.format(
        advertiser=advertiser,
        client_angles=json.dumps(angle_dist(client_angles), indent=2),
        competitor_angles=json.dumps(competitor_angles, indent=2),
        client_formats=json.dumps(client_funnel.get("format_distribution", {}), indent=2),
        competitor_formats=json.dumps(competitor_formats, indent=2),
    )
    if console:
        console.print(f"  [dim]Pass 6/6 — Competitive gap map ({len(competitor_analyses)} competitors)[/dim]")
    result = await _call(prompt, "competitive_gaps", console)
    return result if isinstance(result, dict) else {}


async def _pass_market_context(
    advertiser: str,
    competitors: list[str],
    console=None,
) -> dict:
    """Google Search-grounded Gemini call for competitive landscape.

    Uses Gemini's built-in google_search tool so the response is grounded
    in live search results, not training data. Grounding is incompatible
    with response_mime_type=application/json, so JSON is parsed from text.
    """
    explicit = f" + {len(competitors)} explicit" if competitors else ""
    if console:
        console.print(f"  [dim]Pass 6b — Market context (Google Search grounding, auto-discover{explicit})[/dim]")

    explicit_note = ""
    if competitors:
        names = ", ".join(competitors)
        explicit_note = f" Also include these competitors in your analysis: {names}."

    prompt = MARKET_CONTEXT_PROMPT.format(
        advertiser=advertiser,
        explicit_note=explicit_note,
    )

    client = _get_client()
    try:
        response = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    tools=[genai_types.Tool(google_search=genai_types.GoogleSearch())],
                    temperature=0.1,
                ),
            ),
            timeout=_CALL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        if console:
            console.print("  [yellow]Warning: market_context timed out — skipping[/yellow]")
        return {}
    except Exception as exc:
        if console:
            console.print(f"  [yellow]Warning: market_context failed ({exc}) — skipping[/yellow]")
        return {}

    raw = (response.text or "").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"(\{[\s\S]*\})", raw)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        return {}


async def _analyze_competitor(name: str, ads: list[dict]) -> dict[str, Any]:
    if not ads:
        return {"angles": [], "funnel": {}}
    angles, funnel = await asyncio.gather(
        _pass_angles(name, ads),
        _pass_funnel(name, ads),
    )
    return {"angles": angles, "funnel": funnel}


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def run_all_passes(
    advertiser: str,
    client_ads: list[dict],
    competitor_ads: dict[str, list[dict]],
    console=None,
    scenario: str | None = None,
) -> dict[str, Any]:
    if not client_ads:
        if console:
            console.print(f"  [yellow]No ads for {advertiser} — skipping analysis[/yellow]")
        return {}

    # Pass 0 — classify ad types before anything else
    if console:
        console.print(f"  [dim]Pass 0/7 — Ad type classifier[/dim]")
    type_map = await _pass_classify(advertiser, client_ads, console)

    # Attach type labels to ads in memory
    for ad in client_ads:
        ad["ad_type"] = type_map.get(ad.get("ad_id", ""), "unknown")

    type_counts = {}
    for t in type_map.values():
        type_counts[t] = type_counts.get(t, 0) + 1
    if console:
        console.print(f"  [dim]  → {type_counts}[/dim]")

    # Competitor passes run concurrently with passes 1-4
    competitor_tasks = {
        name: asyncio.create_task(_analyze_competitor(name, ads))
        for name, ads in competitor_ads.items()
        if ads
    }

    # Passes 1-4 + market context are independent — run concurrently.
    # Market context does web fetching (I/O in executor) then a Gemini call.
    voice, angles, funnel, visual, market_context = await asyncio.gather(
        _pass_voice(advertiser, client_ads, console),
        _pass_angles(advertiser, client_ads, console),
        _pass_funnel(advertiser, client_ads, console),
        _pass_visual(advertiser, client_ads, console),
        _pass_market_context(advertiser, list(competitor_ads.keys()), console),
    )

    # SYSTEMIC-04: if Pass 6b returned empty, fall back to most recent non-empty
    # cached result from DB so the generator always has competitive context.
    if not market_context:
        from storage.db import get_stale_market_context
        try:
            cached = get_stale_market_context(advertiser)
            if cached:
                stale_date = cached.get("market_summary", "")[:10]
                market_context = cached
                if console:
                    console.print(f"  [dim]Pass 6b empty — using cached market context[/dim]")
        except Exception:
            pass

    # Pass 5 needs 1-4
    aggregated = {
        "voice": voice,
        "angles": angles,
        "funnel": funnel,
        "visual": visual,
        "ad_count": len(client_ads),
        "platforms": list({a["platform"] for a in client_ads}),
        "type_distribution": type_counts,
    }
    structure = await _pass_structure(advertiser, aggregated, console)

    competitor_analyses: dict[str, dict] = {}
    for name, task in competitor_tasks.items():
        competitor_analyses[name] = await task

    gaps = await _pass_gaps(advertiser, angles, funnel, competitor_analyses, console)

    # Pass 7 — synthetic ad generator (needs voice from pass 1)
    synthetic = await _pass_synthetic(advertiser, voice, client_ads, console, scenario=scenario)

    return {
        "voice": voice,
        "angles": angles,
        "funnel": funnel,
        "visual": visual,
        "structure": structure,
        "gaps": gaps,
        "synthetic": synthetic,
        "market_context": market_context,
        "type_distribution": type_counts,
        "type_map": type_map,  # {ad_id: type} — used by main.py to persist ad_type to DB
    }
