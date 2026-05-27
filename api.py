"""Polling REST API for the Campaign Intelligence Agent.

Start with:
    uvicorn api:app --host 0.0.0.0 --port 8000

Auth: every endpoint except /docs and /openapi.json requires an `X-API-Key`
header matching the `API_KEY` env var.

Rate limits (per IP):
    POST /analyze              — 5/hour   (Gemini cost: ~$0.10–0.30 per call)
    POST /regen/{advertiser}   — 10/hour
    GET  /jobs/{job_id}        — 60/minute
    GET  /brand-dna/{name}     — 30/minute
    GET  /intel-signal/{name}  — 30/minute

Observability: if `SENTRY_DSN` is set, errors and slow requests are reported.

Endpoints:
    POST /analyze               — queue a new analysis job
    GET  /jobs/{job_id}         — poll job status + results
    GET  /brand-dna/{advertiser}    — latest brand DNA for an advertiser
    GET  /intel-signal/{advertiser} — latest intel signal delta
    POST /regen/{advertiser}        — regenerate brand DNA from existing DB data (sync, ~30s)
"""

import json
import os
import uuid
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

# Sentry must initialize before FastAPI is created so it can capture startup
# errors. A missing DSN is a no-op — Sentry stays inert and doesn't fail.
_SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if _SENTRY_DSN:
    import sentry_sdk
    sentry_sdk.init(
        dsn=_SENTRY_DSN,
        traces_sample_rate=1.0,  # full sampling during POC; dial down later
        send_default_pii=False,
    )

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from storage import db


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_API_KEY = os.environ.get("API_KEY", "")


async def require_api_key(x_api_key: str = Header(default="", alias="X-API-Key")):
    if not _API_KEY:
        # Server-side misconfiguration — fail loud so this isn't a silent open door
        raise HTTPException(status_code=503, detail="API_KEY env var not configured on server")
    if x_api_key != _API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key header")


# ---------------------------------------------------------------------------
# Rate limiting (per IP)
# ---------------------------------------------------------------------------

limiter = Limiter(key_func=get_remote_address)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="Campaign Intelligence API", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class AnalyzeRequest(BaseModel):
    advertiser: str
    competitors: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default=["google", "linkedin"])
    scenario: str | None = None


class JobResponse(BaseModel):
    job_id: str
    status: str
    advertiser: str
    competitors: list[str]
    platforms: list[str]
    scenario: str | None
    created_at: str
    completed_at: str | None
    brand_dna: str | None = None
    intel_signal: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Background pipeline runner
# ---------------------------------------------------------------------------

async def _run_pipeline(job_id: str, req: AnalyzeRequest) -> None:
    db.update_job(job_id, "running")
    try:
        import main as pipeline
        await pipeline.run(
            advertiser=req.advertiser,
            competitors=req.competitors,
            platforms=req.platforms,
            scenario=req.scenario,
        )
        db.update_job(job_id, "complete")
    except Exception as exc:
        db.update_job(job_id, "failed", error=str(exc))
        if _SENTRY_DSN:
            import sentry_sdk
            sentry_sdk.capture_exception(exc)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/analyze", status_code=202, dependencies=[Depends(require_api_key)])
@limiter.limit("5/hour")
async def analyze(request: Request, req: AnalyzeRequest, background_tasks: BackgroundTasks):
    job_id = str(uuid.uuid4())
    db.create_job(
        job_id=job_id,
        advertiser=req.advertiser,
        competitors=req.competitors,
        platforms=req.platforms,
        scenario=req.scenario,
    )
    background_tasks.add_task(_run_pipeline, job_id, req)
    return {"job_id": job_id, "status": "queued"}


@app.get("/jobs/{job_id}", response_model=JobResponse, dependencies=[Depends(require_api_key)])
@limiter.limit("60/minute")
async def get_job(request: Request, job_id: str):
    row = db.get_job(job_id)
    if not row:
        raise HTTPException(status_code=404, detail="job not found")

    result: dict = {
        "job_id": row["id"],
        "status": row["status"],
        "advertiser": row["advertiser"],
        "competitors": json.loads(row["competitors"]),
        "platforms": json.loads(row["platforms"]),
        "scenario": row["scenario"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
        "error": row["error"],
    }

    if row["status"] == "complete":
        dna = db.get_brand_dna(row["advertiser"])
        if dna:
            result["brand_dna"] = dna["content"]
        sig = db.get_latest_intel_signal(row["advertiser"])
        if sig:
            result["intel_signal"] = sig["content"]

    return result


@app.get("/brand-dna/{advertiser}", dependencies=[Depends(require_api_key)])
@limiter.limit("30/minute")
async def get_brand_dna(request: Request, advertiser: str):
    row = db.get_brand_dna(advertiser)
    if not row:
        raise HTTPException(status_code=404, detail="no brand DNA found for this advertiser")
    return {"advertiser": advertiser, "content": row["content"], "created_at": row["created_at"]}


@app.get("/intel-signal/{advertiser}", dependencies=[Depends(require_api_key)])
@limiter.limit("30/minute")
async def get_intel_signal(request: Request, advertiser: str):
    row = db.get_latest_intel_signal(advertiser)
    if not row:
        raise HTTPException(status_code=404, detail="no intel signal found for this advertiser")
    return {"advertiser": advertiser, "content": row["content"], "created_at": row["created_at"]}


@app.post("/regen/{advertiser}", dependencies=[Depends(require_api_key)])
@limiter.limit("10/hour")
async def regen(request: Request, advertiser: str):
    """Regenerate brand DNA from existing DB data — skips scraping and analysis (~30s)."""
    from datetime import date
    from generator import markdown as md_generator

    analysis = {
        row["pass_name"]: json.loads(row["result_json"])
        for row in _db_latest_analysis_rows(advertiser)
    }
    if not analysis:
        raise HTTPException(status_code=404, detail="no analysis data found for this advertiser")

    ads = db.get_ads(advertiser)
    platforms = db.get_ads_platforms(advertiser)

    try:
        content = await md_generator.generate(
            advertiser=advertiser,
            platforms=platforms,
            total_ads=len(ads),
            analysis=analysis,
            sample_ads=ads,
            date=date.today().isoformat(),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    db.save_brand_dna(advertiser, content)
    return {"advertiser": advertiser, "brand_dna": content}


def _db_latest_analysis_rows(advertiser: str) -> list:
    conn = db.get_connection()
    rows = conn.execute(
        """SELECT pass_name, result_json FROM analysis_results
           WHERE advertiser = ?
             AND id IN (
                 SELECT MAX(id) FROM analysis_results
                 WHERE advertiser = ?
                 GROUP BY pass_name
             )""",
        (advertiser, advertiser),
    ).fetchall()
    conn.close()
    return rows
