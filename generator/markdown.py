"""Brand DNA markdown generator — single Ollama call."""

import json
import os
from datetime import date as _date

from ollama import AsyncClient

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")

MARKDOWN_PROMPT = """\
You are generating a marketing preference model — a document that gives an LLM \
everything it needs to continue {advertiser}'s advertising without breaking their voice.

Goal: someone reading this file should be able to write new {advertiser} ads that are \
indistinguishable from their real ones. Cite actual phrases from the data. \
If data is thin on a section, say so explicitly — do not invent.

Client: {advertiser}
Platforms analyzed: {platforms}
Total ads analyzed: {total_ads}
Ad type distribution: {type_distribution}
Analysis date: {date}

Voice fingerprint data:
{voice_json}

Ad type classification:
{angles_json}

Format and funnel data:
{funnel_json}

Visual metadata:
{visual_json}

Campaign structure inference:
{structure_json}

Top observed ads (by impression):
{sample_ads_json}

---

Write a markdown document with exactly these sections using ## headers:

## How to Use This Document
2-3 sentences. This is a preference model, not a report. Explain that a reader \
should start with Voice Fingerprint to understand tone, then go to the relevant \
Ad Type section for the format they are writing.

## Voice Fingerprint
The single most important section. Must be specific enough to imitate. Cover:
- Headline formula (the actual pattern, e.g. "Free [Product Name] — [Benefit]")
- Sentence length and rhythm
- 5-8 signature phrases or vocabulary patterns from the actual data
- CTA constructions they use
- What they never say (guardrails)
Use > blockquotes for verbatim examples from the data.

## Ad Type: Form / Lead Gen
Copy structure, observed examples, and the formula for writing a new one.
If fewer than 3 examples in data, note that explicitly.

## Ad Type: Engagement / Brand
Copy structure, observed examples, and the formula for writing a new one.
If no examples in data, say "Insufficient data from this scrape."

## Ad Type: Webinar / Event
Copy structure, observed examples, and the formula for writing a new one.
If no examples in data, say "Insufficient data from this scrape."

## Visual Patterns
What creatives look like — inferred from metadata and descriptions where available. \
Label inferred vs. observed.

## Positioning Map
How they frame their own value proposition. What problem they lead with. \
What they call their product vs. what competitors might call it.

## Synthetic Ad Templates
{synthetic_section}

## What We Don't Know Yet
Data gaps, low-confidence sections, what additional scraping would improve this model.

Write in third person (e.g. "{advertiser} leads with free tools"). \
Use bullet points for lists. Use > blockquotes for direct ad copy examples."""


async def generate(
    advertiser: str,
    platforms: list[str],
    total_ads: int,
    analysis: dict,
    sample_ads: list[dict],
    date: str = "",
) -> str:
    if not date:
        date = _date.today().isoformat()

    angles = analysis.get("angles", [])
    angle_summary = _summarize_angles(angles)

    type_distribution = analysis.get("type_distribution", {})
    type_dist_str = ", ".join(f"{k}: {v}" for k, v in type_distribution.items()) or "not classified"

    synthetic_ads = analysis.get("synthetic", {}).get("synthetic_ads", [])
    synthetic_section = _build_synthetic_section(synthetic_ads)

    prompt = MARKDOWN_PROMPT.format(
        advertiser=advertiser,
        platforms=", ".join(platforms),
        total_ads=total_ads,
        type_distribution=type_dist_str,
        date=date,
        voice_json=json.dumps(analysis.get("voice", {}), indent=2),
        angles_json=json.dumps(angle_summary, indent=2),
        funnel_json=json.dumps(analysis.get("funnel", {}), indent=2),
        visual_json=json.dumps(analysis.get("visual", {}), indent=2),
        structure_json=json.dumps(analysis.get("structure", {}), indent=2),
        sample_ads_json=json.dumps(_slim_ads(sample_ads), indent=2),
        synthetic_section=synthetic_section,
    )

    client = AsyncClient(host=OLLAMA_HOST)
    response = await client.chat(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an expert advertising analyst writing a preference learning model — "
                    "a document that gives an LLM everything it needs to continue a brand's "
                    "advertising without breaking their voice. Write detailed, specific, actionable "
                    "markdown. Ground every claim in the data provided. Output markdown only — "
                    "no JSON, no meta-commentary."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        options={"temperature": 0.3, "num_predict": 4096},
    )

    content = (response.message.content or "").strip()
    header = (
        f"# {advertiser} — Advertising Brand DNA\n"
        f"_Generated {date} · {total_ads} ads analyzed across {', '.join(platforms)}_\n\n"
        "---\n\n"
    )
    return header + content


def _build_synthetic_section(synthetic_ads: list[dict]) -> str:
    if not synthetic_ads:
        return "Insufficient data — re-run with more copy samples to generate synthetic ads."
    lines = []
    for i, ad in enumerate(synthetic_ads, 1):
        ad_type = ad.get("type", "unknown").replace("_", " ").title()
        lines.append(f"### Synthetic Ad {i} — {ad_type}")
        if ad.get("headline"):
            lines.append(f"**Headline:** {ad['headline']}")
        if ad.get("primary_text"):
            lines.append(f"> {ad['primary_text']}")
        if ad.get("cta"):
            lines.append(f"**CTA:** {ad['cta']}")
        if ad.get("visual_description"):
            lines.append(f"**Visual:** {ad['visual_description']}")
        if ad.get("why_on_brand"):
            lines.append(f"_Why on-brand: {ad['why_on_brand']}_")
        lines.append("")
    return "\n".join(lines).strip()


def _summarize_angles(angles: list[dict]) -> dict:
    dist: dict[str, list] = {}
    for item in angles:
        angle = item.get("angle", "unknown")
        if angle not in dist:
            dist[angle] = []
        dist[angle].append({
            "ad_id": item.get("ad_id", ""),
            "reasoning": item.get("reasoning", ""),
            "confidence": item.get("confidence", 0),
        })
    total = max(len(angles), 1)
    return {
        angle: {
            "count": len(examples),
            "percentage": round(len(examples) / total * 100, 1),
            "sample_reasoning": examples[0]["reasoning"] if examples else "",
        }
        for angle, examples in sorted(dist.items(), key=lambda x: len(x[1]), reverse=True)
    }


def _slim_ads(ads: list[dict]) -> list[dict]:
    return [
        {
            "platform": a.get("platform", ""),
            "format": a.get("format", ""),
            "headline": a.get("headline", ""),
            "primary_text": (a.get("primary_text") or "")[:300],
            "cta": a.get("cta", ""),
            "impressions_range": a.get("impressions_range", ""),
        }
        for a in ads
    ]
