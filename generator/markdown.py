"""Brand DNA markdown generator — single Ollama call."""

import json
import os
from datetime import date as _date

from ollama import AsyncClient

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")

MARKDOWN_PROMPT = """\
You are generating a brand advertising intelligence profile for an LLM that will \
continue this company's advertising campaigns.

Goal: someone reading this file should be able to write new ads that are \
indistinguishable from the company's existing work. Capture not just what they say \
but how they say it. Cite actual phrases from the ads. Every claim must be grounded \
in the data below. Avoid generic advice — if data is thin on a section, say so.

Client: {advertiser}
Platforms analyzed: {platforms}
Total ads analyzed: {total_ads}
Analysis date: {date}

Voice fingerprint:
{voice_json}

Angle library:
{angles_json}

Format and funnel mapping:
{funnel_json}

Visual patterns:
{visual_json}

Campaign structure:
{structure_json}

Competitive gap map:
{gaps_json}

Top ads by impression range:
{sample_ads_json}

---

Write a markdown document with exactly these sections using ## headers:

## Overview
2-3 sentences on their advertising posture.

## Brand Voice Fingerprint
How they write — specific enough to imitate. Cover sentence length, tone, recurring \
constructions, what they never say.

## Campaign Architecture
How they structure campaigns and funnels. Which stages they invest in and why.

## Format Playbook
When they use each format and why. What formats they avoid and the implied logic.

## Messaging Angle Library
Every observed angle with examples from actual ads. Note which are primary vs. occasional.

## Visual Language Guide
What their creative looks like — color signals, text-overlay habits, video style.

## What Not To Do
Patterns they consistently avoid. These guardrails are as important as the playbook.

## Competitive Position
Where they're strong, where gaps exist, what angles competitors own that they don't.

## Recommended Next Moves
3-5 specific angles or formats to test next, each with a one-sentence rationale.

## Sample LLM Prompts
3 ready-to-use prompts for generating on-brand ads at different campaign objectives.

Write in third person (e.g. "Notion writes short declarative sentences"). \
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

    prompt = MARKDOWN_PROMPT.format(
        advertiser=advertiser,
        platforms=", ".join(platforms),
        total_ads=total_ads,
        date=date,
        voice_json=json.dumps(analysis.get("voice", {}), indent=2),
        angles_json=json.dumps(angle_summary, indent=2),
        funnel_json=json.dumps(analysis.get("funnel", {}), indent=2),
        visual_json=json.dumps(analysis.get("visual", {}), indent=2),
        structure_json=json.dumps(analysis.get("structure", {}), indent=2),
        gaps_json=json.dumps(analysis.get("gaps", {}), indent=2),
        sample_ads_json=json.dumps(_slim_ads(sample_ads), indent=2),
    )

    client = AsyncClient(host=OLLAMA_HOST)
    response = await client.chat(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an expert advertising analyst writing a comprehensive brand "
                    "intelligence report. Write detailed, specific, actionable markdown. "
                    "Ground every claim in the data provided. Output markdown only — "
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
