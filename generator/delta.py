"""Competitive signal delta generator.

Compares two analysis snapshots for the same advertiser and produces a
structured signal report: what changed, what it likely means strategically.

Called from main.py after saving new analysis, passing the previous snapshot
fetched from the DB before the new save.
"""

import json
import os
from datetime import date as _date, datetime as _datetime

_MIN_BASELINE_DAYS = 7  # deltas within this window are too noisy to be meaningful

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
# Programmatic diff helpers
# ---------------------------------------------------------------------------

def _angle_frequencies(angles_data) -> dict[str, int]:
    """Aggregate angle list into {angle_name: count}."""
    if not isinstance(angles_data, list):
        return {}
    counts: dict[str, int] = {}
    for item in angles_data:
        if isinstance(item, dict):
            angle = item.get("angle") or item.get("angle_name", "")
            if angle:
                counts[angle] = counts.get(angle, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


def _list_diff(prev: list, curr: list) -> dict:
    prev_set = {str(x).strip() for x in (prev or []) if x}
    curr_set = {str(x).strip() for x in (curr or []) if x}
    return {
        "added": sorted(curr_set - prev_set),
        "removed": sorted(prev_set - curr_set),
        "unchanged_count": len(prev_set & curr_set),
    }


def _pct(count: int, total: int) -> str:
    if not total:
        return "0%"
    return f"{round(100 * count / total)}%"


def compute_diffs(prev: dict, curr: dict) -> dict:
    """Compute structured diffs between two analysis snapshots.

    Returns a dict of diffs per dimension, ready to pass to Gemini.
    """
    diffs: dict = {}

    # --- Ad type distribution ---
    prev_td = prev.get("type_distribution", {})
    curr_td = curr.get("type_distribution", {})
    if prev_td or curr_td:
        prev_total = sum(prev_td.values()) if isinstance(prev_td, dict) else 0
        curr_total = sum(curr_td.values()) if isinstance(curr_td, dict) else 0
        all_types = set(prev_td) | set(curr_td)
        diffs["ad_type_mix"] = {
            t: {
                "prev": f"{prev_td.get(t, 0)} ({_pct(prev_td.get(t, 0), prev_total)})",
                "curr": f"{curr_td.get(t, 0)} ({_pct(curr_td.get(t, 0), curr_total)})",
            }
            for t in sorted(all_types)
        }

    # --- Funnel distribution ---
    prev_f = prev.get("funnel", {}).get("funnel_distribution", {})
    curr_f = curr.get("funnel", {}).get("funnel_distribution", {})
    if prev_f or curr_f:
        prev_total = sum(prev_f.values()) if isinstance(prev_f, dict) else 0
        curr_total = sum(curr_f.values()) if isinstance(curr_f, dict) else 0
        diffs["funnel_distribution"] = {
            stage: {
                "prev": f"{prev_f.get(stage, 0)} ({_pct(prev_f.get(stage, 0), prev_total)})",
                "curr": f"{curr_f.get(stage, 0)} ({_pct(curr_f.get(stage, 0), curr_total)})",
            }
            for stage in ["TOFU", "MOFU", "BOFU"]
        }

    # --- Angle frequency ---
    prev_angles = _angle_frequencies(prev.get("angles", []))
    curr_angles = _angle_frequencies(curr.get("angles", []))
    if prev_angles or curr_angles:
        all_angles = set(prev_angles) | set(curr_angles)
        prev_total = sum(prev_angles.values()) or 1
        curr_total = sum(curr_angles.values()) or 1
        diffs["angle_frequencies"] = {
            a: {
                "prev": f"{prev_angles.get(a, 0)} ({_pct(prev_angles.get(a, 0), prev_total)})",
                "curr": f"{curr_angles.get(a, 0)} ({_pct(curr_angles.get(a, 0), curr_total)})",
            }
            for a in sorted(all_angles)
        }

    # --- Voice: CTAs ---
    prev_ctas = prev.get("voice", {}).get("cta_patterns") or prev.get("voice", {}).get("cta_constructions") or []
    curr_ctas = curr.get("voice", {}).get("cta_patterns") or curr.get("voice", {}).get("cta_constructions") or []
    diffs["ctas"] = _list_diff(prev_ctas, curr_ctas)

    # --- Voice: signature phrases ---
    prev_phrases = prev.get("voice", {}).get("signature_phrases") or []
    curr_phrases = curr.get("voice", {}).get("signature_phrases") or []
    diffs["signature_phrases"] = _list_diff(prev_phrases, curr_phrases)

    # --- Voice: tone ---
    prev_tone = prev.get("voice", {}).get("tone_descriptors") or []
    curr_tone = curr.get("voice", {}).get("tone_descriptors") or []
    diffs["tone_descriptors"] = _list_diff(prev_tone, curr_tone)

    # --- Visual: consistency score ---
    prev_vs = prev.get("visual", {}).get("visual_consistency_score")
    curr_vs = curr.get("visual", {}).get("visual_consistency_score")
    if prev_vs is not None or curr_vs is not None:
        diffs["visual_consistency_score"] = {"prev": prev_vs, "curr": curr_vs}

    # --- Market context: competitors ---
    prev_comps = [c.get("name", "") for c in (prev.get("market_context", {}).get("competitors") or []) if isinstance(c, dict)]
    curr_comps = [c.get("name", "") for c in (curr.get("market_context", {}).get("competitors") or []) if isinstance(c, dict)]
    diffs["competitors_mentioned"] = _list_diff(prev_comps, curr_comps)

    # --- Market context: saturated strategies ---
    prev_sat = prev.get("market_context", {}).get("saturated_strategies") or []
    curr_sat = curr.get("market_context", {}).get("saturated_strategies") or []
    diffs["saturated_strategies"] = _list_diff(prev_sat, curr_sat)

    # --- Market context: whitespace ---
    prev_ws = prev.get("market_context", {}).get("whitespace") or []
    curr_ws = curr.get("market_context", {}).get("whitespace") or []
    diffs["whitespace"] = _list_diff(prev_ws, curr_ws)

    return diffs


# ---------------------------------------------------------------------------
# Gemini prompt
# ---------------------------------------------------------------------------

_SYSTEM = (
    "You are a competitive intelligence analyst. "
    "Write precise, actionable signal reports from ad creative data. "
    "Focus on strategic implications, not data description. "
    "Use only the data provided — do not speculate beyond what the diffs show."
)

_PROMPT = """\
You are comparing two ad intelligence analysis runs for {advertiser}.

Previous run: {prev_date} ({prev_ads} ads analyzed)
Current run:  {curr_date} ({curr_ads} ads analyzed)

---

## Programmatic diffs (computed from structured analysis data)

{diffs_json}

---

## Previous voice fingerprint (summary)
{prev_voice_summary}

## Current voice fingerprint (summary)
{curr_voice_summary}

## Previous campaign structure
{prev_structure}

## Current campaign structure
{curr_structure}

---

Write a competitive signal report in this exact structure:

# {advertiser} — Competitive Intelligence Signal
_{curr_date} · Compared against {prev_date} baseline · {curr_ads} ads analyzed_

## What Changed

For each meaningful change (skip noise-level or data-size-driven variance):
### Signal: [short title]
**Observed:** [what the data shows, with numbers from the diffs]
**Implication:** [what this likely means about their strategy — be specific, not generic]

Evidence rule: if you state that a brand is "now using" a specific messaging angle, tone, or competitive tactic, \
you must be able to point to a specific data field in the diffs that supports it. \
Do not promote an inference to a confirmed signal — if the diffs show a shift in angle frequency, \
say "angle frequency shifted"; do not say "ads now explicitly mention X" unless explicit_mentions \
or a verbatim phrase appears in the diff data.

If nothing meaningful changed, say so in one sentence.

## What Held Constant
1-3 bullets: patterns stable across both runs. These are reliable fingerprints.

## Watch List
1-3 bullets: early signals worth monitoring — not conclusive yet, but directionally interesting. \
If none, omit this section.

## Recommended Action for {advertiser}'s Competitors
2-3 bullets: specific, concrete actions a competing advertiser should take based on these signals.
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _days_between(d1: str, d2: str) -> int | None:
    """Return integer days between two ISO date strings, or None if unparseable."""
    try:
        t1 = _datetime.fromisoformat(d1[:10])
        t2 = _datetime.fromisoformat(d2[:10])
        return abs((t2 - t1).days)
    except Exception:
        return None


async def generate(
    advertiser: str,
    prev_analysis: dict,
    curr_analysis: dict,
    prev_date: str,
    curr_date: str,
    prev_ads: int,
    curr_ads: int,
) -> str:
    # Guard: same-day or too-close baselines produce noise, not signal
    gap = _days_between(prev_date, curr_date)
    if gap is not None and gap < _MIN_BASELINE_DAYS:
        next_valid = prev_date  # placeholder — caller can compute exactly
        return (
            f"# {advertiser} — Competitive Intelligence Signal\n"
            f"_{curr_date} · Compared against {prev_date} baseline · {curr_ads} ads analyzed_\n\n"
            f"> **Baseline comparison invalid.** Only {gap} day(s) between runs "
            f"(minimum: {_MIN_BASELINE_DAYS}). Deltas within this window reflect "
            f"ad rotation variance, not strategic shifts. "
            f"Re-run after {_MIN_BASELINE_DAYS} days from {prev_date} for a valid comparison.\n\n"
            f"No signal analysis generated."
        )

    diffs = compute_diffs(prev_analysis, curr_analysis)

    prev_voice = prev_analysis.get("voice", {})
    curr_voice = curr_analysis.get("voice", {})

    def _voice_summary(v: dict) -> str:
        if not v:
            return "No data"
        return json.dumps({
            "headline_formulas": [f.get("formula") for f in (v.get("headline_formulas") or [])],
            "tone": v.get("tone_descriptors"),
            "ctas": v.get("cta_patterns") or v.get("cta_constructions"),
            "signature_phrases": v.get("signature_phrases"),
            "avg_sentence_length": v.get("avg_sentence_length"),
        }, indent=2)

    def _structure_summary(s: dict) -> str:
        if not s:
            return "No data"
        return json.dumps({
            "funnel_approach": (s.get("funnel_approach") or "")[:300],
            "campaign_types": s.get("campaign_types_observed"),
            "testing_behavior": (s.get("testing_behavior") or "")[:200],
            "budget_signals": (s.get("budget_signals") or "")[:200],
        }, indent=2)

    prompt = _PROMPT.format(
        advertiser=advertiser,
        prev_date=prev_date or "unknown",
        curr_date=curr_date or _date.today().isoformat(),
        prev_ads=prev_ads,
        curr_ads=curr_ads,
        diffs_json=json.dumps(diffs, indent=2),
        prev_voice_summary=_voice_summary(prev_voice),
        curr_voice_summary=_voice_summary(curr_voice),
        prev_structure=_structure_summary(prev_analysis.get("structure", {})),
        curr_structure=_structure_summary(curr_analysis.get("structure", {})),
    )

    client = _get_client()
    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            system_instruction=_SYSTEM,
            temperature=0.2,
        ),
    )
    return (response.text or "").strip()
