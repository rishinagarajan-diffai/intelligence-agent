# Campaign Intelligence Agent — Current State

_Last updated: 2026-05-20_

---

## What This Does

Three-phase pipeline:

1. **Scrape** — pull ad creative from Meta Ad Library, Google Ads Transparency Center, and LinkedIn Ads Library for a target advertiser + its competitors
2. **Analyze** — run a six-pass LLM analysis via Ollama (local) to extract voice, angles, funnel mapping, visual patterns, campaign structure, and competitive gaps
3. **Generate** — synthesize a "Brand DNA" markdown file suitable for feeding into a new LLM session to write on-brand ads

---

## How to Run

```bash
cd /Users/differentlabs/different/intelligence-agent
source .venv/bin/activate
python main.py --advertiser "Notion" --competitors "Coda" "Monday.com" --platforms google
```

Available platforms: `meta`, `google`, `linkedin`

All three platforms: `--platforms meta google linkedin`

---

## Environment

File: `/Users/differentlabs/different/intelligence-agent/.env`

```
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=gemma4:e4b
META_ACCESS_TOKEN=EAAa2ViZC3RbgBRg...  (user's token, DO NOT COMMIT)
```

Ollama must be running: `ollama serve`

Model must be pulled: `ollama pull gemma4:e4b` (9.6 GB)

---

## File Structure

```
intelligence-agent/
├── main.py                  # CLI entry point + orchestrator
├── requirements.txt
├── .env                     # local env vars (gitignored)
├── current-state.md         # this file
├── intelligence.db          # SQLite persistence
├── outputs/                 # generated Brand DNA markdown files
│   └── notion-brand-dna-2026-05-20.md
├── scrapers/
│   ├── meta.py              # Meta Ad Library (4-tier fallback)
│   ├── google.py            # Google Ads Transparency Center (Playwright + RPC)
│   └── linkedin.py          # LinkedIn Ads Library (Playwright)
├── analysis/
│   └── agent.py             # Six-pass Ollama analysis pipeline
├── generator/
│   └── markdown.py          # Brand DNA markdown synthesis (Ollama)
└── storage/
    └── db.py                # SQLite: ads, analysis_results, brand_dna tables
```

---

## Scraper Status

### Google — WORKING
- Uses Playwright (headless Chromium) to navigate `adstransparency.google.com`
- Intercepts internal `SearchCreatives` XHR/RPC responses
- Protobuf-JSON field map: `"2"=creative_id, "3"=content, "4"=format(1=image,2=video,3=responsive), "6"=start_ts, "7"=end_ts, "12"=advertiser_name, "13"=impression_rank`
- Format 3 (responsive display): fetches preview JS URL, extracts human-readable copy with `_extract_responsive_copy()` + `_JS_NOISE` filter
- Returns 100 ads reliably for large advertisers (Notion, Coda, Monday.com confirmed)

### Meta — PARTIALLY WORKING
- Token is set (`META_ACCESS_TOKEN` in `.env`)
- **Blocker**: the Facebook app that owns the token needs Ad Library API access approved at `facebook.com/ads/library/api` — this is a manual application process (~days turnaround)
- Fallback chain: Graph API → unauthenticated library API → `__NEXT_DATA__` web scrape → Playwright
- Without API approval, returns 0 ads (web scrape blocked by bot detection)
- **User action required**: apply for Ad Library API access at `facebook.com/ads/library/api`

### LinkedIn — NOT TESTED
- Code exists in `scrapers/linkedin.py`
- Uses Playwright, scrapes `linkedin.com/ad-library/`
- Company ID map hardcoded in `main.py._LINKEDIN_IDS` for: notion, coda, confluence, atlassian, monday.com, airtable, asana, clickup
- Can override via `LINKEDIN_COMPANY_IDS=notion:10257271,coda:18480454` env var
- Untested; likely works but may need selector updates if LinkedIn changed their DOM

---

## Analysis Agent (analysis/agent.py)

### Architecture
Six passes — 1-4 run concurrently, 5 needs 1-4, 6 needs competitor passes:

| Pass | Name | What it does |
|------|------|-------------|
| 1 | Voice fingerprint | Sentence length, tone, vocabulary, CTAs, what they avoid |
| 2 | Angle classification | Tags each ad: pain_point, outcome_led, social_proof, etc. |
| 3 | Format & funnel mapping | TOFU/MOFU/BOFU distribution by format |
| 4 | Visual pattern analysis | Color signals, text-overlay usage, video style |
| 5 | Campaign structure | How they build campaigns, platform strategy, budget signals |
| 6 | Competitive gap map | Angles client owns vs. competitors, white space opportunities |

### Ollama / gemma4:e4b quirks (fixed)
1. **`format="json"` never returns bare arrays** — gemma4 always wraps in an object. Fixed by:
   - Angle prompt asks for `{"classifications": [...]}` wrapper
   - `_pass_angles()` unwraps: checks keys `("classifications", "ads", "results", "angles")`
2. **100-ad context overflow** — 100 ads as JSON exceeds gemma4's comfortable context. Fixed by:
   - `_is_real_copy()` filter strips JS noise (error strings, code fragments) before capping
   - Passes 1-4 all cap at 30-40 ads after filtering
3. **JS noise in responsive display ads** — Google's format-3 ads return a JS preview URL; extracted text contained strings like `"Symbol is not a constructor"` or `"Assertion failed"`. Fixed with `_JS_PATTERNS` regex filter in both the scraper and the voice pass.

### Known remaining issues
- Pass 5 (campaign structure) receives the aggregated output of passes 1-4 as JSON input; if passes 2-4 return empty results (due to parsing failures), pass 5 has little to work with — the output is still generated but will be generic
- Pass 6 (competitive gaps) only runs angle + funnel sub-passes on competitors, not all 6 — this is intentional to save inference time

---

## Generator (generator/markdown.py)

- Single Ollama call, no `format="json"` (freeform markdown)
- `num_predict: 4096` — sufficient for the ~800-1200 word output
- Temperature 0.3 for more factual, less creative output
- Prepends a header with advertiser name, date, and ad count
- Output lands in `outputs/{slug}-brand-dna-{date}.md`

---

## Last Successful Run

```
Advertiser: Notion
Competitors: Coda, Monday.com
Platforms: google
Notion ads scraped: 100
Competitor ads scraped: 200 (100 each)
All 6 passes: completed (passes 2-4 had JSON parse warnings with empty results)
Output: outputs/notion-brand-dna-2026-05-20.md
```

Passes 2-4 produced "unparseable JSON" warnings in the last run — this was the context overflow + array-wrapping issue. The fixes described above (30-ad cap + real-copy filter) were applied after that run.

---

## Next Steps

1. **Rerun with fixes** — `python main.py --advertiser "Notion" --competitors "Coda" "Monday.com" --platforms google` and verify passes 2-4 return valid JSON with angle/funnel/visual data
2. **Meta Ad Library access** — apply at `facebook.com/ads/library/api` (user action)
3. **LinkedIn test** — run with `--platforms linkedin` and verify scraper works
4. **Multi-client** — the architecture is single-client; the broader Different Labs platform needs a `client_id` wrapper to support multiple advertisers (see `CLAUDE.md`)

---

## Key Implementation Notes

### Google scraper: why Playwright?
The Ads Transparency Center is an Angular SPA with bot detection. The only reliable approach is intercepting the `SearchCreatives` internal RPC via Playwright network response events. There is no public API.

### Ollama over Anthropic API
User chose Ollama (local inference) over cloud APIs for this component. Model: `gemma4:e4b` (4B parameter efficient variant of Google's Gemma 4 family).

### Meta token
The `META_ACCESS_TOKEN` in `.env` is the user's personal token. Do NOT commit it. The token is valid but the app backing it needs Ad Library API scope approval before `scraper/meta.py` will return ads via the Graph API path.

### SQLite persistence
`intelligence.db` stores all scraped ads, analysis results, and generated brand DNA docs. Schema in `storage/db.py`. Useful for re-running analysis without re-scraping.
