# Campaign Intelligence Agent — Current State

_Last updated: 2026-05-27 (polling FastAPI endpoint live; Salesforce smoke test passed)_

---

## What This Does

Three-phase pipeline:

1. **Scrape** — pull ad creative from Google Ads Transparency Center, LinkedIn Ad Library, and Meta Ad Library (Playwright fallback, Graph API when approved) for a target advertiser + competitors
2. **Analyze** — 8-pass Gemini API analysis: ad type classification, voice fingerprint, angle classification, funnel mapping, visual patterns, campaign structure, competitive gaps, market context (Google Search grounded), + synthetic ad generation
3. **Generate** — Gemini writes a "Brand DNA" markdown document from all structured analysis data — suitable for feeding into a creative agent to write on-brand ads

---

## Setup

**Requires Python 3.10+.**

```bash
cd /Users/differentlabs/different/intelligence-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium   # installs the Chromium browser binary
cp .env.example .env                    # then add your GEMINI_API_KEY
```

Get a `GEMINI_API_KEY` at https://aistudio.google.com/apikey (free tier is sufficient).

---

## How to Run

### CLI

```bash
cd /Users/differentlabs/different/intelligence-agent
source .venv/bin/activate

# Basic run — Google + LinkedIn, auto-discovers competitors
python main.py --advertiser "HubSpot" --platforms google linkedin

# All 3 platforms including Meta (Playwright scraper)
python main.py --advertiser "ThoughtSpot" --platforms meta google linkedin

# With explicit competitors (adds dedicated intel fetches on top of auto-discovery)
python main.py --advertiser "HubSpot" --competitors "Salesforce" "Pipedrive" --platforms google linkedin

# With a scenario for synthetic ad generator
python main.py --advertiser "HubSpot" --scenario "promoting their new free CRM tier" --platforms google linkedin
```

**To regenerate Brand DNA only (no scraping, no analysis re-run, ~30s):**
```bash
python regen_dna.py
```

### API (new)

```bash
uvicorn api:app --host 0.0.0.0 --port 8000
```

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/analyze` | Queue a new analysis job — returns `{job_id, status}` immediately |
| `GET` | `/jobs/{job_id}` | Poll job status; returns `brand_dna` + `intel_signal` on `complete` |
| `GET` | `/brand-dna/{advertiser}` | Latest stored Brand DNA for an advertiser |
| `GET` | `/intel-signal/{advertiser}` | Latest stored intel signal delta |
| `POST` | `/regen/{advertiser}` | Regenerate Brand DNA from existing DB data, synchronous (~30s) |

**Example:**
```bash
# Queue job
curl -X POST http://localhost:8000/analyze \
  -H "Content-Type: application/json" \
  -d '{"advertiser":"Salesforce","platforms":["google","linkedin"]}'
# → {"job_id":"b954...","status":"queued"}

# Poll
curl http://localhost:8000/jobs/b954...
# → {"status":"complete","brand_dna":"# Salesforce — Advertising...","intel_signal":"..."}
```

**Polling notes:** Job transitions: `queued` → `running` → `complete` | `failed`. On `complete`, `brand_dna` and `intel_signal` are inline in the `GET /jobs/{id}` response. Recommended poll interval: 15s. Typical run time: 3–5 min.

Available platforms: `meta`, `google`, `linkedin`
Default: `google linkedin`

---

## Environment Variables (`.env`)

| Variable | Required | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | **Yes** | Gemini API key — https://aistudio.google.com/apikey |
| `GEMINI_MODEL` | No (default: `gemini-2.5-flash`) | Model for all passes + vision + generation |
| `META_ACCESS_TOKEN` | For Meta platform | Graph API token — needs Ad Library API approval |
| `LINKEDIN_COMPANY_IDS` | No | Override built-in ID map: `notion:10257271,...` |

---

## File Structure

```
intelligence-agent/
├── main.py                    # CLI entry point + orchestrator
├── api.py                     # FastAPI polling API (uvicorn api:app)
├── regen_dna.py               # Phase 3 only — regenerate Brand DNA from existing DB data
├── requirements.txt
├── .env / .env.example
├── current-state.md           # this file
├── agent-history.md           # full change log (newest first)
├── open-issues.md             # known bugs + backlog
├── intelligence.db            # SQLite persistence (gitignored)
├── outputs/                   # generated Brand DNA markdown files
├── logs/                      # pipeline run logs (gitignored)
├── scrapers/
│   ├── google.py              # Google Ads Transparency Center (Playwright + XHR intercept)
│   ├── linkedin.py            # LinkedIn Ad Library (Playwright, no auth needed)
│   ├── meta.py                # Meta Ad Library (Graph API first, Playwright fallback)
│   ├── vision_extractor.py    # Gemini vision for image-only creatives
│   └── web_fetch.py           # DDG search + homepage fetch utility
├── analysis/
│   └── agent.py               # 8-pass Gemini analysis pipeline
├── generator/
│   ├── markdown.py            # Gemini writes Brand DNA document from analysis JSON
│   └── delta.py               # Gemini computes intel signal delta vs prior run
├── scripts/
│   └── linkedin_refresh.py    # LinkedIn OAuth token refresh helper
└── storage/
    └── db.py                  # SQLite: ads, analysis_results, brand_dna, jobs, intel_signals
```

---

## Scraper Status

### Google — WORKING
- Playwright (headless Chromium) scrapes `adstransparency.google.com`
- Intercepts `SearchCreatives` XHR; protobuf-JSON field map decoded
- Returns ~60–100 ads reliably for large advertisers
- Format 3 (responsive display): fetches preview JS URL via `_extract_responsive_copy()`
- P2-5 fix applied: `_extract_responsive_copy()` now filters strings containing `www.` or starting with `Sponsored`

### LinkedIn — WORKING
- Playwright scrapes `linkedin.com/ad-library/search`
- No auth required — publicly accessible
- Returns ~48–51 ads per run

### Meta — WORKING (Playwright), PENDING (Graph API)
- **Strategy 1 (Graph API):** requires Ad Library API approval — submitted 2026-05-22, still pending
- **Strategy 2 (Playwright):** working — intercepts GraphQL at `facebook.com/api/graphql/`, DOM fallback using "Library ID:" anchor
- **Known issue:** keyword search returns noise (ads from other advertisers whose copy contains the search term). Fix: page-ID filtering once Graph API is approved.
- Test confirmed: 39–49 Meta ads per run for large advertisers

---

## Analysis Agent (analysis/agent.py)

8 passes total — 0 runs first, 1–4 + 6b run concurrently, 5 needs 1–4, 6 needs competitor runs, 7 generates synthetic ads:

| Pass | Name | What it does |
|------|------|-------------|
| 0 | Ad type classifier | Labels each ad: `form_lead_gen`, `engagement_brand`, `webinar_event`, `unknown` |
| 1 | Voice fingerprint | Headline formulas, tone, signature phrases, CTAs, what they never say |
| 2 | Angle classification | Tags each ad: pain_point, outcome_led, free_trial, thought_leadership, etc. |
| 3 | Format & funnel mapping | TOFU/MOFU/BOFU distribution by format |
| 4 | Visual pattern analysis | Color signals, text-overlay usage, dominant style (multimodal: real image bytes) |
| 5 | Campaign structure | Platform strategy, testing behavior, budget signals |
| 6 | Competitive gap map | Angles client owns vs. competitors — requires `--competitors` runs |
| **6b** | **Market context** | **Google Search grounded auto-discovery of competitors, saturated strategies, whitespace** |
| 7 | Synthetic ad generator | 3 on-brand ads generated from voice fingerprint + real examples |

**Pass 6b detail:** Uses `genai_types.Tool(google_search=genai_types.GoogleSearch())` — Gemini performs live Google Search and grounds its response in retrieved results. No `--competitors` flag required. Incompatible with `response_mime_type=application/json` — JSON parsed from free-form text via regex fallback.

---

## Generator (generator/markdown.py)

- Single Gemini call — all structured analysis JSON + 30 sample ads
- `temperature=0.3`
- Output: `outputs/{slug}-brand-dna-{date}.md` + persisted to `brand_dna` DB table

**Brand DNA sections (in order):**
1. How to Use This Document
2. Voice Fingerprint
3. Ad Type: Form / Lead Gen
4. Ad Type: Engagement / Brand
5. Ad Type: Webinar / Event
6. Visual Patterns
7. Positioning Map
8. Competitive Landscape
9. Synthetic Ad Templates
10. What We Don't Know Yet

---

## DB Schema (storage/db.py)

**`ads`** — one row per scraped ad
```
id, platform, advertiser, advertiser_type, ad_id, format,
headline, primary_text, description, cta,
image_url, video_url, start_date, end_date, impressions_range,
scraped_at, raw_json, visual_description, vision_extracted, ad_type
UNIQUE(ad_id, advertiser, platform)
```

**`analysis_results`** — one row per pass per run
```
id, advertiser, pass_name, result_json, created_at
```

**`brand_dna`** — one row per advertiser (DELETE-before-INSERT)
```
id, advertiser, content, created_at
```

**`jobs`** — one row per API job (new)
```
id (UUID), advertiser, competitors (JSON array), platforms (JSON array),
scenario, status (queued|running|complete|failed), error, created_at, completed_at
```

**`intel_signals`** — one row per delta run, accumulates (new)
```
id, advertiser, content, created_at
```

---

## Last Successful Runs (2026-05-27)

| Advertiser | Platforms | DB ads | Output | Model | How run |
|---|---|---|---|---|---|
| Salesforce | google, linkedin | 101 | `outputs/salesforce-brand-dna-2026-05-27.md` | gemini-3.1-pro-preview | API (`POST /analyze`) |
| ThoughtSpot | google, linkedin | 80 | `outputs/thoughtspot-brand-dna-2026-05-27.md` | gemini-3.1-pro-preview | CLI |
| OpenAI | meta, google, linkedin | 110 | `outputs/openai-brand-dna-2026-05-27.md` | gemini-2.5-flash | CLI |
| Anthropic | meta, google, linkedin | ~80 | `outputs/anthropic-brand-dna-2026-05-27.md` | gemini-2.5-flash | CLI |
| Rippling | meta, google, linkedin | ~82 | `outputs/rippling-brand-dna-2026-05-27.md` | gemini-2.5-flash | CLI |

_Note: OpenAI/Anthropic/Rippling Brand DNAs are from pre-KeyError-fix runs. Re-run recommended._

---

## QA Status (2026-05-27)

**API smoke test (Salesforce)** — end-to-end confirmed: `POST /analyze` → job queued, polled to `complete` in ~3.5 min, 16,627-char brand DNA + intel signal both present in `GET /jobs/{id}` response.

**Gemini 3.1 Pro evaluation** — ThoughtSpot run confirmed measurably better instruction following vs. 2.5 Flash. Platform Strategy blockquote renders correctly; writing formulas more specific.

**Phase 1.6 fix** — ownership filter now runs after vision extraction. Escape room ads (Google Display Network ads associated with Rippling's ATC account) are now caught after vision populates their headlines.

**Intel signal baseline guard** — verified working. ThoughtSpot 0-day same-run baseline correctly returned "Baseline comparison invalid."

**`{N}` KeyError fix** — `{{N}}` escaped in `_PROMPT`. Was silently crashing Phase 3 on all post-audit-fix runs.

**P2-6 fix applied (2026-05-26):** `_walk_json()` in `meta.py` now filters by `page.name`.
**P2-7 fix applied (2026-05-26):** CTA rules sentence now explicitly forbids slash-composite CTAs.

---

## Open Issues

See `open-issues.md`. As of 2026-05-27:

| Priority | Item |
|---|---|
| P2 | Re-run OpenAI/Anthropic/Rippling to get post-fix Brand DNAs (prior runs lost to KeyError) |
| P2 | Descope "SSO Documentation" CTA unverified (P2-4) |
| P3 | Pass 0 classifier only runs on top-40 ads — remainder have `ad_type=NULL` in DB |

---

## Production Gaps (Intelligence Agent)

Before this is production-ready as a service:

1. **Meta Graph API approval** — pending since 2026-05-22; unblocks noise-free Meta scraping
2. **Meta page-ID filtering** — post-filter by page name to remove keyword-match noise (interim fix)
3. **SQLite → Postgres** — needed for concurrent multi-tenant runs; `get_connection()` is the only swap point
4. **`client_id` scoping** — no multi-tenancy in DB or scraper layer yet
5. **Scheduled re-scraping** — one-shot today; needs weekly refresh of `intel_signals`
6. **`--limit` CLI flag** — scrape limit not tunable without editing source
7. **LinkedIn auth** — image-only ads have no copy without authenticated scraping
8. **`regen_dna.py` module guard** — runs `asyncio.run(main())` at import time; API bypasses it, but the file needs a `if __name__ == "__main__"` guard to be importable cleanly

---

## Delta Signal Output

Every pipeline run generates a second output alongside the Brand DNA:

- **Brand DNA** — `outputs/{slug}-brand-dna-{date}.md` + `brand_dna` DB table
- **Intel Signal** — `outputs/{slug}-intel-signal-{date}.md` + `intel_signals` DB table

`generator/delta.py` computes programmatic diffs (angle frequencies, funnel distribution, CTAs, signature phrases, visual consistency, competitors) then calls Gemini to interpret strategic implications. Requires a prior run in the DB — first run produces no delta.

ThoughtSpot delta (2026-05-27 vs 2026-05-26) detected: "Dashboards Are Dead. Try AI." as a new signature phrase, pain-point angle 5% → 38%, BOFU 5% → 28%, visual consistency 0.4 → 0.8.

---

## Next Steps

1. Re-run OpenAI, Anthropic, Rippling to get clean post-fix Brand DNAs
2. Decide on hybrid model routing: 3.1 Pro for Phase 3 (Brand DNA + delta), Flash for analysis passes
3. Run Descope + HubSpot to build delta baselines
4. Add `if __name__ == "__main__"` guard to `regen_dna.py` so it's cleanly importable
5. Productionize: Postgres, `client_id`, scheduled re-scraping
