# Open Issues — Campaign Intelligence Agent

_Last updated: 2026-05-26 (post-demo session — Meta Playwright working, Competitive Landscape section added)_

Issues identified via QA runs against HubSpot, Descope, ThoughtSpot, and Salesforce Brand DNA outputs. All P0 and P1 issues resolved. Language B is now a data ceiling (testimonials in headline field, truncated scrapes), not a code gap — Language A requires Meta platform data or authenticated LinkedIn image fetch.

---

## P0 — Blocking (fix before using Brand DNA in production)

### ~~P0-1: Visual pattern section is copy-derived, not image-derived~~ — FIXED 2026-05-22

**Fix applied:** `_pass_visual()` in `analysis/agent.py` now fetches image bytes for top 10 ads via `_fetch_image_for_visual()` and passes them as `genai_types.Part.from_bytes()`. VISUAL_PROMPT rewritten to instruct Gemini to describe only what is visually observable, not infer from copy. Falls back to text-only if no images fetchable. **Remaining ceiling:** Google display ad corpus is text-heavy/minimal by design — richer visual creative lives on LinkedIn (no image fetch without auth) and Meta (pending API approval). Visual scores improved but remain C for HubSpot/ThoughtSpot due to platform coverage gap, not code gap.

---

### ~~P0-2: Third-party / reseller ads being scraped as advertiser's own~~ — FIXED 2026-05-22

**Fix applied:** `scrapers/google.py` `scrape()` captures `target_advertiser_id` from ATC URL after clicking target advertiser; filters out creatives where proto field `"1"` doesn't match. `_normalize()` adds `advertiser_id` field. `analysis/ad_filter.py` (new module) adds hybrid ownership filter: domain check first → Gemini classification fallback for ads without landing URLs. LinkedIn `_parse_card()` filters cards where aria-label company name doesn't match target advertiser.

---

## P1 — High priority (degrade output quality noticeably)

### ~~P1-1: Bare brand name entries in verbatim headline list~~ — FIX APPLIED 2026-05-22 (verify in p6)

**Root cause confirmed:** LinkedIn image-only ads with real primary_text pass the main.py filter (kept for body copy), but their bare brand-name headlines appear in the verbatim list. main.py filter correctly keeps these ads; the problem is in the generator surfacing their empty headline.

**Fix applied:** main.py filter added for ads with bare-brand headline AND no primary_text. Generator prompt updated to skip bare brand name headlines in the verbatim list entirely. p5 QA still showed ×7 entries — generator prompt fix was applied after p5, will confirm clean in p6.

---

### ~~P1-2: Salesforce Slack sub-brand bleed skewing formula frequency~~ — FIXED 2026-05-22

**Fix applied:** `analysis/agent.py` `VOICE_PROMPT` `headline_formulas` schema now has optional `sub_brand_note` field. `generator/markdown.py` headline formula instruction tells Gemini to note sub-brand concentration in parentheses. p5 QA confirmed: Salesforce formula #2 now shows "(frequency driven primarily by Slack ads)." "Slack - Official Site" ×4 in verbatim list is factually accurate — 4 distinct Slack ads in the data.

---

### ~~P1-1 (old): Sample ads passed to Brand DNA generator contain duplicates~~ — FIXED 2026-05-22

**Fix applied:** `main.py` `sample_ads` construction now deduplicates on `(headline.strip(), primary_text[:100].strip())` after sort by impressions. Highest-impression copy kept when same creative appears under multiple ad_ids.

---

### ~~P1-2 (old): LinkedIn attribution line bleeding into primary_text~~ — FIXED 2026-05-22

**Fix applied:** `scrapers/linkedin.py` `_parse_card()` strips leading lines matching `"Sponsored "` or containing `"www."` after `primary_text` extraction.

---

### ~~P1-3: Headline formula accuracy is low for HubSpot and ThoughtSpot~~ — FIXED 2026-05-22

**Fix applied:** `VOICE_PROMPT` updated to extract `headline_formulas` array (up to 3, ranked by frequency, each with formula/frequency/example). `generator/markdown.py` renders these as a ranked list in the Voice Fingerprint section.

---

## P2 — Medium priority (reduce document reliability/trust)

### ~~P2-5: Google responsive display ads leaking "Sponsored. www.domain." into primary_text~~ — FIXED 2026-05-26

**Fix applied:** `scrapers/google.py` `_extract_responsive_copy()` candidate filter now excludes strings containing `www.` or starting with `Sponsored`. Two lines added to the `clean` list comprehension.

---

### ~~P2-6: Meta keyword search returns noise — non-advertiser ads bleed into results~~ — FIXED 2026-05-26

**Fix applied:** `scrapers/meta.py` `_walk_json()` now checks `page.name` in the web UI GraphQL response before adding an ad. If `page_name` is present and the advertiser name is not in it, the ad is skipped. Ads without a `page_name` field still pass through to `analysis/ad_filter.py` as before. **Remaining ceiling:** Graph API with `search_page_ids` once Ad Library API approval arrives — that will be the definitive fix.

---

### ~~P2-7: Synthetic CTA composite string~~ — FIXED 2026-05-26

**Fix applied:** `generator/markdown.py` CTA rules sentence now ends with: "If the ALLOWED list contains multiple variants of the same CTA (e.g. multiple demo CTAs), pick exactly one — do not combine them with slashes or commas." Prevents `Request/Book/Schedule Demo` composites.

---

### P2-4: Descope `SSO Documentation` CTA is unverified

**What's wrong:** `scrapers/google.py` `_extract_responsive_copy()` fetches the rendered preview JS URL for Format 3 (responsive display) ads and extracts quoted strings. The attribution text "Sponsored. descope.com. www.descope.com/. Embed bot protection..." starts with a capital letter and contains a space, so it passes the candidate regex filter and ends up as the `primary_text` for that ad. Confirmed: Google ad CR00623979497372254209 for Descope.

**Root cause:** The candidate filter in `_extract_responsive_copy()` has JS noise exclusions but no URL/domain pattern exclusion. Strings containing `www.` or starting with `Sponsored` slip through.

**Fix:** In `_extract_responsive_copy()`, add to the candidate filter:
```python
and "www." not in s
and not s.startswith("Sponsored")
```

**File:** `scrapers/google.py` — `_extract_responsive_copy()` candidate `clean` list comprehension
**Effort:** ~5 min

---

### ~~P2-1: Synthetic CTA compliance — partial misses across advertisers~~ — FIXED 2026-05-26

**Fix applied:** `generator/markdown.py` CTA rules line in Synthetic Ad Templates now ends with explicit negative constraint: "Do not use any CTA string not present in the ALLOWED list above, even if similar text appears elsewhere in the data or prompt." Verify in next regen run.

---

### ~~P2-1 (old): Synthetic template body copy not in blockquotes~~ — FIXED 2026-05-22
**Fix applied:** Generator prompt now includes explicit `> ` blockquote format example. Confirmed working in Descope/ThoughtSpot/Salesforce p5 outputs.

---

### ~~P2-2 (old): CTA text embedded inside verbatim headline blockquotes~~ — FIXED 2026-05-22
**Fix applied:** Generator prompt now says "ONLY the headline text in the blockquote, nothing else before or after." Also removed the "add CTA in italics after blockquote" permission that caused overcorrection in p5. Confirmed clean in Salesforce p5.

---

### ~~P2-3 (old): Synthetic template visual description hallucinates brand creative~~ — FIXED 2026-05-22
**Fix applied:** Generator prompt constrains visual descriptions to "ONLY visual elements consistent with what was observed in the Visual Patterns section." ThoughtSpot "AI brain graphic" confirmed gone in p5.

---

### ~~P2-1 (old): Platform strategy sections are generic boilerplate~~ — FIXED 2026-05-22

**Fix applied:** `generator/markdown.py` `_PROMPT` Positioning Map section now instructs Gemini to prefix platform strategy with italicized disclaimer: "_Note: platform strategy below is inferred from go-to-market priors, not from observed platform-segmented creative data._" Confirmed present in all 4 p4 Brand DNA outputs.

---

### ~~P2-2 (old): ThoughtSpot synthetic engagement_brand templates use BOFU CTAs~~ — FIXED 2026-05-22

**Fix applied:** `SYNTHETIC_PROMPT` in `analysis/agent.py` now has explicit CTA-by-type rules (awareness CTAs for engagement_brand; conversion CTAs for form_lead_gen; registration CTAs for webinar_event).

---

### ~~P2-3 (old): HubSpot messaging angle frequency count is inflated~~ — FIXED 2026-05-22

**Fix applied:** `ANGLE_PROMPT` in `analysis/agent.py` now instructs Gemini to assign exactly one angle per ad (dominant angle only).

---

### P2-4: Descope `SSO Documentation` CTA is unverified

**What's wrong:** "SSO Documentation" is listed as a CTA construction in Descope's Voice Fingerprint but does not appear in any verbatim ad example. It may be a sitelink or an inferred/hallucinated value.

**Fix:** Run a verification step — check if "SSO Documentation" appears as a sitelink in any raw scraped ad data for Descope. If not, remove it from the Brand DNA on the next Descope run.

**Verification query:**
```sql
SELECT ad_id, headline, primary_text, raw_json FROM ads
WHERE advertiser='Descope' AND (headline LIKE '%SSO%' OR primary_text LIKE '%SSO Documentation%');
```

**File:** No code change needed — re-run Descope scraper and inspect raw sitelink data
**Effort:** ~5 min investigation

---

## P3 — Low priority (minor quality / polish)

### ~~P3-1: Truncated CTA artifact in HubSpot Brand DNA~~ — FIXED 2026-05-26

**Fix applied:** Root cause was in `scrapers/vision_extractor.py`, not `scrapers/google.py`. Gemini vision was reading visible sitelink text from display ad images and returning a comma-separated list as the `cta` field, which truncated mid-word. `_VISION_PROMPT` updated: CTA description now says "single CTA button text only — one short phrase, NOT a list of sitelinks." Takes effect on next full pipeline run (vision re-extracts on new scrapes).

---

### ~~P3-2: ThoughtSpot funnel distribution presented as definitive despite pass instability~~ — CLOSED (no bug)

**Investigation result:** Pipeline uses in-memory analysis during a full run — never reads back stale rows from DB for generation. `regen_dna.py` uses `MAX(id) GROUP BY pass_name`, also correct. The BOFU variance (2–13) is natural variance across different scrape sessions with different ad rotation, not a stale-read bug. No code change needed. The Brand DNA will always reflect the most recent run's data.

---

### ~~P3-3: `brand_dna` table accumulates unlimited historical versions~~ — FIXED 2026-05-26

**Fix applied:** `storage/db.py` `save_brand_dna()` — `DELETE FROM brand_dna WHERE advertiser = ?` added before INSERT. Table now keeps exactly 1 row per advertiser.

---

## Resolved Issues

### Resolved in p5–p9 session (2026-05-22 afternoon/evening)
- ~~P1-1 (new): Bare brand name entries in verbatim list~~ — fixed: `main.py` filter + generator prompt rules (4 explicit verbatim headline rules)
- ~~P1-2 (new): Salesforce Slack sub-brand bleed~~ — fixed: `sub_brand_note` field in VOICE_PROMPT + generator prompt renders it in parentheses
- ~~P2-1 (old): Synthetic template body not in blockquotes~~ — fixed: generator prompt has explicit `> ` format example
- ~~P2-2 (old): CTA text inside headline blockquotes~~ — fixed: "Include nothing before or after each blockquote line"
- ~~P2-3 (old): Synthetic visual description hallucinates brand creative~~ — fixed: "ONLY visual elements consistent with what was observed in Visual Patterns"
- ~~CTA key mismatch: generator checking wrong key name~~ — fixed: multi-key fallback chain (`cta_constructions` → `cta_patterns` → `ctas`)
- ~~No CTA missing from ALLOWED list~~ — fixed: `No CTA` always appended to cta_items
- ~~Form/Lead Gen templates using No CTA~~ — fixed: per-type CTA rules in generator prompt

### Resolved in p4 session (2026-05-22 afternoon)
- ~~P0-1: Visual pass not using image vision~~ — fixed: `_pass_visual()` now fetches image bytes + VISUAL_PROMPT rewrote to force image-first observation
- ~~P0-2: Third-party ads being scraped as advertiser's own~~ — fixed: Google `advertiser_id` filter + hybrid ownership filter (`analysis/ad_filter.py`) + LinkedIn company name filter in `_parse_card()`
- ~~P1-1 (old): Sample ads duplicated in verbatim list~~ — fixed: content dedup on `(headline, primary_text[:100])` in `main.py`
- ~~P1-2 (old): LinkedIn attribution line in primary_text~~ — fixed: leading "Sponsored" / "www." lines stripped in `_parse_card()`
- ~~P1-3: Single headline formula too prescriptive~~ — fixed: `VOICE_PROMPT` now extracts 3 ranked formulas
- ~~P2-1 (old): Platform strategy sections are generic boilerplate~~ — fixed: italicized disclaimer added to generator prompt
- ~~P2-2 (old): Engagement_brand synthetic templates use BOFU CTAs~~ — fixed: CTA-by-type rules in `SYNTHETIC_PROMPT`
- ~~P2-3 (old): Angle count inflation~~ — fixed: one-angle-per-ad instruction in `ANGLE_PROMPT`
- ~~Thought-leadership bylines in verbatim headline list~~ — fixed: `_is_byline()` em-dash filter in `main.py`
- ~~Stale DB rows from filtered ads persisting across runs~~ — fixed: full `DELETE WHERE advertiser=? AND platform=?` in `save_ads()` before inserts

### Resolved in earlier sessions (2026-05-22 morning)
- ~~`ad_type` column missing from DB schema~~ — fixed
- ~~`--scenario` CLI flag missing~~ — fixed
- ~~`migrate_vision_columns()` crash on startup~~ — fixed (renamed + expanded)
- ~~Playwright ImportError silent return~~ — fixed (now raises RuntimeError)
- ~~Duplicate ads accumulating across re-runs~~ — fixed (DELETE-before-INSERT)
- ~~Hardcoded synthetic scenario~~ — fixed (derived from voice pass)
- ~~JS noise strings in Brand DNA sample ads~~ — fixed (`_is_real_copy()` filter)
- ~~SQLite database locked errors on parallel runs~~ — fixed (`timeout=30` on connection)
