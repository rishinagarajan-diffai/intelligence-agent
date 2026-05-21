# Agent History — Marketing Intelligence Agent

Running log of all code changes. Newest entries at the top.
See CLAUDE.md for the logging format and rules.

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
