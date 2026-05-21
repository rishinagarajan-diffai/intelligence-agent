"""Brand DNA markdown generator — builds document programmatically from structured analysis data."""

import os
from collections import Counter
from datetime import date as _date

# Kept for environment consistency; no Ollama call is made in this module.
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2")


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

    voice = analysis.get("voice", {})
    synthetic_ads = analysis.get("synthetic", {}).get("synthetic_ads", [])
    type_distribution = analysis.get("type_distribution", {})
    angles = analysis.get("angles", [])
    funnel = analysis.get("funnel", {})
    visual = analysis.get("visual", {})
    structure = analysis.get("structure", {})

    type_dist_str = (
        ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in type_distribution.items())
        or "not classified"
    )

    slimmed = _slim_ads(sample_ads)

    parts = [
        (
            f"# {advertiser} — Advertising Brand DNA\n"
            f"_Generated {date} · {total_ads} ads analyzed across {', '.join(platforms)} · "
            f"Ad type distribution: {type_dist_str}_\n\n"
            "---"
        ),
        (
            "## How to Use This Document\n\n"
            f"This is a preference model, not a report. "
            f"Start with **Voice Fingerprint** to internalize {advertiser}'s tone and vocabulary before writing anything. "
            f"Then go to the relevant **Ad Type** section for the format you're producing. "
            f"The **Synthetic Ad Templates** section shows example ads generated from this fingerprint — "
            f"use them as a gut-check that your copy sounds like {advertiser}."
        ),
        _voice_section(advertiser, voice, slimmed),
        _ad_type_section("Form / Lead Gen", "form_lead_gen", voice, type_distribution, slimmed),
        _ad_type_section("Engagement / Brand", "engagement_brand", voice, type_distribution, slimmed),
        _ad_type_section("Webinar / Event", "webinar_event", voice, type_distribution, slimmed),
        _visual_section(visual),
        _positioning_section(advertiser, structure, angles, funnel),
        _synthetic_section(advertiser, synthetic_ads),
        _gaps_section(type_distribution, total_ads, slimmed),
    ]

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _voice_section(advertiser: str, voice: dict, ads: list[dict]) -> str:
    lines = ["## Voice Fingerprint"]

    formula = voice.get("headline_formula", "")
    if formula:
        lines.append(f"\n**Headline formula:** `{formula}`")

    avg_len = voice.get("avg_sentence_length")
    tone = voice.get("tone_descriptors", [])
    vocab = voice.get("vocabulary_level", "")
    rhythm_parts = []
    if avg_len:
        rhythm_parts.append(f"~{avg_len}-word sentences")
    if tone:
        rhythm_parts.append(f"tone: {', '.join(tone)}")
    if vocab:
        rhythm_parts.append(f"vocabulary: {vocab}")
    if rhythm_parts:
        lines.append(f"\n**Rhythm and tone:** {'; '.join(rhythm_parts)}.")

    opening = voice.get("opening_hook_pattern", "")
    if opening:
        lines.append(f"\n**Opening hook pattern:** {opening}")

    sig = voice.get("signature_phrases", [])
    if sig:
        lines.append("\n**Signature phrases — use these to sound on-brand:**")
        for p in sig:
            lines.append(f"- {p}")

    ctas = voice.get("cta_patterns", [])
    if ctas:
        lines.append("\n**CTA constructions:**")
        for c in ctas:
            lines.append(f"- `{c}`")

    never = voice.get("what_they_never_say", [])
    if never:
        lines.append("\n**What they never say (guardrails):**")
        for n in never:
            lines.append(f"- {n}")

    real = [a for a in ads if (a.get("headline") or "").strip()][:8]
    if real:
        lines.append("\n**Verbatim headlines from the data:**")
        for ad in real:
            hl = (ad.get("headline") or "").strip()
            cta = (ad.get("cta") or "").strip()
            line = f"> {hl}"
            if cta:
                line += f"  _({cta})_"
            lines.append(line)

    return "\n".join(lines)


def _ad_type_section(title: str, key: str, voice: dict, type_dist: dict, ads: list[dict]) -> str:
    count = type_dist.get(key, 0)
    lines = [f"## Ad Type: {title}"]

    if count == 0:
        lines.append("\nInsufficient data from this scrape.")
        return "\n".join(lines)

    if count < 3:
        lines.append(f"\n_Note: only {count} example(s) classified as this type — patterns are low-confidence._")

    by_type = voice.get("by_type", {}).get(key, {})
    pattern = by_type.get("headline_pattern", "")
    example = by_type.get("example_headline", "")

    if pattern:
        lines.append(f"\n**Headline formula:** `{pattern}`")

    if example:
        lines.append(f"\n**Example from the data:**\n> {example}")

    type_ads = [a for a in ads if a.get("ad_type") == key and (a.get("headline") or "").strip()][:4]
    if type_ads:
        lines.append("\n**Observed ads:**")
        for ad in type_ads:
            hl = (ad.get("headline") or "").strip()
            pt = (ad.get("primary_text") or "").strip()[:150]
            cta = (ad.get("cta") or "").strip()
            lines.append(f"> **{hl}**")
            if pt:
                lines.append(f"> {pt}")
            if cta:
                lines.append(f"> _{cta}_")
            lines.append(">")

    lines.append("\n**Formula for writing a new ad of this type:**")
    if pattern:
        lines.append(f"1. Headline: `{pattern}`")
    ctas = voice.get("cta_patterns", [])
    if ctas:
        lines.append(f"2. CTA: choose from {', '.join(f'`{c}`' for c in ctas[:3])}")
    never = voice.get("what_they_never_say", [])
    if never:
        lines.append(f"3. Avoid: {'; '.join(never[:3])}")

    return "\n".join(lines)


def _visual_section(visual: dict) -> str:
    lines = ["## Visual Patterns"]

    style = visual.get("dominant_visual_style", "")
    if style:
        lines.append(f"\n**Dominant style _(inferred)_:** {style}")

    patterns = visual.get("image_content_patterns", [])
    if patterns:
        lines.append("\n**Image content patterns _(inferred from format metadata)_:**")
        for p in patterns:
            lines.append(f"- {p}")

    text_overlay = visual.get("text_overlay_usage", "")
    if text_overlay:
        lines.append(f"\n**Text overlay usage:** {text_overlay}")

    colors = visual.get("color_palette_signals", [])
    if colors:
        lines.append("\n**Color palette signals:**")
        for c in colors:
            lines.append(f"- {c}")

    video = visual.get("video_style")
    if video and str(video).lower() not in ("null", "none", ""):
        lines.append(f"\n**Video style:** {video}")

    score = visual.get("visual_consistency_score")
    if score is not None:
        lines.append(f"\n**Visual consistency score:** {score:.1f}/1.0")

    if not any([style, patterns, text_overlay, colors]):
        lines.append(
            "\nInsufficient visual metadata — Google display ads do not expose creative descriptions. "
            "Add Meta or LinkedIn to get actual visual data."
        )

    return "\n".join(lines)


def _positioning_section(advertiser: str, structure: dict, angles: list[dict], funnel: dict) -> str:
    lines = ["## Positioning Map"]

    funnel_approach = structure.get("funnel_approach", "")
    if funnel_approach:
        lines.append(f"\n**Funnel approach:** {funnel_approach}")

    campaign_types = structure.get("campaign_types_observed", [])
    if campaign_types:
        lines.append("\n**Campaign types observed:**")
        for t in campaign_types:
            lines.append(f"- {t}")

    testing = structure.get("testing_behavior", "")
    if testing:
        lines.append(f"\n**Testing behavior:** {testing}")

    if angles:
        angle_counts = Counter(a.get("angle", "unknown") for a in angles)
        top_angles = angle_counts.most_common(5)
        lines.append("\n**Top messaging angles (by frequency):**")
        for angle, count in top_angles:
            lines.append(f"- {angle.replace('_', ' ')}: {count} ads")

    funnel_dist = funnel.get("funnel_distribution", {})
    if funnel_dist:
        lines.append("\n**Funnel stage distribution:**")
        for stage, count in funnel_dist.items():
            lines.append(f"- {stage}: {count} ads")

    platform_strategy = structure.get("platform_strategy", {})
    google_strat = platform_strategy.get("google", "")
    if google_strat:
        lines.append(f"\n**Google strategy:** {google_strat}")

    return "\n".join(lines)


def _synthetic_section(advertiser: str, synthetic_ads: list[dict]) -> str:
    lines = ["## Synthetic Ad Templates"]

    if not synthetic_ads:
        lines.append(
            "\nInsufficient data — re-run with more copy samples to generate synthetic ads."
        )
        return "\n".join(lines)

    lines.append(
        f"\n_{len(synthetic_ads)} ads generated in {advertiser}'s voice from the fingerprint above. "
        f"Use these to calibrate new copy before publishing._"
    )

    for i, ad in enumerate(synthetic_ads, 1):
        ad_type = (ad.get("type") or "unknown").replace("_", " ").title()
        hl = (ad.get("headline") or "").strip()
        pt = (ad.get("primary_text") or "").strip()
        cta = (ad.get("cta") or "").strip()
        visual = (ad.get("visual_description") or "").strip()
        why = (ad.get("why_on_brand") or "").strip()

        lines.append(f"\n### Template {i} — {ad_type}")
        if hl:
            lines.append(f"**Headline:** {hl}")
        if pt:
            lines.append(f"> {pt}")
        if cta:
            lines.append(f"**CTA:** `{cta}`")
        if visual:
            lines.append(f"**Visual:** _{visual}_")
        if why:
            lines.append(f"_Voice pattern: {why}_")

    return "\n".join(lines)


def _gaps_section(type_dist: dict, total_ads: int, ads: list[dict]) -> str:
    lines = ["## What We Don't Know Yet"]

    lines.append(
        f"\n**Copy coverage:** vision extraction yielded readable copy for a subset of "
        f"{total_ads} scraped ads. Analysis quality scales with copy coverage."
    )

    unknown_count = type_dist.get("unknown", 0)
    if unknown_count:
        lines.append(
            f"- {unknown_count} ads could not be type-classified — "
            "more vision coverage would reduce this."
        )

    if not type_dist.get("webinar_event"):
        lines.append(
            "- No webinar/event ads in this dataset — HubSpot may not run them on Google, "
            "or they weren't in this scrape window."
        )

    engagement_count = type_dist.get("engagement_brand", 0)
    if engagement_count < 3:
        lines.append(
            f"- Only {engagement_count} engagement/brand ad(s) observed. "
            "Google skews toward direct response — add Meta or LinkedIn for awareness patterns."
        )

    lines.append(
        "- This scrape covers Google only. Meta adds video/carousel formats; "
        "LinkedIn adds job-title-targeted copy patterns."
    )
    lines.append(
        "- No competitor data included. Adding competitors would surface white-space angles "
        "and reveal what HubSpot is deliberately not saying."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slim_ads(ads: list[dict]) -> list[dict]:
    return [
        {
            "platform": a.get("platform", ""),
            "format": a.get("format", ""),
            "headline": a.get("headline", ""),
            "primary_text": (a.get("primary_text") or "")[:300],
            "cta": a.get("cta", ""),
            "impressions_range": a.get("impressions_range", ""),
            "ad_type": a.get("ad_type", "unknown"),
        }
        for a in ads
    ]
