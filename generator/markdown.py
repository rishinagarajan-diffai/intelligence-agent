"""Brand DNA markdown generator — Gemini writes the document from structured analysis data."""

import json
import os
from datetime import date as _date

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


_SYSTEM = (
    "You are an expert advertising strategist and copywriter. "
    "Write clear, specific, actionable brand documents. "
    "Use ONLY data provided — never invent statistics, quotes, or patterns not in the input. "
    "Output clean GitHub-flavored markdown. No preamble, no meta-commentary, no prose about what you are doing."
)

_PROMPT = """\
Write a Brand DNA preference model for {advertiser}. This is not a report — it is a working \
document a copywriter picks up to immediately write new ads that sound indistinguishable from \
real {advertiser} ads.

You have structured analysis data derived from {total_ads} real {advertiser} ads scraped from \
{platforms}. Use ONLY the data below.

---

### Ad type distribution
{type_distribution}

### Voice fingerprint
{voice}

### Messaging angles
{angles}

### Funnel & format data
{funnel}

### Visual patterns
{visual}

### Campaign structure
{structure}

### Sample ad copy ({n_ads} real ads)
{sample_ads}

### Competitive landscape (RAG-fetched, not model inference)
{market_context}

### Synthetic ad examples
{synthetic}

---

Write the document in this exact structure:

# {advertiser} — Advertising Brand DNA
_{date} · {total_ads} ads analyzed across {platforms} · Ad type distribution: {type_dist_str}_

---

## How to Use This Document
2-3 sentences. This is a preference model. Tell the reader what to read first and why.

## Voice Fingerprint
- Headline formulas — ranked list (1. most common → 3. least common). For each: backtick-formatted pattern, estimated frequency, one verbatim example in quotes. If a formula's frequency is primarily driven by a specific sub-brand or product (e.g. Slack ads under Salesforce), note this in parentheses after the frequency.
- Rhythm and tone: avg sentence length, tone adjectives, vocabulary level
- Opening hook pattern
- Signature phrases — bulleted list of phrases that feel distinctly on-brand
- CTA constructions — exact CTAs in backticks
- What they never say — guardrails, bulleted
- Verbatim headlines from the data — blockquote each one. Rules: (1) Include ONLY actual ad headlines — not body copy, customer testimonials, social proof quotes, or descriptive phrases. (2) Each blockquote must be unique — do not repeat the same headline twice. (3) Include nothing before or after each blockquote line — no sitelinks, CTA text, tracking suffixes, or italic lines. (4) Skip any ad where the headline is just the advertiser brand name alone with no other text. (5) Brand-affiliation filter: skip any headline that does not mention the brand name, a known product name, or a domain — headlines about unrelated businesses, escape rooms, tourism, or other clearly off-brand topics must be excluded even if they appear in the sample data.

## Ad Type: Form / Lead Gen
Count the observed form_lead_gen examples in the sample data before writing.
- 5+ examples: write headline formula, then a sub-section labeled "Example headlines:" (not "Observed examples:") listing 2-3 verbatim headline + CTA pairs, then step-by-step writing formula.
- 2–4 examples: write formula and examples but prepend: **[LOW CONFIDENCE — {{N}} examples only. Validate with human before campaign use.]**
- 0–1 examples: write only: **[INSUFFICIENT DATA — do not use for campaign generation. Fewer than 2 observed examples. Use general Voice Fingerprint as a guide and flag output for human review.]**
If CTA was not captured for an observed ad, write `[CTA not captured]`.
{mofu_cta_fallback}

## Ad Type: Engagement / Brand
Same structure and confidence thresholds as Form / Lead Gen above.

## Ad Type: Webinar / Event
Same structure and confidence thresholds. If zero examples observed, write only:
**[NO DATA — ad type not observed in this dataset. Do not generate campaigns of this type without a separate brief.]**

## Visual Patterns
Dominant style, image content patterns (bulleted), text overlay usage, color palette signals. \
Mark anything inferred vs directly observed from visual_description fields. \
If sparse, say so in one sentence.

## Positioning Map
Funnel approach, campaign types observed (bulleted), testing behavior, \
top messaging angles by frequency, funnel stage distribution. \
If both Google and LinkedIn are in the data, write a platform strategy subsection for each platform — \
but render it as a clearly blocked warning so agents can distinguish inferred from observed:

> ## ⚠️ Platform Strategy — INFERRED, NOT OBSERVED
> The following is inferred from go-to-market priors. No platform-segmented creative data was \
> captured. Do not treat as validated. Confirm with paid media team before generating \
> platform-specific campaigns.
>
> **Google:** [inferred note]
> **LinkedIn:** [inferred note]

## Competitive Landscape
_Source: web-fetched content only — not model inference. Fields marked "insufficient data" mean the \
information was not present in fetched sources._

market_summary as a single paragraph. Then:

**Competitors:**
For each competitor in the data: bullet with **[Name]** in bold, then `known_for`. If ad_angle is not empty, add "Advertising angle: [ad_angle]." If positioning_vs_category is not empty, add it. Skip fields that say "insufficient data".

**Saturated strategies — crowded, differentiate or avoid:**
Bulleted list from saturated_strategies. If empty, write "No patterns observed in fetched data."

**Whitespace — underused in this market:**
Bulleted list from whitespace. After each bullet, add: _(not cross-referenced against {advertiser}'s own recent product releases — verify before treating as open opportunity)_
If empty, write "No clear whitespace identified in fetched data."

**Strategic implications for {advertiser}:**
Bulleted list from strategic_implications. If empty, omit this subsection.

If market_context is empty or has no useful data, write one sentence: "_Competitive landscape data unavailable for this run._"

## Synthetic Ad Templates
ALLOWED CTAs (use ONLY these exact strings, no others): {cta_list}
For each synthetic ad in the data: ### Template N — Type, then headline on its own line, then body copy in a blockquote (every line prefixed with "> "), then CTA in backticks, visual description in italics, voice pattern note in italics. Example format:
### Template 1 — Form / Lead Gen
**[Headline text]**
> Body copy line one.
> Body copy line two.
`CTA text`
_Visual: describe visual elements only from the Visual Patterns section — do not invent graphics, characters, or imagery not mentioned there._
_Voice: pattern note here_
For visual descriptions: describe ONLY visual elements consistent with what was observed in the Visual Patterns section above. Do not invent specific graphics, mascots, characters, or imagery not mentioned in the data.
CTA rules by type: For `form_lead_gen` and `webinar_event` templates, always use a conversion or registration CTA from the ALLOWED list — never `No CTA`. For `engagement_brand` templates, use `No CTA` if the observed sample ads for that type show blank CTAs; otherwise pick from the ALLOWED list. Do not use any CTA string not present in the ALLOWED list above, even if similar text appears elsewhere in the data or prompt. If the ALLOWED list contains multiple variants of the same CTA (e.g. multiple demo CTAs), pick exactly one — do not combine them with slashes or commas.
Body copy rules: The `> ` blockquote contains ONLY the ad body copy — do not include signature phrases, slogans, or CTA text inside the blockquote body.
Guardrail validation: after writing each template, silently check the headline and body copy against the "What they never say" list in the Voice Fingerprint. If any phrase violates a guardrail (e.g. vague jargon the brand explicitly avoids, passive voice where brand is active, hyperbolic claims the brand prohibits), rewrite that element before outputting. Do not output a template that violates its own brand guardrails.
If no synthetic ads, say so in one sentence.

## What We Don't Know Yet
Specific gaps based on THIS data: which platforms are missing, which ad types have thin coverage, \
what the classifier returned as unknown, what competitors would unlock. \
No generic disclaimers — only gaps visible in the data above.
"""


async def generate(
    advertiser: str,
    platforms: list[str],
    total_ads: int,
    analysis: dict,
    sample_ads: list[dict],
    date: str = "",
    prev_analysis: dict | None = None,
) -> str:
    if not date:
        date = _date.today().isoformat()

    voice = analysis.get("voice", {})
    angles = analysis.get("angles", [])
    funnel = analysis.get("funnel", {})
    visual = analysis.get("visual", {})
    structure = analysis.get("structure", {})
    synthetic = analysis.get("synthetic", {})
    market_context = analysis.get("market_context", {})
    type_distribution = analysis.get("type_distribution", {})

    # SYSTEMIC-01: inject MOFU fallback when MOFU ads exist but no stage-specific
    # CTA data is captured (voice pass returns CTAs in aggregate, never by funnel stage).
    mofu_count = (funnel.get("funnel_distribution") or {}).get("MOFU", 0) if isinstance(funnel, dict) else 0
    if mofu_count > 0:
        mofu_cta_fallback = (
            f"MOFU coverage: {mofu_count} ads observed but no stage-specific CTA data captured. "
            "Append this block verbatim after the writing formula:\n"
            "> **MOFU CTA default (unvalidated):** Use `Learn more` or `Download` as neutral "
            "fallbacks for mid-funnel content. Do not use direct conversion CTAs (demo, trial, "
            "pricing) at this stage without platform data confirming they convert. Flag any MOFU "
            "campaign for human CTA review before launch."
        )
    else:
        mofu_cta_fallback = ""

    # Filter null/empty headline formula patterns so "Empty String" doesn't reach the generator.
    # Covers both top-level headline_formulas list and per-type by_type sub-objects.
    _null_patterns = {"empty string", "", "null", "none", "n/a"}
    if isinstance(voice, dict):
        voice = dict(voice)
        if isinstance(voice.get("headline_formulas"), list):
            voice["headline_formulas"] = [
                f for f in voice["headline_formulas"]
                if f.get("formula", "").lower().strip() not in _null_patterns
            ]
        if isinstance(voice.get("by_type"), dict):
            cleaned_by_type = {}
            for ad_type, type_data in voice["by_type"].items():
                if isinstance(type_data, dict):
                    pattern = type_data.get("headline_pattern", "").lower().strip()
                    if pattern in _null_patterns:
                        type_data = {**type_data, "headline_pattern": "[no headline observed for this ad type]"}
                cleaned_by_type[ad_type] = type_data
            voice["by_type"] = cleaned_by_type

    # SYSTEMIC-06: drop synthetic ads for types not observed in this run's data
    observed_types = set(type_distribution.keys()) if isinstance(type_distribution, dict) else set()
    if observed_types and isinstance(synthetic, dict):
        ads_list = synthetic.get("synthetic_ads") or []
        filtered_ads = [a for a in ads_list if a.get("type", "") in observed_types]
        if len(filtered_ads) < len(ads_list):
            synthetic = {**synthetic, "synthetic_ads": filtered_ads}

    type_dist_str = (
        ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in type_distribution.items())
        or "not classified"
    )

    raw_ctas = (
        voice.get("cta_constructions")
        or voice.get("cta_patterns")
        or voice.get("ctas")
        or []
    )
    if isinstance(raw_ctas, list):
        cta_items = [f"`{c}`" for c in raw_ctas if c and str(c).strip()]
    else:
        cta_items = [str(raw_ctas)] if raw_ctas else []

    # BRAND-04: when the current CTA set collapsed to ≤2 values, carry forward
    # historical CTAs from the previous run so the agent has a usable range.
    if len(cta_items) <= 2 and prev_analysis:
        prev_voice = prev_analysis.get("voice", {})
        prev_raw = (
            prev_voice.get("cta_constructions")
            or prev_voice.get("cta_patterns")
            or prev_voice.get("ctas")
            or []
        )
        prev_ctas = [str(c).strip() for c in (prev_raw if isinstance(prev_raw, list) else []) if c]
        curr_strings = {c.strip("`") for c in cta_items}
        for c in prev_ctas:
            if c not in curr_strings:
                cta_items.append(f"`{c}` _(previously observed)_")

    cta_items.append("`No CTA`")  # always valid for ad types with no observed CTA
    cta_list = ", ".join(cta_items)

    slimmed = [
        {
            "platform": a.get("platform", ""),
            "format": a.get("format", ""),
            "headline": a.get("headline", ""),
            "primary_text": (a.get("primary_text") or "")[:300],
            "cta": a.get("cta") or "",
            "ad_type": a.get("ad_type", "unknown"),
            "visual_description": (a.get("visual_description") or "")[:150],
        }
        for a in sample_ads
    ][:30]

    prompt = _PROMPT.format(
        advertiser=advertiser,
        total_ads=total_ads,
        platforms=", ".join(platforms),
        date=date,
        type_dist_str=type_dist_str,
        cta_list=cta_list,
        mofu_cta_fallback=mofu_cta_fallback,
        type_distribution=json.dumps(type_distribution, indent=2),
        voice=json.dumps(voice, indent=2),
        angles=json.dumps(angles[:30], indent=2),
        funnel=json.dumps(funnel, indent=2),
        visual=json.dumps(visual, indent=2),
        structure=json.dumps(structure, indent=2),
        market_context=json.dumps(market_context, indent=2),
        sample_ads=json.dumps(slimmed, indent=2),
        synthetic=json.dumps(synthetic, indent=2),
        n_ads=len(slimmed),
    )

    client = _get_client()
    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            system_instruction=_SYSTEM,
            temperature=0.3,
        ),
    )

    return (response.text or "").strip()
