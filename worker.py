"""ARQ worker — pulls analyze jobs from Redis and runs the pipeline.

Run with:
    arq worker.WorkerSettings

Why this exists: FastAPI's BackgroundTasks run in-process and die when the
container restarts (every Railway deploy, every env var change). Moving the
4-minute pipeline to a separate worker process backed by a Redis queue means
jobs survive API restarts.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# Sentry init mirrors api.py so worker-side exceptions also report.
_SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if _SENTRY_DSN:
    import sentry_sdk
    sentry_sdk.init(
        dsn=_SENTRY_DSN,
        traces_sample_rate=1.0,
        send_default_pii=False,
    )

from arq.connections import RedisSettings

from storage import db


REDIS_URL = os.environ.get("REDIS_URL", "")


async def run_pipeline(ctx, job_id: str, advertiser: str, competitors: list[str], platforms: list[str], scenario: str | None):
    db.update_job(job_id, "running")
    try:
        import main as pipeline
        await pipeline.run(
            advertiser=advertiser,
            competitors=competitors,
            platforms=platforms,
            scenario=scenario,
        )
        db.update_job(job_id, "complete")
    except Exception as exc:
        db.update_job(job_id, "failed", error=str(exc))
        if _SENTRY_DSN:
            import sentry_sdk
            sentry_sdk.capture_exception(exc)
        raise  # let ARQ also mark the queue job as failed


async def startup(ctx):
    db.init_db()


class WorkerSettings:
    functions = [run_pipeline]
    redis_settings = RedisSettings.from_dsn(REDIS_URL) if REDIS_URL else RedisSettings()
    max_jobs = 1          # serial — one job at a time (demo)
    max_tries = 1         # no retry — caller resubmits if it fails (demo)
    job_timeout = 900     # 15 min; pipeline normally ~4 min
    keep_result = 0       # results live in Postgres, not Redis
    on_startup = startup
