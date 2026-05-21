# Agent History — Marketing Intelligence Agent

Running log of all code changes. Newest entries at the top.
See CLAUDE.md for the logging format and rules.

---

## 2026-05-21 — Replace Ollama markdown call with programmatic document builder

**What changed:**
- `generator/markdown.py` — complete rewrite. Removed Ollama API call and `MARKDOWN_PROMPT` template. Now builds the entire Brand DNA document in Python from structured analysis data. New helpers: `_voice_section()`, `_ad_type_section()`, `_visual_section()`, `_positioning_section()`, `_synthetic_section()`, `_gaps_section()`. `_slim_ads()` now includes `ad_type` field.
- `main.py` — bumped `sample_ads` cap from 10 to 20 (more per-type examples in section builders); updated console message from "Calling Claude..." to "Building Brand DNA from analysis data..."

**Why:** gemma4:e4b does not follow explicit section format instructions for long-form markdown generation. Every pipeline run produced a different document structure with generic consulting-speak and no citations from the actual data — defeating the entire purpose of a preference model. The model's structured JSON outputs from passes 1–6 already contain everything the document needs (headline formulas, signature phrases, CTA patterns, guardrails, per-type breakdowns, synthetic ads). Building from that data directly is faster, deterministic, and always grounded in observed evidence.

**How it helps:** Document is now guaranteed to include verbatim examples, synthetic ad templates, per-type formulas, and data gap disclosures on every run. No model call in Phase 3 means no additional timeout risk and no hallucination in the output.

**Traceback notes:**
- `_ad_type_section()` filters `ads` by `ad_type == key` — this only works if `ad_type` is present on the ad dicts. It is set by `run_all_passes()` in `analysis/agent.py` (Pass 0 attaches `ad["ad_type"]` in memory). If Pass 0 fails or returns empty, all ads will have `ad_type = "unknown"` and per-type sections will show "Insufficient data."
- The `_gaps_section()` uses `total_ads` (the full count) but `ads` is only the top-20 sample. Copy coverage note is therefore approximate.
- `OLLAMA_HOST` and `MODEL` env vars remain imported for environment consistency but are unused in this module.

---

## 2026-05-21 — Preference learning prompts: ad type classifier, voice reframe, synthetic generator

**What changed:**
- `analysis/agent.py` — added `CLASSIFIER_PROMPT` and `_pass_classify()` (Pass 0: classifies each ad as form_lead_gen / engagement_brand / webinar_event / unknown before any other pass); rewrote `VOICE_PROMPT` and `_pass_voice()` to answer "what do I need to write a new ad?" rather than "what patterns exist?" (adds `headline_formula`, `signature_phrases`, `what_they_never_say`, `by_type` breakdowns); added `SYNTHETIC_PROMPT` and `_pass_synthetic()` (Pass 6: generates 3 on-brand synthetic ads from voice fingerprint + real examples); updated `run_all_passes()` to run Pass 0 before everything, attach `ad_type` labels to ads in memory, run Pass 6 after voice, and return `synthetic` and `type_distribution` in result dict
- `generator/markdown.py` — rewrote `MARKDOWN_PROMPT` for preference learning format with sections: How to Use, Voice Fingerprint, Ad Type: Form/Lead Gen, Ad Type: Engagement/Brand, Ad Type: Webinar/Event, Visual Patterns, Positioning Map, Synthetic Ad Templates, What We Don't Know Yet; updated `generate()` to accept `type_distribution` and `synthetic` from analysis, build `synthetic_section` markdown via new `_build_synthetic_section()` helper, and pass both to the prompt; updated system prompt framing from "brand intelligence report" to "preference learning model"

**Why:** The first successful pipeline run (15 copy samples, HubSpot, 2026-05-21) produced a real but generic Brand DNA — valid patterns, but not structured for the actual use case. The goal is not a report; it's a preference model: a reader should be able to pick it up and write a new HubSpot ad without ever having seen a real one. That requires per-ad-type formulas, verbatim phrase examples, explicit guardrails ("what they never say"), and at least one synthetic example to validate the fingerprint.

**How it helps:** The output now includes type-level breakdown (form/brand/webinar), explicit voice fingerprint structured for imitation, and 3 synthetic example ads generated in the actual brand voice. This is what gets handed to the creative agent downstream.

**Traceback notes:**
- `_pass_classify()` runs before the type labels are available, so it only gets headline + cta (no primary_text for image-only ads). Classification accuracy is lower for vision-extracted ads where primary_text is short. If type_distribution shows mostly "unknown", more vision coverage helps.
- `SYNTHETIC_PROMPT` specifies a hardcoded scenario ("promoting their free CRM platform to small business owners"). This will need to become a parameter when the system is productionized for real clients.
- `generate()` no longer passes `gaps_json` to the prompt — the competitive gap data is not in the new template. Gaps are still in the analysis dict; just not surfaced in markdown until we add that section.
- Pass numbering in console output: classifier is Pass 0/7, voice is Pass 1/6, synthetic is Pass 6/7. The numbering will look inconsistent until we clean up the progress display.

---

## 2026-05-21 — Vision extraction pass for Google display ad creatives

**What changed:**
- `scrapers/vision_extractor.py` — new module, ~120 lines
- `storage/db.py` — added `visual_description` (TEXT) and `vision_extracted` (INTEGER DEFAULT 0) columns; added `update_ad_vision()` and `migrate_vision_columns()` functions
- `main.py` — added Phase 1.5 (vision extraction) between scraping and analysis; added `db.migrate_vision_columns()` call on startup; imports `vision_extractor`

**Why:** Google Ads Transparency returns ~98% of display ads as format 1 (image-only creatives). The scraper correctly captures `image_url` for these ads but the headline/primary_text fields come back empty. All six analysis passes filter on `_is_real_copy()` which requires non-empty text — so 98/100 ads were silently dropped before analysis. The result was a Brand DNA output generated entirely from the model's prior knowledge of HubSpot, not from actual HubSpot ad copy.

**How it helps:** Phase 1.5 fetches each ad image and uses `gemma4:e4b`'s vision capability to read the copy baked into the creative. Extracted headline, primary_text, cta, and a visual description are written back to the DB and passed to the analysis passes. Analysis now runs on real observed copy instead of hallucinated patterns.

**Traceback notes:**
- Vision calls time out at 45s per image — a timed-out image is skipped (not a crash). If you see many timeouts, check Ollama is responsive: `curl http://localhost:11434/api/ps`
- Only the top 25 ads by impressions are processed (cap defined by `_MAX_ADS = 25` in `vision_extractor.py`). Analysis passes use ≤ 40 ads anyway.
- Concurrency is intentionally sequential — `OLLAMA_NUM_PARALLEL=1` means Ollama serializes GPU work regardless. Concurrent asyncio tasks just create queue pressure.
- If Ollama hangs mid-pipeline (as happened on first attempt): `pkill -f "python main.py"`, then `pkill ollama`, restart with `ollama serve`. The hang was caused by no-timeout vision calls blocking forever.
- `migrate_vision_columns()` is safe to run on existing DBs — checks column existence before ALTER TABLE.

---

## 2026-05-21 — Initial commit: intelligence agent codebase

**What changed:**
- `main.py` — orchestrator: scrape → analyze → generate Brand DNA markdown
- `scrapers/google.py` — Playwright-based Google Ads Transparency scraper; intercepts `SearchCreatives` XHR; returns ~100 ads for large advertisers
- `scrapers/meta.py` — Meta Ad Library API scraper (requires Ad Library API scope approval)
- `scrapers/linkedin.py` — LinkedIn Ad Library scraper (untested)
- `analysis/agent.py` — 6-pass Ollama analysis pipeline (passes 1–4 concurrent via asyncio.gather; pass 5 needs 1–4; pass 6 is competitive gap map)
- `generator/markdown.py` — single Ollama call synthesizing Brand DNA markdown from all 6 passes
- `storage/db.py` — SQLite persistence; `ads`, `analysis_results`, `brand_dna` tables
- `requirements.txt` — ollama, requests, beautifulsoup4, playwright, python-dotenv, rich
- `.env.example` — required environment variables
- `.gitignore` — excludes .env, intelligence.db, outputs/, .venv/

**Why:** Foundation for the marketing preference learning system. Goal: scrape a company's historical ad creatives across platforms, run structured analysis, and generate a markdown file that gives a downstream LLM everything it needs to continue that company's advertising without breaking their voice.

**How it helps:** End-to-end pipeline runs on a single command: `python main.py --advertiser "HubSpot" --platforms google`. Output lands in `outputs/{slug}-brand-dna-{date}.md`. SQLite persistence means scraping and analysis are decoupled — re-run analysis passes without re-hitting ad library APIs.

**Traceback notes:**
- `gemma4:e4b` quirk: `format="json"` never returns bare arrays — always wrapped in an object. `_pass_angles()` unwraps using keys `("classifications", "ads", "results", "angles")`.
- Google scraper only: Meta requires Ad Library API scope approval (manual process, takes days). LinkedIn code exists but is untested.
- JS noise from Google responsive display ads (format=3) filtered by `_JS_PATTERNS` regex in both `scrapers/google.py` and `analysis/agent.py`.
- Context overflow fixed by capping at 30–40 ads after `_is_real_copy()` filtering.
