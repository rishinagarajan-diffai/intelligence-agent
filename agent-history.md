# Agent History — Marketing Intelligence Agent

---

## 2026-05-27 — Containerization + Postgres support (Railway deploy)

**What changed:** `storage/db.py` (dual-backend rewrite), `analysis/agent.py` (SYSTEMIC-04 inline query → `db.get_stale_market_context()`), `api.py` (removed raw sqlite3 call in /regen), `requirements.txt` (+psycopg2-binary), `Dockerfile` (new), `.dockerignore` (new), `railway.toml` (new)
**Why:** POC ready to host; Railway + Postgres selected. Playwright/Chromium require a specific base image (`mcr.microsoft.com/playwright/python`) rather than a minimal Python image.
**How it helps:** `docker build` produces a deployable image. `DATABASE_URL` env var switches the entire persistence layer to Postgres — no other config needed. SQLite still works locally when `DATABASE_URL` is unset. `get_stale_market_context()` centralizes the SYSTEMIC-04 JSON query so it uses the right JSON dialect per backend (SQLite `json_extract` vs Postgres `jsonb`).
**Traceback notes:** `_PgConn` wraps psycopg2 to expose the same `execute/executemany/commit/close` interface as sqlite3.Connection so all existing db callers work unchanged. Railway sets `$PORT` at runtime — CMD uses `${PORT:-8000}` so local `docker run` also works without setting PORT. `postgres://` URL prefix auto-corrected to `postgresql://` (Railway emits both).

## 2026-05-28 — CI: import smoke test + Docker build on every PR/push

**What changed:** `.github/workflows/ci.yml` (new). Two jobs: (1) `smoke-test` does `pip install -r requirements.txt`, imports `api` and `worker`, runs `db.init_db()`, asserts `WorkerSettings` config hasn't drifted; (2) `docker-build` builds the Dockerfile via `docker/build-push-action@v5` with GHA cache.
**Why:** Several bugs this session would've been caught by a 30-second CI step instead of a failed Railway deploy: the `{N}` KeyError (Python `.format()` choking on the prompt template), the Railway `startCommand` `$PORT` expansion issue, SYSTEMIC-04 SQL dialect drift. Direct-commits-to-main with no CI = these silently shipped.
**How it helps:** PRs to main and pushes to main now run import + DB-init + Docker-build checks before code reaches production. First run: both jobs green, 3m40s total. Deploy preview URLs (Railway PR environments) require a paid plan and are intentionally skipped per "barebones for demo" stance — CI catches the bug classes we've actually hit; preview URLs mostly matter for design review of UI.
**Traceback notes:** Smoke test relies on `import api` and `import worker` working without REDIS_URL/DATABASE_URL set. Both modules tolerate missing env vars (api uses lazy `_get_redis_pool`, db.py falls through to SQLite). Scrapers (which import playwright) aren't pulled in by these imports because `main as pipeline` is imported inside `run_pipeline()` not at module level. Docker build uses GHA layer caching so subsequent runs should be much faster than 3m40s.

## 2026-05-27 — Redis-backed ARQ worker replaces in-process BackgroundTasks

**What changed:** `worker.py` (new — ARQ `WorkerSettings` + `run_pipeline` task), `api.py` (drops `BackgroundTasks`, `POST /analyze` now enqueues to Redis via `arq.create_pool`), `Dockerfile` (`/start.sh` dispatches on `SERVICE_MODE` env var so the same image runs as API or worker), `requirements.txt` (+arq), `railway.toml` (drop `healthcheckPath` so worker without HTTP server can deploy). New Railway services: `Redis` (addon, id `d032a83b`), `intelligence-worker` (id `b4f6354e`).
**Why:** Auto-deploy is now wired. Every push kills any in-flight `BackgroundTasks` job mid-pipeline. The DB row stays `running` forever, no exception for Sentry to capture, customer never gets a result.
**How it helps:** Jobs go into Redis on `POST /analyze` and survive API restarts. The worker is a completely separate Railway service running the same Docker image with `SERVICE_MODE=worker`, so deploying the API doesn't touch the worker. Verified end-to-end with Notion: submitted at 00:18:40, picked up by worker within 6s, completed at 00:22:48 (~4 min), 12.9k char Brand DNA returned via `GET /jobs/{id}`. WorkerSettings: `max_jobs=1` (serial), `max_tries=1` (no retry — caller resubmits), `job_timeout=900` (15min buffer over typical 4min).
**Traceback notes:** Three operational gotchas hit during setup. (1) Railway access tokens from OAuth expire in ~1 hour — long batches of API calls hit 401 mid-stream; refresh by having the user run `railway whoami` in their terminal. (2) `railway.toml` deploy config OVERRIDES service-level `serviceInstanceUpdate` mutations — setting `healthcheckPath=""` via API didn't take because the toml's `/docs` value kept being re-applied on each deploy. Had to remove it from toml entirely. (3) Worker service was marked FAILED by Railway despite `arq worker` actually running fine, because Railway expected the toml's `/docs` healthcheck (no HTTP server on worker). Fix: drop healthcheck from toml; both services rely on container-startup success now. (4) Worker service created via API didn't have webhook auto-deploy until clicked through the dashboard like the API service was — manual `serviceInstanceDeploy(commitSha=HEAD)` works as a fallback. To-do: install Railway GitHub App association on the worker service via dashboard the same way as the API.

## 2026-05-27 — Sentry activated + full performance tracing

**What changed:** `api.py` (`traces_sample_rate` 0.1 → 1.0); `SENTRY_DSN` env var set on Railway with the project's real DSN.
**Why:** Sentry was wired but inert until a DSN was provided. Bumping sample rate to 1.0 captures every request as a performance trace during POC — fine at low volume, dial back later.
**How it helps:** Every uncaught exception in a route or in the background `_run_pipeline` job now reports to the `intelligence-agent` Sentry project with full stack trace. Every API request also generates a performance trace (P95 latency per route, slow-query detection) visible at `sentry.io/performance/`. Setting `SENTRY_DSN` triggered an automatic Railway redeploy via the new webhook.
**Traceback notes:** `traces_sample_rate=1.0` will eat Sentry's free quota faster — drop to 0.1 or 0.25 once volume picks up. DSN is in Railway env vars only; not committed to the repo (Sentry DSNs are sender-only credentials but still treated as secrets).

## 2026-05-27 — Auto-deploy wired via Railway GitHub App (free-tier path)

**What changed:** `.github/workflows/deploy.yml` switched from `on: push` to `on: workflow_dispatch` (manual only); Railway GitHub App installed on the repo via dashboard.
**Why:** Railway free-tier accounts can't mint API tokens with deploy permissions, so the GitHub Actions → Railway GraphQL path was blocked at the `RAILWAY_TOKEN` step. Railway's first-party GitHub App webhook works on free tier and doesn't need an API token at all.
**How it helps:** Every push to `main` now triggers a Railway build automatically via webhook. Verified: commit `4d3ad5d` started building within seconds of push, with zero API calls. The manual `workflow_dispatch` workflow is kept as a fallback (useful if dashboard webhook breaks or for forced redeploys at a specific commit).
**Traceback notes:** When a service is created via the Railway API with `source.repo`, Railway records the repo URL but does NOT install the GitHub App or wire the webhook. The dashboard's "Branch connected to production" message is misleading in this state — it shows the connection exists but the webhook never fires until a human installs the GitHub App via the OAuth flow. Two ways to fix: (1) Disconnect/Reconnect from the dashboard, or (2) install the app directly at `github.com/apps/railway-app/installations/new`. After install, also flip the "Auto Deploy" toggle on (separately from "Wait for CI" which should stay off until there are real tests).

## 2026-05-27 — Hardening pass: auth, rate limiting, vision coverage, Sentry, CI/CD

**What changed:** `api.py` (X-API-Key auth + slowapi rate limits + optional Sentry), `requirements.txt` (+slowapi, +sentry-sdk[fastapi]), `scrapers/vision_extractor.py` (`_MAX_ADS` 25 → 50), `.github/workflows/deploy.yml` (new).
**Why:** Pre-hardening, the hosted API was unauthenticated against my Gemini key — anyone with the URL could spend money. Vision was processing only 25/91 image-only ads. Errors had no observability. Every commit needed a manual `serviceInstanceDeploy` with explicit `commitSha`.
**How it helps:**
- All endpoints (except `/docs`, `/openapi.json`) now require `X-API-Key` header matching server-side `API_KEY` env var. Verified 401 on missing/wrong key, 200 on correct key.
- Per-IP rate limits via slowapi: `/analyze` 5/hr, `/regen` 10/hr, `/jobs/{id}` 60/min, reads 30/min.
- Vision extracts top 50 image-only ads (was 25) — doubles DB coverage for ownership filter + future analysis improvements.
- Sentry initializes only if `SENTRY_DSN` env var set (graceful no-op otherwise). Background job exceptions captured via `sentry_sdk.capture_exception`.
- GitHub Actions workflow (`.github/workflows/deploy.yml`) calls Railway GraphQL `serviceInstanceDeploy` with HEAD's `commitSha` on every push to main. Requires `RAILWAY_TOKEN` GitHub secret (a PAT from `railway.com/account/tokens`, NOT the OAuth access token).
**Traceback notes:** `slowapi` rate-limit decorators require the route handler signature to include `request: Request` — added that to all endpoints. Sentry init MUST happen before FastAPI app creation to catch startup errors. The first GitHub Actions run failed (expected) because `RAILWAY_TOKEN` secret hadn't been added yet — workflow exits cleanly with an error message in that case.

## 2026-05-27 — Live Railway deploy + HubSpot smoke test

**What changed:** No code changes — Railway project provisioning + first live deploy.
**Why:** Move from localhost API to a hosted endpoint so external systems can call the pipeline.
**How it helps:** API now serves at `https://intelligence-api-production-0758.up.railway.app` with the Postgres addon attached via `${{Postgres.DATABASE_URL}}` reference. HubSpot live test: 135 ads scraped + 8-pass analyzed in 3.7 min, 16,302-char brand DNA returned inline via `GET /jobs/{id}`. Docs available at `/docs`. GitHub repo: `https://github.com/rishinagarajan-diffai/intelligence-agent` (public).
**Traceback notes:** Railway provisioning order matters — (1) `serviceCreate` with `source.repo`, (2) `variableUpsert` for `GEMINI_API_KEY`, (3) `variableUpsert` for `DATABASE_URL` = `${{Postgres.DATABASE_URL}}` (literal value, not the resolved URL), (4) `variableUpsert` for `PORT=8000` so the domain target matches uvicorn's bind port, (5) `serviceInstanceDeploy` with `commitSha` arg pointing at HEAD, (6) `serviceDomainCreate` with `targetPort: 8000`. **The `commitSha` arg is critical** — without it, `serviceInstanceDeploy` redeploys the *snapshot at service creation time*, not the latest GitHub commit. This caused 5 consecutive failed builds before I realized Railway was deploying commit `fc5d05d` (the initial one with the broken `startCommand`) on every redeploy. GitHub auto-deploy webhooks are NOT wired up when the service is created via API — only via the dashboard. Workaround: pass `commitSha` explicitly every redeploy, or wire the webhook manually.

## 2026-05-27 — Salesforce API smoke test

**What changed:** No code changes — validation run only.
**Why:** First live end-to-end test of the new polling API with a real advertiser.
**How it helps:** Confirmed full pipeline works via API: `POST /analyze` → job queued, background task ran, `GET /jobs/{id}` returned `complete` in ~3.5 min with 16,627-char brand DNA and intel signal both present in response. 101 Salesforce ads analyzed across google + linkedin. Ad type distribution: engagement_brand: 16, form_lead_gen: 5, webinar_event: 8.
**Traceback notes:** Intel signal was 363 chars (minimal) because this was Salesforce's first run — no prior baseline in DB to delta against.

## 2026-05-27 — Polling FastAPI endpoint

**What changed:** `api.py` (new), `storage/db.py` (new tables + functions), `main.py` (+1 line), `requirements.txt` (+fastapi, uvicorn)
**Why:** POC needs a stable interface for external callers (Slack bots, orchestrators, dashboards) to trigger analysis jobs and retrieve results without waiting on a blocking CLI call.
**How it helps:** `POST /analyze` queues a background job and returns a `job_id` immediately. Callers poll `GET /jobs/{job_id}` until `status == "complete"`, then read `brand_dna` and `intel_signal` from the response. `GET /brand-dna/{advertiser}` and `GET /intel-signal/{advertiser}` retrieve the latest stored results directly. `POST /regen/{advertiser}` regenerates brand DNA from existing DB data synchronously (~30s). Intel signals are now persisted to the `intel_signals` DB table in addition to being written as files.
**Traceback notes:** `main.run()` uses a module-level Rich console — background task output goes to stdout, which is fine. `regen_dna.py` runs `asyncio.run(main())` at import time so the `/regen` endpoint bypasses it and calls `md_generator.generate()` directly. Start server with `uvicorn api:app --host 0.0.0.0 --port 8000`.

## 2026-05-27 — SYSTEMIC-04 + SYSTEMIC-01 (re-fix) + Empty String headline filter

**What changed:**

_SYSTEMIC-04 stale cache fallback_ — `analysis/agent.py` `run_all_passes()`: after the concurrent gather, if `market_context` is `{}`, queries `analysis_results` for the most recent non-empty market_context row for that advertiser (`json_array_length($.competitors) > 0`). If found, uses it with a `[Cached from {date}]` prefix in `market_summary`. Runs in the same shared-state block as the other passes — the gather completes first, then a single DB read fills the gap if needed. Limitation: only helps brands that have previously had a successful Pass 6b result; brands with no cached result still show empty competitive landscape.

_SYSTEMIC-01 trigger fix_ — `generator/markdown.py`: changed `mofu_count < 3` → `mofu_count > 0`. The voice pass returns CTAs in aggregate (never by funnel stage), so MOFU CTAs are always unobserved regardless of how many MOFU-stage ads exist. The fallback should fire whenever MOFU ads are present and inject the safe default CTA guidance. Confirmed firing across all 4 brands.

_Empty String headline formula filter_ — `generator/markdown.py` `generate()`: filters null/empty patterns from both `voice.headline_formulas` (top-level list) and `voice.by_type[type].headline_pattern` before the voice dict is JSON-serialized into the prompt. Replaces empty `by_type` patterns with `"[no headline observed for this ad type]"` so the generator gets accurate signal without the `Empty String` noise.

**Why:** All three were causing the generator to receive bad or missing data: SYSTEMIC-04 left 3 of 4 brands with zero competitive context; SYSTEMIC-01's wrong trigger meant the MOFU fallback never fired; the Empty String pattern caused the agent to receive an unusable headline formula as canonical guidance.
**Traceback notes:** SYSTEMIC-04 stale cache uses `json_array_length` SQLite function — available in SQLite 3.38+ (macOS ships 3.43+). The Empty String filter checks both `headline_formulas` list and `by_type` dict because the voice pass outputs the pattern in two separate structures.

---

## 2026-05-27 — SYSTEMIC-01 + SYSTEMIC-06: MOFU fallback and synthetic type guard

**What changed:** `generator/markdown.py`

_SYSTEMIC-01_ — MOFU CTA fallback is now injected by Python, not left to Gemini to decide. `generate()` reads `funnel_distribution.MOFU`; if < 3 ads, sets `mofu_cta_fallback` to the full fallback blockquote and passes it as a format arg. Previously the prompt told Gemini to add this block "if mid-funnel CTAs are absent" — Gemini was not reliably triggering on that condition.

_SYSTEMIC-06_ — Synthetic ads for unobserved ad types are now dropped in Python before the prompt is built. `generate()` computes `observed_types = set(type_distribution.keys())` and filters `synthetic["synthetic_ads"]` to remove any entry whose `type` field isn't in that set. Prevents Pass 7's webinar/event template from appearing in Brand DNA when `type_distribution` has no `webinar_event` key.

**Why:** Both were prompt-only fixes that Gemini wasn't following reliably. Moving to Python-level logic makes the behavior deterministic regardless of model.
**Traceback notes:** SYSTEMIC-01 threshold is 3 MOFU ads — adjust if cadence changes. SYSTEMIC-06 relies on `type` field in synthetic ads matching keys in `type_distribution`; Pass 7 prompt uses `form_lead_gen`, `engagement_brand`, `webinar_event` — same keys the classifier uses.

---

## 2026-05-27 — Gemini 3.1 Pro evaluation run (ThoughtSpot)

**What changed:** No code change. Pipeline run with `GEMINI_MODEL=gemini-3.1-pro-preview` against ThoughtSpot (google + linkedin, 80 ads after ownership filter).
**Why:** Evaluating whether 3.1 Pro produces meaningfully better Brand DNA output vs. 2.5 Flash before committing to a model upgrade or hybrid routing.
**Findings:**
- `⚠️ Platform Strategy — INFERRED, NOT OBSERVED` blockquote rendered correctly — 3.1 Pro followed the instruction exactly; 2.5 Flash had been rendering a plain italic note instead (SYSTEMIC-02 now working with 3.1 Pro).
- Writing formulas are more specific: numbered 4-step formulas with concrete copy instructions vs. Flash's vaguer pattern descriptions.
- Instruction fidelity higher overall: `[NO DATA]` Webinar section, specific gap bullets, observed-vs-inferred labeling all followed correctly.
- Competitive Landscape empty this run — market_context Pass 6b returned no useful results (unrelated to model; Google Search grounding can be inconsistent).
- Intel signal baseline guard fired correctly (0-day baseline → "Baseline comparison invalid" message).
- Verdict: **measurably better for Brand DNA generation**. Worth using for Phase 3 (generator) and delta at minimum. Analysis passes (taxonomy, classification) don't need Pro quality.

**Output:** `outputs/thoughtspot-brand-dna-2026-05-27.md`
**Traceback notes:** Model is `models/gemini-3.1-pro-preview` — preview tier, pricing not published. Test via `GEMINI_MODEL=gemini-3.1-pro-preview python regen_dna.py` (no scraping cost) before committing to full-pipeline upgrade.

---

## 2026-05-27 — Fix KeyError in brand DNA generator prompt template

**What changed:** `generator/markdown.py` — escaped `{N}` → `{{N}}` in the `_PROMPT` string.
**Why:** SYSTEMIC-01/03 audit fix added `{N}` as a literal placeholder for Gemini to fill in, but Python's `.format()` treated it as a format key, raising `KeyError: 'N'`. This was silently killing every Phase 3 brand DNA generation since the audit fixes were applied.
**How it helps:** Brand DNA generation now completes instead of crashing.
**Traceback notes:** Any `{...}` in `_PROMPT` that isn't a named format arg must be doubled (`{{...}}`). Check for others if adding new literal-brace instructions.

---

## 2026-05-27 — BRAND-01: move ownership filter to after vision extraction

**What changed:** `main.py` — Phase 1.1 (ownership filter) moved to after Phase 1.5 (vision extraction), renamed Phase 1.6.
**Why:** Escape room ads scraped from Google were passing the ownership filter because they had blank copy at filter time (image-only ads). Vision extraction then populated "Locked In Escape Middleburg FL" as the headline — but the filter had already run. By running the filter after vision, Gemini sees the actual extracted ad copy and correctly rejects non-brand-affiliated ads.
**How it helps:** Eliminates off-brand image content (escape rooms, unrelated Google Display Network ads) from sample_ads and analysis input.
**Traceback notes:** The root cause is Google's Transparency Center associating Display Network ads with an advertiser's account even when the creative isn't brand-specific. The fix is at the pipeline level, not the scraper level.

---

## 2026-05-27 — Audit fixes: 9 of 10 issues from cross-brand QA audit

**What changed:**

_SYSTEMIC-05_ — `generator/delta.py`: added `_days_between()` and a pre-flight guard at the top of `generate()`. If current and baseline dates are fewer than 7 days apart, returns a single-paragraph "Baseline comparison invalid" block instead of running the full delta analysis. Eliminates same-day noise signals (Rippling and ThoughtSpot were comparing same-day runs).

_BRAND-01 (scraper)_ — `scrapers/google.py`: when `target_advertiser_id` is not captured (ATC navigation didn't land on an advertiser page), now falls back to filtering by proto field "12" (advertiser_name). Prevents escape room ads from passing through when Rippling's ATC page doesn't return a stable advertiser_id.

_BRAND-01 (prompt)_ — `generator/markdown.py`: added rule (5) to verbatim headline filter: "skip any headline that does not mention the brand name, a known product name, or a domain — headlines about unrelated businesses, escape rooms, tourism, or other clearly off-brand topics must be excluded even if they appear in the sample data."

_SYSTEMIC-01_ — `generator/markdown.py`: Form/Lead Gen, Engagement/Brand, and Webinar/Event ad type sections now have tiered confidence thresholds (5+ examples = full formula, 2–4 = LOW CONFIDENCE block, 0–1 = INSUFFICIENT DATA block, 0 for Webinar = NO DATA block). MOFU CTA fallback block added to Form/Lead Gen writing instructions.

_SYSTEMIC-02_ — `generator/markdown.py`: inferred platform strategy section now renders as a `>` blockquote with `⚠️ Platform Strategy — INFERRED, NOT OBSERVED` heading instead of a single italic disclaimer that blended with observed prose.

_SYSTEMIC-03_ — same as SYSTEMIC-01 (confidence thresholds cover this).

_SYSTEMIC-04_ — `generator/markdown.py`: each whitespace bullet now has a trailing caveat: "_(not cross-referenced against {advertiser}'s own recent product releases — verify before treating as open opportunity)_". Real fix (product launch web search) deferred.

_SYSTEMIC-06_ — `generator/markdown.py`: added "Guardrail validation" rule in Synthetic Ad Templates section instructing Gemini to silently check each template against the "What they never say" list and rewrite violations before outputting.

_BRAND-02_ — `generator/delta.py`: added evidence rule to the prompt: angle-change signals must point to a diff field, not infer from untracked data. "Do not say 'ads now explicitly mention X' unless explicit_mentions or a verbatim phrase appears in the diff data."

_BRAND-04_ — `generator/markdown.py` + `main.py`: `generate()` now accepts `prev_analysis` param. When current CTA set collapses to ≤2 items, pulls CTAs from previous voice pass and appends them as `_(previously observed)_` options. Prevents Anthropic-style CTA collapse from stripping the agent of a usable range.

**Not implemented:** BRAND-03 (OpenAI stale product landscape) — requires curating fetch sources per-advertiser; deferred to next research refresh.

**Why:** Cross-brand audit across ThoughtSpot, Anthropic, OpenAI, Rippling identified 10 issues (6 systemic, 4 brand-specific) that would cause campaign agents to misread inferred data as observed, generate off-brand templates, or act on same-day noise signals.

**Traceback notes:** SYSTEMIC-05 uses a 7-day minimum — adjust `_MIN_BASELINE_DAYS` in `delta.py` if pipeline cadence changes. BRAND-04 CTA carryforward only activates when ≤2 CTAs are present; it labels historical items `_(previously observed)_` so the agent can weight them lower. The guardrail validation (SYSTEMIC-06) is prompt-based self-correction, not a deterministic pass — it reduces violations but doesn't eliminate them.

---

## 2026-05-27 — Delta signal report (competitive intelligence diff across runs)

**What changed:**
- `generator/delta.py` — new module. `compute_diffs()` programmatically diffs two analysis snapshots across: ad type mix, funnel distribution, angle frequencies, CTAs, signature phrases, tone descriptors, visual consistency score, competitors mentioned, saturated strategies, whitespace. `generate()` passes the structured diffs + condensed voice/structure summaries to Gemini (temp 0.2) which writes a signal report with: What Changed (per-signal observations + implications), What Held Constant, Watch List, Recommended Action.
- `storage/db.py` — added `get_latest_analysis()`: returns `(analysis_by_pass, run_date, ad_count)` for the most recent run per advertiser. Uses MAX(id) GROUP BY pass_name — call BEFORE saving the new analysis to get the prior snapshot.
- `main.py` — fetches previous analysis before saving new one; now saves `type_distribution` to `analysis_results` (was previously skipped); calls `delta.generate()` after brand DNA write if a previous run exists; writes delta to `outputs/{slug}-intel-signal-{date}.md`.

**Why:** ABM frameworks (per Growth Unhinged article) route actionable signals, not documents. The Brand DNA is the "read this for context" artifact; the delta is the event that triggers sales/marketing action. A "BOFU surge" or "new positioning phrase" is a CRM-routable signal — a static Brand DNA update is not.

**How it helps:** Running ThoughtSpot produced a real delta: detected "Dashboards Are Dead. Try AI." emerging as a new signature, pain-point angle surging from 5% → 38%, BOFU from 5% → 28%, visual consistency from 0.4 → 0.8, and new competitors (Improvado, Looker) entering their mental model. Output: `outputs/thoughtspot-intel-signal-2026-05-27.md`.

**Traceback notes:** First run after deploy shows `prev_ads: 0` in the header — expected, because the previous run used the old code that skipped saving `type_distribution`. Fixed: new code saves it, so subsequent deltas will have accurate counts. The funnel/angle diffs themselves are accurate regardless (they come from the `funnel` and `angles` pass rows, which were always saved).

---

## 2026-05-26 — Fix P2-6 Meta noise filter + P2-7 CTA composite string

**What changed:**
- `scrapers/meta.py` — two changes to `_parse_card_text()` (DOM fallback path):
  1. **Page-name filter**: extracts the page name from the line immediately before "Sponsored" in the card DOM (verified by inspecting live Meta ad library cards — page name is at line ~11, after UI chrome). If the page name doesn't contain the advertiser name, the entire card is dropped. This eliminates keyword-search noise (escape room ads, China tourism, romance fiction that mention "rippling" as an adjective).
  2. **DOM noise strings added to skip set**: "this ad has multiple versions", "ads use this creative", "see summary details" added. Date range pattern (`\d{1,2} Mon YYYY - \d{1,2} Mon YYYY`) added as a regex filter.
- `scrapers/meta.py` — `_walk_json()`: added `page.name` check for the GraphQL JSON path (handles the case when XHR interception does return results).
- `generator/markdown.py` — CTA rules sentence: added "If the ALLOWED list contains multiple variants of the same CTA (e.g. multiple demo CTAs), pick exactly one — do not combine them with slashes or commas."

**Why:** P2-6 (Meta keyword noise) and P2-7 (synthetic CTA composite string) remained open after 2026-05-26 demo session. Investigation revealed the XHR interception was returning 0 for Rippling, so all 37 ads came from the DOM fallback, which had no page-name filter. The GraphQL path fix was insufficient — the DOM path needed the same treatment.

**Traceback notes:** Verified by running against Rippling: 37 → 7 ads after filter (all 7 are real Rippling HR ads). Ad type distribution changed from noise-dominated "webinar_event: 14, unknown: 2" to correct "engagement_brand: 4, form_lead_gen: 3". The page name heuristic (line before "Sponsored") was confirmed by inspecting live Meta ad library DOM — structure: [zero-width spaces, Inactive, Library ID, date, Platforms, EU transparency, Open Drop-down, See ad details, **PAGE NAME**, Sponsored, ad copy]. If XHR interception succeeds in future runs, the GraphQL path filter also fires as a secondary check.

---

## 2026-05-26 — Competitive Landscape section with Google Search grounding

**What changed:**
- `scrapers/web_fetch.py` — new module (DDG-based, currently unused — kept for homepage fetch utility)
- `analysis/agent.py` — added `MARKET_CONTEXT_PROMPT` and `_pass_market_context()`. New pass runs concurrently with passes 1-4 using Gemini's `google_search` grounding tool. Auto-discovers competitors via live Google Search — no `--competitors` flag required. `--competitors` still works as an explicit override. Grounding is incompatible with `response_mime_type=application/json`, so this pass parses JSON from free-form text via regex fallback.
- `generator/markdown.py` — added `## Competitive Landscape` section between Positioning Map and Synthetic Ad Templates. Renders: market_summary, per-competitor known_for/ad_angle/positioning, saturated strategies, whitespace, strategic implications.

**Why:** Demo feedback requested competitive context in the Brand DNA. Web scraping (DDG, Bing, G2) proved unreliable due to bot detection. Gemini grounding uses Google Search under the hood — no API key or scraping needed.

**Traceback notes:** Grounding requires `genai_types.Tool(google_search=genai_types.GoogleSearch())` and does NOT work with `response_mime_type="application/json"`. JSON is parsed from response text via `re.search(r'(\{[\s\S]*\})', raw)`. If grounding fails (timeout or API error), pass returns `{}` and the section renders as "unavailable" — pipeline continues.

---

## 2026-05-26 — Ran all 4 advertisers with meta+google+linkedin (p2/p14 runs)

**What changed:** No code changes. Pipeline runs only.

**Advertisers run:** Rippling (p2), OpenAI (p2), ThoughtSpot (p14), Anthropic (p2) — all with `--platforms meta google linkedin`.

**Scrape results:**
| Advertiser | Google | LinkedIn | Meta | Total in DB |
|---|---|---|---|---|
| Rippling | 3 | 35 | 47 | 85 |
| OpenAI | 60 | 0 | 39 | 99 |
| ThoughtSpot | 60 | 50 | 49 | 159 |
| Anthropic | 60 | 50 | 45 | 155 |

**What Meta added per advertiser:**

*Rippling:* New CTAs "Try Rippling Today!" and "See how it works in a short demo" (including £100 gift card offer). New visual direction: earthy greens/browns/mustard yellow (not the dark purple from LinkedIn). Full-funnel confirmed with urgency/scarcity and social_proof angles. BOFU went from 4 to 6. Note: Meta keyword search pulled in noise (escape room ad — "Locked In Escape Middleburg FL"). Signal quality issue — see below.

*OpenAI:* Massive shift in brand picture. Google-only showed pure BOFU (TOFU: 0, MOFU: 0, BOFU: 25). Meta revealed full-funnel: thought leadership, founder stories ("A Minute For Kindness" narrative campaign), natural landscape photography (icebergs/ocean/rocky shore), social proof, customer quotes. Low visual consistency (0.4) — heavy format experimentation on Meta. OpenAI is running two completely different creative vocabularies by platform.

*ThoughtSpot:* First run with Meta data. 164 ads analyzed. Revealed: customer testimonial narrative content (Charlotte Miller Airbnb stories — but likely cross-contamination noise), Cisco Live / WordPress event broadcasts, competitive displacement ads ("ThoughtSpot vs Legacy BI"), highly balanced funnel (TOFU: 13, MOFU: 14, BOFU: 11). Visual consistency 0.3 = most experimental brand of the four.

*Anthropic:* Previous google+linkedin showed zero BOFU. Meta revealed 13 BOFU ads. New CTAs: "Talk with Claude", "Build, debug, and ship", "Get Smarter on AI (For Free)", "Join [X] readers". New products visible: Claude Code, Claude Pro, Claude Cowork, TLDR newsletter partner ads, Canva Create event ad. Intriguing hook pattern on Meta: "AI won't replace you, but a person using AI will." Headline formula shift toward empowerment/urgency on Meta vs. clinical feature-focus on Google.

**Signal quality issue identified:** Meta Playwright scraper does keyword search, so results include ads from other advertisers whose copy contains the search term. Rippling results included an escape room ad; ThoughtSpot results included Charlotte Miller Airbnb testimonials. This is NOT a bug — it mirrors how the public Ad Library works. Graph API search_terms parameter has the same behavior. Fix options: (1) post-filter by page_name/advertiser_name field in scraped JSON, (2) use page_id instead of search_terms in Graph API. Not yet fixed.

**Why:** Testing Meta platform layer to assess what it adds above Google+LinkedIn baseline. Answer: significant — different funnel stages, different creative vocabulary, different visual styles.

**Traceback notes:** LinkedIn returned 0 for OpenAI — either not using LinkedIn or brand name didn't match. Rippling Google only got 3 ads (generic match issue — low signal from Google for this advertiser, see prior history). Meta is now the primary data source for Rippling specifically.

---

## 2026-05-26 — Raise analysis cap 25→40, generator sample 20→30

**What changed:**
- `analysis/agent.py` — all 5 `][:25]` slices raised to `][:40]` (classifier, voice, angles, funnel, visual, structure passes). Synthetic pass `[:10]` and vision image fetch `[:10]` left unchanged.
- `generator/markdown.py` — `slimmed` sample raised from `[:20]` to `[:30]`

**Why:** More copy-rich ads per pass sharpens voice fingerprint, angle distribution, and funnel ratios for larger advertisers (HubSpot, Salesforce have 100+ ads in DB). Generator gets 10 more real examples to ground the Brand DNA document.
**Traceback notes:** The meaningful quality ceiling is platform diversity (Meta pending, LinkedIn no image auth), not sample size. This is a marginal improvement, not a breakthrough.

---

## 2026-05-26 — Rewrite meta.py: Playwright scraper as primary fallback

**What changed:** `scrapers/meta.py` — full rewrite. Removed non-functional `_library_api` and `_web_scrape` strategies (both require Ad Library API permission). New `_playwright_scrape()` as primary fallback after Graph API:
- Navigates to `facebook.com/ads/library/?country=GB&q={advertiser}`
- Intercepts GraphQL responses at `facebook.com/api/graphql/` and `ads_archive` endpoints
- Handles both API format (`ad_creative_bodies`) and web UI GraphQL format (`ad_creative_body`)
- Handles streaming/newline-delimited JSON responses
- Dismisses cookie consent dialogs
- Scrolls to load more ads
- DOM fallback: uses `Library ID:` as stable anchor, walks up DOM tree to card container, extracts text and images via `page.evaluate()`
- Added `_normalize_web()` for web UI GraphQL format; renamed `_normalize()` to `_normalize_api()`

**Tested:** HubSpot meta-only run returned 40 scraped → 33 owned after ownership filter. All 7 analysis passes + Brand DNA generation succeeded.
**Why:** Meta Graph API approval pending. Playwright scrapes the same public ad library the Graph API reads from.
**Traceback notes:** DOM fallback triggers only when XHR interception yields nothing. Cookie consent dismissal tries 4 selectors. `country=GB` used — change to `US` for US-only advertisers.

---

## 2026-05-26 — p11 full pipeline run + QA

**What changed:** Full pipeline run (fresh scrape + vision + all 7 analysis passes + generation) for HubSpot, Descope, ThoughtSpot, Salesforce.

**p11 QA scores:**

| Document | Language | Visual | Alignment |
|---|---|---|---|
| HubSpot | B | B | A |
| Descope | B | A | A |
| ThoughtSpot | B | A | A |
| Salesforce | B | B | A |

**What improved:**
- Synthetic CTA compliance: all 4 advertisers fully compliant — every CTA from ALLOWED list ✅
- Ad type body copy: plain text confirmed across all 4 ✅
- `[CTA not captured]` label working (ThoughtSpot Form/Lead Gen) ✅
- LinkedIn attribution stale artifacts cleared for ThoughtSpot and Salesforce ✅
- Platform disclaimer in all 4 ✅

**New bug found — Google responsive display attribution bleed (P2-5):**
`scrapers/google.py` `_extract_responsive_copy()` extracts text from rendered ad preview JS. The string "Sponsored. descope.com. www.descope.com/." passes the candidate filter (starts with capital, contains space, no JS keywords) and ends up in `primary_text`. Confirmed via DB: Google ad CR00623979497372254209 for Descope. The LinkedIn scraper fix doesn't cover this path. Fix: add `www.` and `Sponsored` domain pattern to candidate filter in `_extract_responsive_copy`.

**Remaining Language B ceiling:** Testimonials in headline field (Salesforce: "See how Slack has reinvented work chat"), truncated headlines ("Passwordless au" — still in Descope headline from a different scrape). Not fixable without Meta platform data or scraper-level validation.

---

## 2026-05-26 — p10 regen run + QA

**What changed:** No new code — regen_dna.py run to validate p10 generator fixes (ad type body format + `[CTA not captured]` label).

**p10 QA scores:**

| Document | Language | Visual | Alignment |
|---|---|---|---|
| HubSpot | B | B | A |
| Descope | B | A | A |
| ThoughtSpot | B | A | A |
| Salesforce | B | B | A |

**What improved:** `[CTA not captured]` label working in HubSpot Form/Lead Gen. Ad type body copy plain text (not blockquotes) confirmed across all 4. HubSpot and ThoughtSpot synthetic CTAs fully ALLOWED-list compliant.

**Remaining issues:**
- Salesforce Template 1: `` `Get Started for Free` `` — not in ALLOWED list (`30-Day Free Trial`, `Get 50% off`, `No CTA`). ALLOWED list sparse because voice analysis only captured 2 real CTAs. Gemini overrides constraint when list looks thin.
- Descope Template 3: `` `Auth in a few lines of code` `` — tagline used as CTA. Writing formula in Ad Type section also suggests it as valid CTA, which compounds the problem.
- Stale DB artifacts still visible (ThoughtSpot/Descope): LinkedIn attribution lines and truncated headlines from before the scraper fixes. Will clear on next full pipeline run.

**Next:** Full pipeline run to pick up P3-1 vision fix and clear stale DB data.

---

## 2026-05-26 — Fix P2-1, P3-1, P3-3 (CTA constraint, vision sitelink truncation, brand_dna retention)

**What changed:**
- `generator/markdown.py` `_PROMPT` — CTA rules line in Synthetic Ad Templates now ends with explicit negative constraint: "Do not use any CTA string not present in the ALLOWED list above, even if similar text appears elsewhere in the data or prompt."
- `scrapers/vision_extractor.py` `_VISION_PROMPT` — CTA field description changed from `"button text like Get started or Learn more or empty string"` to `"single CTA button text only — one short phrase like 'Get started' or 'Learn more', NOT a list of sitelinks — or empty string if no button"`. This stops Gemini vision from returning comma-separated sitelink lists (e.g. "HubSpot Pricing, HubSpot Smart CRM, Get a Fr") as the CTA value.
- `storage/db.py` `save_brand_dna()` — `DELETE FROM brand_dna WHERE advertiser = ?` added before INSERT. Table now keeps exactly 1 row per advertiser instead of accumulating unboundedly.

**Why:** P2-1: Gemini was ignoring the ALLOWED CTAs constraint and pulling CTA text from headline examples elsewhere in the prompt. P3-1: Vision extractor's CTA prompt was ambiguous — Gemini was treating visible sitelink text as the CTA field and truncating mid-word. P3-3: brand_dna table had 8+ rows per advertiser from repeated pipeline runs with no cleanup.

**How it helps:** Synthetic CTAs will be strictly from the injected ALLOWED list. Vision-extracted CTA fields will be single button phrases (or empty), not sitelink blobs. brand_dna table stays clean.

**Traceback notes:** P3-2 (funnel instability) was investigated and is already correct — pipeline uses in-memory analysis during a full run; regen_dna.py uses MAX(id) GROUP BY pass_name. The instability is natural variance across different scrape datasets, not a stale-read bug. No code change needed.

---

## 2026-05-22 — Created regen_dna.py: regenerate Brand DNA without re-scraping

**What changed:** New file `regen_dna.py` created in `intelligence-agent/` root. Reads latest analysis pass results from DB (MAX(id) per pass_name per advertiser), loads ads from `ads` table, calls `md_generator.generate()` for all 4 advertisers concurrently via `asyncio.gather`.
**Why:** `main.py` has no `--skip-scrape` flag — every run scrapes live, re-runs all 7 analysis passes, and regenerates. When only the generator prompt changed, re-running the full pipeline wastes 3–5 minutes of scraping and Gemini API calls for analysis passes that haven't changed. `regen_dna.py` loads existing DB data and runs only Phase 3 (generation), taking ~30 seconds total.
**How it helps:** Fast iteration on generator prompt changes — fix prompt, run `python regen_dna.py`, QA the 4 outputs in seconds instead of minutes.
**Traceback notes:** This is a one-shot utility script — not wired into `main.py`. Run it from `intelligence-agent/` with `.venv` active: `python regen_dna.py`. If analysis data is missing for an advertiser, it prints a skip message and continues. Uses `MAX(id) GROUP BY pass_name` to get the latest result for each pass (same advertiser may have multiple rows per pass from earlier runs).

---

## 2026-05-22 — Fix observed example ad format: plain text body, [CTA not captured] label

**What changed:** generator/markdown.py _PROMPT — Ad Type sections now explicitly specify observed example ad format: body copy as plain text (not blockquotes), CTA in backticks, and `[CTA not captured]` label when CTA field is empty
**Why:** p9 QA found ThoughtSpot/HubSpot observed example ads using `> ` blockquotes for body copy (wrong — blockquotes reserved for verbatim headlines); `No CTA` appearing on Form/Lead Gen examples with no explanation (confusing — looks like a valid pattern when it's actually a data capture failure)
**How it helps:** Ad type sections will use consistent plain-text body format; missing CTAs will be clearly labeled as data gaps rather than valid CTA patterns
**Traceback notes:** This is a generator format fix only — the underlying data quality issues (truncated headlines, testimonials in scraped headline fields) remain and represent the ceiling for Language A until scraper quality improves

---

## 2026-05-22 — p8 + p9 pipeline runs + QA (CTA key fix, No CTA/type rules, verbatim list hygiene)

**What changed:** No new code — pipeline reruns and QA documenting p8 and p9 results.

**p8 QA scores (after cta_patterns key fix):**

| Document | Language | Visual | Alignment |
|---|---|---|---|
| HubSpot | B | B | A |
| Descope | B | B | A |
| ThoughtSpot | B | A | A |
| Salesforce | B | B | A |

**p9 QA scores (after verbatim list hygiene + CTA by-type rules):**

| Document | Language | Visual | Alignment |
|---|---|---|---|
| HubSpot | B | B | A |
| Descope | B | A | A |
| ThoughtSpot | B | A | A |
| Salesforce | B | B | A |

**What's working:** Alignment A across all 4. Visual A for ThoughtSpot and Descope. All synthetic template CTAs from ALLOWED list. Sub-brand notes in Salesforce formula. No bare brand name entries. No third-party pollution.

**Language B ceiling diagnosis:** Remaining B issues are upstream data quality limitations, not generator prompt compliance failures:
- Salesforce: "With Slack, we know our data is always secure." stored in scraped headline field — appears in verbatim list despite "headlines only" instruction
- Descope: "Passwordless au" truncated scrape artifact in headline field
- HubSpot/ThoughtSpot: `No CTA` on Form/Lead Gen observed examples — real data gap (CTA not scraped), now labeled `[CTA not captured]` in p10
- ThoughtSpot: body copy used `> ` blockquotes in ad type sections — fixed in generator prompt for p10

**Language A requires:** Better upstream data — Meta platform (pending approval), LinkedIn image fetch auth, or stricter headline field validation at scrape time.

---

## 2026-05-22 — Generator prompt: verbatim list hygiene + body blockquote + CTA by type

**What changed:** generator/markdown.py _PROMPT — (1) Verbatim headlines instruction now has 4 explicit rules: headlines only (no testimonials/body copy), no duplicates, nothing before/after blockquote, skip bare brand names; (2) Synthetic template CTA rules now specify form_lead_gen/webinar_event must use a conversion CTA (never No CTA), engagement_brand uses No CTA only when observed sample ads show blank CTAs; (3) Body blockquote rule added: no signature phrases, slogans, or CTA text inside the > body
**Why:** p8 QA found: ThoughtSpot duplicate verbatim headline ("Future-Proof Your AI Strategy" ×2); Salesforce customer testimonial in verbatim list ("With Slack, we know our data is always secure."); Descope Template 3 body included "Auth in a few lines of code." inside > blockquote; Salesforce Template 2 (Form/Lead Gen) used No CTA instead of 30-Day Free Trial
**How it helps:** Language pillar should reach A — all remaining B-blockers are generator prompt compliance issues now explicitly addressed
**Traceback notes:** CTA by-type rule is the critical addition — previous instruction didn't distinguish between ad types, letting Gemini use No CTA universally when unsure

---

## 2026-05-22 — Fix CTA key mismatch: try cta_patterns fallback in generator

**What changed:** generator/markdown.py generate() — CTA extraction now tries `cta_constructions`, then `cta_patterns`, then `ctas` before defaulting to empty list
**Why:** p7 QA found ThoughtSpot and Salesforce ALLOWED CTAs blocks showed only `No CTA` — DB inspection confirmed the voice pass stores CTAs under `cta_patterns` (matching VOICE_PROMPT schema), but the generator was only checking `cta_constructions`. HubSpot/Descope happened to use `cta_constructions` in some runs, masking the bug
**How it helps:** All advertisers' observed CTAs now correctly injected into the ALLOWED list at generation time, regardless of which key name Gemini chose
**Traceback notes:** VOICE_PROMPT in analysis/agent.py explicitly uses `cta_patterns` as the schema key (line 68). The fallback chain handles any key name drift across Gemini versions

---

## 2026-05-22 — Add No CTA to ALLOWED list + blank-CTA-type instruction

**What changed:** generator/markdown.py generate() — `No CTA` always appended to cta_items list so it's always in the ALLOWED CTAs injected into the prompt; _PROMPT Synthetic Ad Templates — replaced "If ALLOWED CTAs list is empty, write No CTA" with "If the sample ads for a given ad type show blank or absent CTAs, use No CTA — do not substitute a CTA from the ALLOWED list"
**Why:** p6 QA found Descope Template 2 used "Learn more" (not in ALLOWED list) and ThoughtSpot Templates 1+3 used "Learn more"/"Download" for engagement/brand type where observed CTAs were blank — Gemini had no valid fallback so invented one
**How it helps:** Gemini now has an explicit valid option (No CTA) when the ad type doesn't use CTAs; the per-type instruction tells it to check the sample ads before picking from the ALLOWED list
**Traceback notes:** No CTA is always in the ALLOWED list now regardless of what cta_constructions contains — this is intentional since any ad type may have templates with no CTA

---

## 2026-05-22 — p6 pipeline run + QA (post CTA injection fix)

**What changed:** No new code — pipeline rerun and QA documenting p6 results.

**QA scores:**

| Document | Language | Visual | Alignment |
|---|---|---|---|
| HubSpot | B | A | A |
| Descope | B | B | A |
| ThoughtSpot | B | A | A |
| Salesforce | B | B | B |

**What improved vs p5:** Bare brand name entries gone from all docs. Visual A for HubSpot and ThoughtSpot. Alignment A for HubSpot, Descope, ThoughtSpot. CTA injection working — HubSpot Templates 1+3 correctly on-list.

**Remaining Language B blockers:** "Learn more" still appearing in engagement/brand templates with blank observed CTAs (Descope Template 2, ThoughtSpot Templates 1+3). Root cause: global cta_constructions list includes CTAs from all ad types; Gemini applies them to brand templates where observed CTAs are blank. Fix: always include No CTA in ALLOWED list + per-type instruction to use No CTA when sample ads show blank CTAs.

---

## 2026-05-22 — Inject observed CTA list directly into synthetic template instruction

**What changed:** generator/markdown.py — (1) `generate()` extracts `cta_constructions` from the voice analysis dict and formats them as a literal backtick-quoted list (`cta_list`); (2) `_PROMPT` Synthetic Ad Templates section now opens with "ALLOWED CTAs (use ONLY these exact strings, no others): {cta_list}" injected at the point of use, replacing the vague cross-reference to "the Voice Fingerprint section"
**Why:** p5 QA found HubSpot Template 2 used "Learn More", Salesforce Template 2 used "Get Started for Free", ThoughtSpot Template 1 used "Learn more" — none in their observed CTA lists. Root cause: the constraint ("use CTAs from Voice Fingerprint section") was 80+ lines away from where Gemini was writing templates — attention decay made it unreliable
**How it helps:** Gemini sees the exact allowed CTA strings at the moment of writing each template — no cross-referencing, no memory required. Converts a "follow a rule about another section" problem into a direct lookup
**Traceback notes:** `cta_constructions` key in voice dict is a list of strings. Handles edge cases: empty list → "none observed"; non-list value → str() fallback. If voice pass fails entirely and returns {}, cta_list will be "none observed" and templates will write `No CTA` per the fallback instruction

---

## 2026-05-22 — Fix generator prompt: skip bare brand name headlines, remove CTA-after-blockquote

**What changed:** generator/markdown.py _PROMPT — verbatim headlines instruction now (1) explicitly says nothing before or after the blockquote line, removing the "add CTA in italics after blockquote" permission that caused Gemini to append CTAs to every headline; (2) adds instruction to skip any ad where headline is just the advertiser brand name alone
**Why:** p5 QA found: our CTA-after-blockquote fix overcorrected — Gemini appended italic CTAs after every verbatim headline (Descope ×9 affected lines); bare "HubSpot" and "Descope" ×7 entries still appearing despite main.py filter (those ads have real primary_text so pass the filter, but their empty headlines shouldn't appear in the verbatim list)
**How it helps:** Verbatim headlines section will only show meaningful ad headlines; no CTA noise around blockquotes
**Traceback notes:** Bare brand name ads are still included in sample_ads for their body copy signal — we only suppress their bare headline from the verbatim list, not the ad itself. The generator prompt skip instruction is the right layer since the filter can't distinguish "include body copy but not headline" at the data level

---

## 2026-05-22 — p5 pipeline run + QA (post generator prompt + sub-brand + bare name fixes)

**What changed:** No new code — pipeline rerun and QA pass documenting p5 results.

**QA scores:**

| Document | Language | Visual | Alignment |
|---|---|---|---|
| HubSpot | C | B | B |
| Descope | B | A | A |
| ThoughtSpot | B | A | B |
| Salesforce | B | A | B+ |

**What improved vs p4:** Visual jumped to A for Descope/ThoughtSpot/Salesforce. Sub-brand note working in Salesforce ("frequency driven primarily by Slack ads"). Descope template blockquotes fixed. ThoughtSpot "AI brain graphic" hallucination gone. No third-party brands in any verbatim list.

**Persistent/new issues:**
- Bare brand name entries (HubSpot ×7, Descope ×7) still present — filter passes them because they have real primary_text; fix moved to generator prompt (skip bare-name headlines in verbatim list)
- CTA-after-blockquote overcorrection: Gemini appended italic CTAs after every verbatim headline — removed that permission from generator prompt
- HubSpot Template 2 CTA "Learn More" invented (not in observed list)
- Salesforce Template 2 "Get Started for Free" used as CTA but not in CTA constructions list
- ThoughtSpot Template 1 "Learn more" applied to engagement_brand type where observed CTAs were blank

**Traceback notes:** The two remaining synthetic CTA mismatches may require tightening the generator prompt to be more explicit: "use ONLY the CTAs listed in the CTA constructions bullet above" rather than the broader "CTA list in Voice Fingerprint section."

---

## 2026-05-22 — Fix generator prompt: blockquotes, CTA noise, visual grounding, sub-brand notes

**What changed:** generator/markdown.py _PROMPT — (1) verbatim headline blockquote instruction now says include ONLY headline text, not sitelinks/CTA suffixes; (2) Synthetic Ad Templates section now has explicit blockquote format example with "> " prefix; (3) synthetic visual descriptions constrained to only observed Visual Patterns data; (4) synthetic CTAs constrained to only observed CTAs from Voice Fingerprint; (5) headline formula frequency instruction now asks to flag sub-brand concentration
**Why:** p4 QA found: ThoughtSpot verbatim headlines had CTA text embedded in blockquotes; Descope synthetic template bodies were plain prose not blockquotes; ThoughtSpot Template 1 hallucinated "AI brain graphic" not in data; Descope Template 3 used unobserved CTA "Sign Up Free"; Salesforce "[Product] - Official Site" formula inflated by Slack sub-brand ads to false ~28%
**How it helps:** Brand DNA documents will have clean verbatim headline lists, correctly formatted synthetic templates, no hallucinated visual claims in templates, no invented CTAs, and explicit sub-brand attribution in formula frequency
**Traceback notes:** All prompt-only changes — no parsing or data changes. Generator uses temperature=0.3 so compliance is high but not guaranteed; QA should verify on next pipeline run

---

## 2026-05-22 — Add sub-brand detection to voice pass headline formula analysis

**What changed:** analysis/agent.py VOICE_PROMPT — headline_formulas schema now includes optional sub_brand_note field; Gemini is instructed to flag when a formula's frequency is primarily driven by a sub-brand product (e.g. Slack ads within Salesforce dataset)
**Why:** p4 QA found Salesforce "[Product] - Official Site" formula at ~28% frequency was almost entirely Slack ads — misleading signal about Salesforce's core brand headline strategy
**How it helps:** Voice Fingerprint will surface sub-brand concentration explicitly so copywriters know which formulas reflect the core brand vs sub-brand creative
**Traceback notes:** sub_brand_note is optional — Gemini should omit it when no sub-brand concentration is detected. Downstream generator prompt (markdown.py) also updated to surface this note in the headline formula list

---

## 2026-05-22 — Filter bare brand name entries from sample_ads

**What changed:** main.py — sample_ads construction list comprehension now drops ads where headline == advertiser name (case-insensitive) AND primary_text has no real copy
**Why:** p4 QA found 7 "HubSpot" entries in the HubSpot Brand DNA verbatim headline list — LinkedIn image-only ads where no headline was captured; scraper leaves headline empty or it defaults to the advertiser name
**How it helps:** Verbatim headline list will only contain actual ad copy, not scraper fallback noise
**Traceback notes:** Filter only drops ads with bare brand name headline AND no real body copy — ads with a bare headline but meaningful primary_text are kept, since their body copy still contributes useful signal to the analysis

---

## 2026-05-22 — Second QA run (p4) after stale-row wipe + byline filter fixes

**What changed:** No code changes — QA-only run against 4 Brand DNA outputs (hubspot, descope, thoughtspot, salesforce, all dated 2026-05-22) produced by the p4 pipeline after stale-row wipe and byline filter fixes.

**QA scores (language structure / visual accuracy / alignment):**

| Document | Language | Visual | Alignment |
|---|---|---|---|
| HubSpot | B | C | B |
| Descope | B | B+ | A- |
| ThoughtSpot | B | C | B |
| Salesforce | B | B+ | A- |

**What improved vs prior QA:** Bylines gone. No Salesforce third-party ads ("Salesforce Ben", "Cloud Protection for Salesforce") in verbatim lists. Slack reduced to 1× and correctly attributed as owned sub-brand. Platform strategy disclaimer present in all 4 docs. 3 ranked headline formulas in all 4 docs.

**New critical issues surfaced:**
- HubSpot: 7 bare "HubSpot" entries in verbatim headlines — scraper noise from LinkedIn ads with no real headline captured
- Descope: Synthetic template body copy not in blockquotes (plain prose, not `> ` format); Template 3 CTA "Sign Up Free" is unobserved
- ThoughtSpot: CTA text embedded inside verbatim headline blockquotes (e.g. `> BI Solutions... *Pricing & Plans*`); Template 1 visual description hallucinates "AI brain graphic"
- Salesforce: "Slack - Official Site" ×3 in verbatim headlines; `[Product] - Official Site` formula inflated to ~28% by Slack ads — sub-brand bleed in formula frequency counts

**Traceback notes:** Bare "HubSpot" entries are LinkedIn ads where the scraper captures no headline (image-only ads where `image_url` is set but `img_alt` was also empty). These pass `_is_real_copy()` as empty strings... wait, actually they wouldn't pass `_is_real_copy()` — they must be coming from something else. Likely the advertiser name appears as headline fallback somewhere. Worth investigating before applying a fix. Blockquote formatting in synthetic templates is a generator prompt issue, not a data issue.

---

## 2026-05-22 — Filter thought-leadership bylines from sample_ads

**What changed:** main.py — added _is_byline() helper and filter in sample_ads construction; drops headlines matching "Firstname Lastname — Title" pattern (em dash, not hyphen)
**Why:** LinkedIn thought-leadership ads have author name+title as headline (e.g. "Kipp Bodnar — CMO at HubSpot"); these passed the ownership filter (company IS the advertiser) but leaked into Brand DNA verbatim headline list as fake ad creative signals
**How it helps:** Verbatim headline list in Brand DNA only contains actual ad copy, not author bylines
**Traceback notes:** Filter uses em dash (—) which ad copy does not use (ad copy uses hyphens). Applied only to sample_ads for the generator, not to client_ads used for analysis passes

## 2026-05-22 — Fix stale DB rows: full platform wipe before save_ads inserts

**What changed:** storage/db.py save_ads() — added DELETE FROM ads WHERE advertiser=? AND platform=? at the start of each save batch, before per-ad inserts
**Why:** save_ads() previously only deleted rows it was about to re-insert (by ad_id). Third-party ads filtered out by the new ownership filter were never re-scraped, so their old rows survived indefinitely in the DB
**How it helps:** Each scrape run produces a clean DB state for that advertiser+platform — no stale rows from previous runs
**Traceback notes:** Wipe is inside the same connection as inserts so it's atomic. All ads in a batch share the same advertiser+platform so the first ad's fields are used to build the DELETE predicate

---

## 2026-05-22 — Add hybrid ad ownership filter (landing page domain + Gemini fallback)

**What changed:** analysis/ad_filter.py — new module; filter_owned_ads() checks landing URL domain against _KNOWN_DOMAINS first, falls back to Gemini classification for ads without URLs; scrapers/linkedin.py — _parse_card() now extracts landing_url from CTA button; scrapers/google.py — _normalize() extracts landing_url from proto field "5"; main.py — filter_owned_ads() called on client_ads after scraping, before vision extraction
**Why:** String-matching aria-label company name missed cases like Slack ads (Salesforce-owned), thought-leadership bylines from employees, and any edge case where the company name doesn't exactly match. Landing page domain is a reliable signal; Gemini fallback handles ads without URLs
**How it helps:** Only ads that genuinely promote the target advertiser's products reach the analysis pipeline
**Traceback notes:** landing_url is in-memory only, not persisted to DB. Gemini fallback defaults to True (keep) on failure. filter_owned_ads wrapped in try/except so a filter crash never stops the pipeline

## 2026-05-22 — Fix LinkedIn scraper to filter third-party company ads

**What changed:** scrapers/linkedin.py _parse_card() — added early company name filter using the aria-label first segment; returns None for any card where the displayed company name doesn't match the target advertiser (case-insensitive)
**Why:** LinkedIn text search returns ads from companies that mention the advertiser name — consultants, publishers, third-party vendors (e.g. "Salesforce Ben", "Cloud Protection for Salesforce") were appearing in Salesforce Brand DNA
**How it helps:** Brand DNA will only reflect the target company's own ads, not third-party or partner accounts
**Traceback notes:** Filter uses aria-label split on comma — format is "{CompanyName}, {AdFormat}, View details". If aria-label is empty or malformed, company_name defaults to empty string which won't match any real advertiser, safely dropping the card

## 2026-05-22 — Fix VISUAL_PROMPT to force image-first observation

**What changed:** analysis/agent.py VISUAL_PROMPT — rewrote opening paragraph to tell Gemini it is seeing real ad images and must describe only what is visually observable, not infer from copy signals
**Why:** _pass_visual() was sending real image bytes via Part.from_bytes() but the prompt told Gemini to use "copy signals and metadata" — so Gemini anchored on text and produced copy-inferred visual descriptions (shields/locks for Descope) instead of describing actual pixel content
**How it helps:** Visual Patterns section will now reflect actual image content (workflow diagrams, brand colors, layout patterns) rather than security iconography implied by the ad copy
**Traceback notes:** Structured ad metadata (visual_description fields) is still included in the prompt for context, but the instruction now makes clear that image observation takes precedence

## 2026-05-22 — P2-2 + P2-3: Fix synthetic CTA types and angle double-counting

**What changed:** analysis/agent.py SYNTHETIC_PROMPT — added explicit CTA-by-type rule (awareness CTAs for engagement_brand, conversion CTAs for form_lead_gen, registration CTAs for webinar_event); ANGLE_PROMPT — added instruction to assign exactly one angle per ad (the dominant one)
**Why:** P2-2: synthetic engagement_brand templates were using BOFU CTAs (Start free trial) — ThoughtSpot templates 1+3 had wrong CTAs; P2-3: angle pass was double-counting angles per ad, inflating frequency counts (HubSpot showed 22 free_trial instances for 13 ads)
**How it helps:** Synthetic templates will have contextually appropriate CTAs; angle frequency counts will be accurate (one angle per ad)
**Traceback notes:** Both are prompt-only changes; no parsing code changes needed

## 2026-05-22 — P2-1: Label platform strategy as inferred, not observed

**What changed:** generator/markdown.py _PROMPT — Positioning Map section now instructs Gemini to prefix the platform strategy note with an italicized disclaimer that it's inferred from GTM priors, not observed platform-segmented data
**Why:** All four Brand DNA outputs had identical generic platform strategy boilerplate (Google for intent, LinkedIn for B2B) not grounded in actual per-platform creative analysis
**How it helps:** Readers see the disclaimer and know not to treat the platform strategy as data-backed; prevents acting on unreliable signal
**Traceback notes:** Full fix would require platform-segmented scraping and analysis; this is a trust label, not a data fix

## 2026-05-22 — P1-3: Extract up to 3 ranked headline formulas

**What changed:** analysis/agent.py VOICE_PROMPT — headline_formula (single string) replaced with headline_formulas (array of up to 3 objects: formula, frequency, example); generator/markdown.py _PROMPT — Voice Fingerprint section updated to render a ranked list
**Why:** Single headline formula had low match rates (HubSpot 36%, ThoughtSpot 45%) because brands run multiple headline structures; a ranked list is more accurate and actionable
**How it helps:** Brand DNA Voice Fingerprint now shows the top 3 headline patterns with frequency estimates, giving copywriters a realistic picture of the brand's headline variety
**Traceback notes:** No parsing code changed — _pass_voice() returns Gemini's raw JSON dict; the schema change flows through automatically. Downstream consumers of analysis["voice"]["headline_formulas"] should expect a list, not a string

## 2026-05-22 — P1-2: Strip LinkedIn attribution line from primary_text

**What changed:** scrapers/linkedin.py — _parse_card() now strips leading "Sponsored Advertiser www.domain.com" lines from primary_text after extraction
**Why:** LinkedIn ad cards include a platform attribution line that was being captured as body copy and appearing verbatim in Brand DNA documents
**How it helps:** primary_text now contains only actual ad copy, not scraper metadata
**Traceback notes:** Strip applies only to leading lines — if the pattern appears mid-copy it is left in place (shouldn't happen; attribution is always a header)

## 2026-05-22 — P1-1: Content-based dedup for sample_ads

**What changed:** main.py — sample_ads construction now deduplicates on (headline, primary_text[:100]) after sorting by impressions, keeping the highest-impression copy when the same creative runs under multiple ad_ids
**Why:** QA found 50% duplicate headline rate in Salesforce Brand DNA verbatim list — same creative text was appearing multiple times under different ad_ids
**How it helps:** Brand DNA verbatim headline list shows 20 distinct creatives, not the same ad repeated
**Traceback notes:** Dedup happens after impressions sort so highest-impression copy is always kept; seen-set is keyed on stripped content not ad_id

## 2026-05-22 — P0-2: Filter third-party ads from Google scraper

**What changed:** scrapers/google.py — scrape() now captures advertiserId from ATC URL after clicking the target advertiser, then filters out any creatives where proto field "1" doesn't match; _normalize() adds advertiser_id field to the returned dict
**Why:** Google scraper was pulling reseller/third-party ads that mention the target advertiser's name (e.g. "Cloud Protection for Salesforce" by WITH Secure appeared in Salesforce's scraped creative set)
**How it helps:** Brand DNA analysis will only reflect the target advertiser's own creative strategy, not competitor or reseller ads using their brand name
**Traceback notes:** Falls back to no-filtering if ATC URL doesn't contain advertiserId param (safe default). advertiserId param key may vary — checked both "advertiserId" and "advertiser_id" variants

## 2026-05-22 — P0-1: Fix visual pass to use real image bytes

**What changed:** analysis/agent.py — _pass_visual() now fetches image bytes for top 10 ads with image_url and passes them as genai_types.Part.from_bytes() in a multimodal Gemini call; falls back to text-only if no images fetchable
**Why:** Visual pattern analysis was inferring style from ad copy text, not actual images — QA found 3 of 4 Brand DNA outputs had wrong visual descriptions (HubSpot, Salesforce, Descope)
**How it helps:** Brand DNA "Visual Patterns" section now reflects actual image content — colors, layouts, visual style derived from real creative pixels
**Traceback notes:** Falls back to text-only path when image fetch fails; existing visual_description fields from vision_extractor still included in prompt for image-only ads without fetchable URLs

## 2026-05-22 — Parallel 4-advertiser run + 3-agent QA audit

**What changed:**
- `storage/db.py` — `get_connection()`: added `timeout=30` to `sqlite3.connect()` to prevent "database is locked" errors when running multiple pipeline instances in parallel
- `open-issues.md` — created new file documenting all known bugs and improvement backlog with fix guidance, file paths, and effort estimates
- `current-state.md` — full rewrite: added parallel run instructions, DB schema documentation, QA status table, known limitations per pass, updated last-runs table with Descope/Thoughtspot/Salesforce

**Why:** Ran the pipeline in parallel for HubSpot, Descope, Thoughtspot, and Salesforce (staggered 10s apart). Followed with a 3-agent QA: language structure, image analysis, and alignment analysis subagents running concurrently.

**How it helps:** Four Brand DNA outputs are now available for use. QA surfaced 12 issues (2 P0, 3 P1, 4 P2, 3 P3) with specific fix guidance. The critical finding: the visual pattern analysis section is wrong for 3 of 4 advertisers because it infers visual style from text ad copy rather than actual image pixels.

**Run results:**
| Advertiser | Ads scraped | All 7 passes | Output |
|---|---|---|---|
| HubSpot | 150 | ✓ | `outputs/hubspot-brand-dna-2026-05-22.md` |
| Descope | 148 | ✓ | `outputs/descope-brand-dna-2026-05-22.md` |
| Thoughtspot | 148 | ✓ | `outputs/thoughtspot-brand-dna-2026-05-22.md` |
| Salesforce | 150 | ✓ | `outputs/salesforce-brand-dna-2026-05-22.md` |

**QA findings summary:**
- **P0-1:** Visual pass (`_pass_visual` in `analysis/agent.py`) derives visual style from text — needs Gemini vision on actual image bytes
- **P0-2:** Google scraper pulling third-party/reseller ads under large advertiser names (Salesforce sample included a WITH Secure ad)
- **P1-1:** Brand DNA sample ads not deduplicated on content — Salesforce verbatim list had 50% duplicate headlines
- **P1-2:** LinkedIn scraper capturing "Sponsored [Name] www.domain.com" attribution line as body copy
- **P1-3:** Headline formula accuracy low (HubSpot 36%, ThoughtSpot 45%) — formula too prescriptive, brands use multiple structures
- **P2-1:** Platform strategy sections are generic boilerplate across all 4 docs — not grounded in platform-segmented data
- **P2-2:** ThoughtSpot synthetic `engagement_brand` templates use BOFU CTAs — should use TOFU CTAs
- **P2-3:** HubSpot "22 instances free_trial" count likely inflated from angles pass double-counting
- **P2-4:** Descope "SSO Documentation" CTA unverified in examples — possibly hallucinated
- **P3-1/2/3:** CTA truncation artifact, funnel instability display, brand_dna table unbounded growth

**Traceback notes:** SQLite timeout fix: `timeout=30` means each write will wait up to 30s before raising OperationalError — sufficient for the staggered parallel run cadence. The visual pass limitation is structural: `_pass_visual()` currently receives `image_url` strings in the ad dicts but never fetches them. Compare to `vision_extractor.py` which correctly uses `httpx`/`requests` + `genai_types.Part.from_bytes()`.

## 2026-05-22 — Autoplan QC: ad_type DB column, --scenario CLI flag, 7-pass alignment

**What changed:**
- `storage/db.py` — `init_db()`: added `ad_type TEXT DEFAULT NULL` column to `ads` CREATE TABLE schema
- `storage/db.py` — added `update_ad_type(ad_id, advertiser, ad_type)` function to persist classifier results
- `storage/db.py` — renamed `migrate_vision_columns()` to `migrate_columns(console=None)`; added `ad_type` column migration; added Rich console logging for migration events; added dedup DELETE for existing DBs
- `analysis/agent.py` — `_pass_synthetic()`: added `scenario: str | None = None` parameter; added curly-brace escaping so user-supplied `--scenario` text doesn't crash `SYNTHETIC_PROMPT.format()`; fixed pass label from "Pass 6/7" to "Pass 7/7"; added Gemini quota hint to timeout warning
- `analysis/agent.py` — `run_all_passes()`: added `scenario: str | None = None` parameter; added `"type_map"` key to return dict so main.py can persist per-ad classifications
- `main.py` — `run()`: added `scenario: str | None = None` parameter; calls `db.migrate_columns(console)` (was `migrate_vision_columns()`); added early-return with actionable message when zero client ads scraped; persists `type_map` via `db.update_ad_type()` after analysis; excludes `type_map` and `type_distribution` from `db.save_analysis()` loop; updated "6 analysis passes" → "7 analysis passes"
- `main.py` — `main()`: added `GEMINI_API_KEY` preflight check before argparse; changed `--platforms` default to `["google", "linkedin"]` (was all three); added `--scenario` CLI argument with detailed help text; prints stderr warning when no competitors supplied; passes `scenario=args.scenario` to `run()`
- `scrapers/google.py` — Playwright `ImportError` now raises `RuntimeError` with install hint (was silent `return []`)
- `scrapers/linkedin.py` — same Playwright `ImportError` fix as google.py
- `current-state.md` — added Setup section (5-step install sequence, Playwright note, API key source link); updated How to Run with `--scenario` example; updated default platforms note; updated Meta status to BLOCKED with approval timeline
- `CLAUDE.md` — updated description to "7-pass Gemini API analysis"; replaced Ollama setup with Gemini API setup; updated env vars table; updated architecture section; removed Ollama/gemma4 quirks section, added Gemini API notes
- `requirements.txt` — added Python 3.10+ requirement comment and Playwright binary install reminder
- `.env.example` — added Required/Optional labels and API key source URL to all entries

**Why:** Autoplan review (6-pass: CEO + Eng + DX) surfaced three P1 blockers: (1) `ad_type` column missing from DB schema — classifier results existed only in memory, never persisted; (2) `--scenario` flag missing from CLI — no way to drive synthetic ad generation from command line; (3) `migrate_vision_columns()` call in main.py would crash for users upgrading from pre-vision DBs. P2/P3 items: GEMINI_API_KEY not checked before argparse, Playwright errors silently returning empty results, pass count mismatch in log output, stale CLAUDE.md docs.

**How it helps:** `ad_type` per-ad column is now persisted and queryable. `--scenario` enables demo-specific synthetic ad generation. Early API key check gives an actionable error message before any scraping starts. Playwright errors surface with install instructions. Documentation matches current implementation (Gemini, not Ollama).

**Traceback notes:** `update_ad_type()` uses `WHERE (ad_id, advertiser)` — intentional cross-platform write since ad_id namespaces don't collide across platforms. `type_map` and `type_distribution` excluded from `save_analysis()` loop — they are runtime metadata, not analysis pass outputs. Curly brace escape on `--scenario` value is required because `SYNTHETIC_PROMPT` uses `str.format()`.

## 2026-05-22 — Dedup fix: DELETE-before-INSERT + scraper batch dedup in save_ads

**What changed:**
- `storage/db.py` — `save_ads()`: added in-batch dedup (set of `(ad_id, advertiser, platform)` keys) before INSERT to handle scrapers that return the same ad_id twice in one scrape run
- `storage/db.py` — `save_ads()`: added `DELETE FROM ads WHERE ad_id = ? AND advertiser = ? AND platform = ?` before INSERT for each row, making re-runs overwrite instead of append regardless of whether the UNIQUE constraint exists on the table
- `intelligence.db` — manually removed 148 accumulated duplicate rows from previous runs (before → 704 rows, after → 556 rows)

**Why:** The `INSERT OR REPLACE` fix was insufficient because the UNIQUE constraint only applies to new tables (`CREATE TABLE IF NOT EXISTS` doesn't modify existing schema). Additionally, some scrapers (Google) return duplicate ad_ids within a single scrape batch. The explicit DELETE-before-INSERT is constraint-independent and also handles within-batch duplication.

**How it helps:** Pipeline is now idempotent — running twice on the same advertiser produces exactly the same number of rows in the DB. Confirmed: 108 Thoughtspot ads after 1st run, 108 after 2nd run, 0 same-platform duplicates.

**Traceback notes:** Root cause of duplicates was two-part: (1) `INSERT OR REPLACE` needs a UNIQUE constraint to trigger — without it, it's a plain INSERT. (2) Google scraper returns some ad_ids twice per scrape (likely a pagination overlap). The dedup set in save_ads handles both cases.

## 2026-05-22 — Fix 6 QC root causes (classifier, visual pass, synthetic scenario, duplicates, JS noise)

**What changed:**
- `analysis/agent.py` — `_pass_classify()`: added `primary_text` (truncated 200 chars) to slim dict so image-only ads classify correctly
- `analysis/agent.py` — `_pass_visual()`: added `visual_description` field to `visual_data` so Gemini vision extractions feed the visual pattern pass
- `analysis/agent.py` — `_pass_synthetic()`: replaced hardcoded `scenario="promoting their free CRM platform to small business owners"` with a derived scenario built from the advertiser's `tone_descriptors` and `opening_hook_pattern` from the voice pass
- `analysis/agent.py` — `SYNTHETIC_PROMPT`: renamed `why_on_brand` key to `voice_pattern_used` with a clearer instruction to prevent Gemini echoing the key name as its value
- `storage/db.py` — `init_db()`: added `UNIQUE(ad_id, advertiser, platform)` constraint to `ads` table so new installs don't accumulate duplicate rows
- `storage/db.py` — `save_ads()`: changed `INSERT INTO` to `INSERT OR REPLACE INTO` so re-runs overwrite rather than append
- `storage/db.py` — `migrate_vision_columns()`: added a dedup DELETE for existing DBs that predate the UNIQUE constraint
- `main.py` — `sample_ads` construction: now applies `_is_real_copy()` filter before the impressions sort so JS error strings never reach the Brand DNA generator

**Why:** QC subagents found 6 classes of output errors — wrong synthetic scenarios (ThoughtSpot ads were about "small business CRM"), duplicate ads in Brand DNA from re-runs, JS noise strings in sample ads passed to the generator, visual pass never seeing extracted image descriptions, classifier missing body copy for image-only ads, and Gemini echoing `"headline_formula"` as a literal string.

**How it helps:** Synthetic ads will match the actual advertiser's product and voice. Brand DNA won't repeat the same ad. Visual pattern analysis has real image descriptions. Image-only ads are classified correctly. Gemini returns a meaningful voice attribution for each synthetic ad.

**Traceback notes:** The UNIQUE constraint only applies to new table creation — existing `intelligence.db` uses the dedup DELETE in `migrate_vision_columns()` instead. `INSERT OR REPLACE` uses the UNIQUE key if the constraint exists, otherwise falls back to a normal insert (so the migration order matters: dedup first, then subsequent runs use OR REPLACE cleanly).

## 2026-05-22 — LinkedIn OAuth refresh script + pipeline runs for Descope and Thoughtspot

**What changed:**
- `scripts/linkedin_refresh.py` — new script. Tries `LINKEDIN_REFRESH_TOKEN` first; falls back to full browser OAuth flow. Saves new tokens to `.env` via `python-dotenv`'s `set_key`. Accepts `LINKEDIN_REDIRECT_URI` env override (default: `https://localhost`). Note: the intelligence agent's LinkedIn scraper uses Playwright and does NOT require this token — this script is for other platform services using the LinkedIn API directly.
- Ran full pipeline for Descope and Thoughtspot (google + linkedin), outputs in `outputs/`.

**Why:** LinkedIn access token in `.env` had expired. Built a reusable refresh script so tokens can be renewed without manual copy-paste.

**Traceback notes:**
- `LINKEDIN_REDIRECT_URI` must match a registered redirect URI in the LinkedIn app at developers.linkedin.com
- LinkedIn only returns a refresh token if `offline_access` scope is requested; basic `openid profile email` scopes may not include one
- Script location: `intelligence-agent/scripts/linkedin_refresh.py` — run with `python scripts/linkedin_refresh.py` from the `intelligence-agent/` directory

---

## 2026-05-22 — Gemini writes the Brand DNA document (replace Python builder)

**What changed:**
- `generator/markdown.py` — complete rewrite. Removed all Python section-builder helpers (`_voice_section`, `_ad_type_section`, `_visual_section`, etc.) and replaced with a single `client.aio.models.generate_content()` call. The prompt passes all structured analysis JSON (voice, angles, funnel, visual, structure, synthetic, type_distribution) plus up to 20 sample ads with `visual_description` included. Document structure is specified in the prompt template `_PROMPT`; Gemini writes the full markdown. `temperature=0.3` for consistent but not mechanical prose. `_slim_ads` now includes `visual_description` field and uses `a.get("cta") or ""` to prevent `_null_` appearing in output.

**Why:** The Python builder produced mechanically formatted output with no synthesis across sections — each section was independently assembled from its pass's JSON. Gemini can cross-reference all data, write per-platform strategy notes (Google vs LinkedIn separately), produce more detailed step-by-step writing formulas, and surface richer insights from the combined dataset. The Python approach was a workaround for gemma4's instruction-following failures; Gemini doesn't need it.

**How it helps:** Brand DNA output now includes full body copy quotes in ad examples, per-platform strategy notes, more specific gap analysis grounded in the actual data, and better-synthesized voice fingerprints.

**Traceback notes:**
- `response_mime_type` is NOT set for this call (markdown output, not JSON). Omitting it lets Gemini output clean markdown.
- `_null_` CTAs were appearing in output when `ad.get("cta")` returned `None` and was JSON-serialized. Fixed with `or ""`.
- If document quality degrades, check that all 7 analysis passes completed — empty voice/angles/funnel dicts produce thin output.

---

## 2026-05-22 — Remove Ollama startup check from main.py + .env cleanup

**What changed:**
- `main.py` — removed `_check_ollama()` function and the early-exit block that gated startup on Ollama being reachable. Removed `OLLAMA_HOST` and `MODEL` module-level vars; replaced with `GEMINI_MODEL`. Updated the header panel to show `{GEMINI_MODEL} via Gemini API`. Fixed a stale `MODEL` reference in the Phase 1.5 console message.
- `.env` — removed `OLLAMA_HOST` and `OLLAMA_MODEL` entries.
- `.env.example` — replaced Ollama vars with `GEMINI_API_KEY` and `GEMINI_MODEL=gemini-2.5-flash`.

**Why:** After swapping analysis and vision to Gemini, the Ollama startup check was blocking the pipeline entirely (`sys.exit(1)`) even though Ollama is no longer needed.

**Traceback notes:**
- If Ollama vars are still set in `.env` from an old setup they are now ignored — safe to leave or remove.

---

## 2026-05-22 — Swap Ollama for Gemini API (gemini-2.5-flash)

**What changed:**
- `analysis/agent.py` — replaced `ollama.AsyncClient` with `google.genai` client. Swapped `client.chat()` for `client.aio.models.generate_content()` with `response_mime_type="application/json"` and `system_instruction`. Reduced `_CALL_TIMEOUT` from 300s back to 120s (Gemini is fast; 300s was a workaround for Ollama resource pressure). Removed `OLLAMA_HOST` / `MODEL` env vars; added `GEMINI_API_KEY` / `GEMINI_MODEL` (default: `gemini-2.5-flash`).
- `scrapers/vision_extractor.py` — replaced moondream/Ollama vision with Gemini vision. `_fetch_image()` now returns raw bytes instead of base64. `_extract_one()` uses `genai_types.Part.from_bytes()` + `Part.from_text()`. Vision calls now run **concurrently** via `asyncio.gather` (was sequential — forced by Ollama's single-GPU serialization; Gemini handles parallel requests natively). Removed `OLLAMA_HOST` / `VISION_MODEL` env vars.
- `requirements.txt` — replaced `ollama>=0.3.0` with `google-genai>=1.0.0`.
- `.env.example` — replaced Ollama vars with `GEMINI_API_KEY` and `GEMINI_MODEL`.

**Why:** Ollama on an 11.8GB M4 caused repeated timeouts and resource contention — gemma4:e4b (8GB) couldn't run 4 concurrent analysis passes, and moondream required `OLLAMA_NUM_PARALLEL=2` to avoid VRAM competition. Gemini API has no local resource constraints, faster latency (~2-3s per call vs 30-120s for Ollama), and native JSON mode that reliably returns structured output.

**How it helps:** No Ollama server to manage. Vision extraction can now run all 25 ads concurrently (was sequential). Analysis passes 1-4 run truly concurrently. `_CALL_TIMEOUT` back to 120s instead of 300s.

**Traceback notes:**
- `GEMINI_API_KEY` must be set in `.env`. Model defaults to `gemini-2.5-flash`; override with `GEMINI_MODEL=gemini-2.5-pro` etc.
- `response_mime_type="application/json"` replaces Ollama's `format="json"` — Gemini enforces JSON schema at the API level, more reliable than Ollama's best-effort JSON mode.
- Concurrent vision calls via `asyncio.gather` — if rate limits are hit, add a semaphore (e.g. `asyncio.Semaphore(10)`) around `_extract_one`.
- `generator/markdown.py` still imports `OLLAMA_HOST`/`MODEL` env vars (marked unused in the module) — harmless but can be cleaned up later.

Running log of all code changes. Newest entries at the top.
See CLAUDE.md for the logging format and rules.

---

## 2026-05-21 — moondream vision model + analysis timeout fixes

**What changed:**
- `scrapers/vision_extractor.py` — switched vision model from gemma4:e4b to moondream (1.6GB, purpose-built for image text extraction). Added `OLLAMA_VISION_MODEL` env var (default: `moondream`). Raised `_TIMEOUT` from 45s to 60s. Updated console message to show which model is being used.
- `analysis/agent.py` — raised `_CALL_TIMEOUT` from 120s to 300s. Reduced per-pass sample cap from 40/30 to 25 across all passes (voice, angles, funnel, visual, classifier).

**Why:** Two separate timeout problems discovered during repeated pipeline runs:
1. gemma4:e4b (8GB) was timing out on every vision call — it's too large for fast image processing on an 11.8GB M4. moondream (1.6GB) completed all 25/25 vision calls in 314s with zero timeouts.
2. Analysis passes 1-4 run concurrently via asyncio.gather. With 40 samples each, 4 concurrent gemma4:e4b requests exceeded the 120s timeout. 300s is more appropriate for the actual pass duration. Reducing to 25 samples also reduces per-call context size.

**How it helps:** moondream run achieved 25/25 vision successes vs 0/25 with gemma4 under load. 99 ads had copy going into analysis (vs ~40 in previous runs). The 300s timeout ensures analysis passes complete on larger datasets.

**Traceback notes:**
- If moondream is not pulled, set `OLLAMA_VISION_MODEL=gemma4:e4b` to fall back to the main model. Pull moondream with `ollama pull moondream`.
- `OLLAMA_NUM_PARALLEL=2` is the right setting when running moondream for vision + gemma4:e4b for analysis together — avoids VRAM competition.
- Analysis was still timing out at 120s even after the moondream fix. Root cause: 4 concurrent 25-sample passes on gemma4:e4b under Ollama's parallel scheduling. 300s resolved this in subsequent testing.
- **Tomorrow:** restart Ollama with `OLLAMA_NUM_PARALLEL=2 ollama serve`, then run `python main.py --advertiser "HubSpot" --platforms google linkedin`. Everything else is ready.

---

## 2026-05-21 — LinkedIn Ad Library scraper — working implementation

**What changed:**
- `scrapers/linkedin.py` — complete rewrite. Previous version tried to load `linkedin.com/ad-library/search?companyIds={id}` directly, which returns an empty state ("No results found") — the `companyIds` URL param does not auto-trigger the search. New approach: navigate to the search page, fill the `accountOwner` input with the advertiser name, press Enter, then parse the server-rendered results. Key selectors: `.ad-preview` (card wrapper), `.base-ad-preview-card[aria-label]` (format extraction), `.commentary__content` (post body copy), `.ad-preview__dynamic-dimensions-image` (creative image/video thumbnail). Added `scroll_into_view_if_needed()` on each card before parsing to trigger lazy image loading. Added HubSpot, Salesforce, Zendesk, Intercom, Drift to the company ID lookup table (IDs are not used in the new approach but kept for reference).

**Why:** The old scraper was never tested and had wrong assumptions about how LinkedIn's Ad Library works (it's a plain HTML form, not a SPA with API calls). Discovered through live browser inspection: the page is server-side rendered after form submission, there's no XHR data fetch, and bot detection (PerimeterX + protechts.net) blocks API-level calls but not form submission.

**How it helps:** LinkedIn ads are primarily "thought leadership" style (executives/employees posting sponsored content), which means the `primary_text` field has real, long-form copy that analysis passes can work with directly — unlike Google display ads which require vision extraction. Returns ~20 ads per run with 85%+ having usable copy and image URLs for the remaining vision extraction.

**Traceback notes:**
- LinkedIn bot detection allows form-based navigation but would likely block high-frequency scraping. For demo use, one advertiser at a time is fine.
- The `company_id` param is no longer used in `scrape()` — the search uses the advertiser name string. The `_KNOWN_IDS` dict is kept for reference but not for scraping.
- Single image ads: `image_url` is the full creative image. Video ads: `video_url` is the thumbnail URL (not the actual video stream).
- Thought-leadership ads (influencer posts) have `primary_text` as the post content and `headline` as "Person Name — Title". Brand ads (direct HubSpot) have no post text but have image URLs for vision extraction.

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
