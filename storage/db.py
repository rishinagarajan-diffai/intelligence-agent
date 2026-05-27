"""Persistence layer. SQLite for local dev; Postgres (DATABASE_URL) in production."""

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "intelligence.db"

_raw_url = os.environ.get("DATABASE_URL", "")
# psycopg2 requires postgresql://, Railway sometimes emits postgres://
DATABASE_URL = _raw_url.replace("postgres://", "postgresql://", 1) if _raw_url.startswith("postgres://") else _raw_url
_is_postgres = bool(DATABASE_URL)


def _sql(query: str) -> str:
    """Swap ? → %s for Postgres positional params."""
    return query.replace("?", "%s") if _is_postgres else query


class _PgConn:
    """Thin sqlite3-compatible wrapper around psycopg2 so all callers see one interface."""

    def __init__(self, raw):
        import psycopg2.extras
        self._conn = raw
        self._cur = raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, sql: str, params=()):
        self._cur.execute(_sql(sql), params)
        return self._cur

    def executemany(self, sql: str, params_seq):
        import psycopg2.extras
        psycopg2.extras.execute_batch(self._cur, _sql(sql), list(params_seq))
        return self._cur

    def commit(self):
        self._conn.commit()

    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass
        self._conn.close()


def get_connection():
    if _is_postgres:
        import psycopg2
        return _PgConn(psycopg2.connect(DATABASE_URL))
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_TABLES = [
    """\
CREATE TABLE IF NOT EXISTS ads (
    id               {pk},
    platform         TEXT NOT NULL,
    advertiser       TEXT NOT NULL,
    advertiser_type  TEXT NOT NULL DEFAULT 'client',
    ad_id            TEXT,
    format           TEXT,
    headline         TEXT,
    primary_text     TEXT,
    description      TEXT,
    cta              TEXT,
    image_url        TEXT,
    video_url        TEXT,
    start_date       TEXT,
    end_date         TEXT,
    impressions_range TEXT,
    scraped_at       TEXT NOT NULL,
    raw_json         TEXT,
    visual_description TEXT,
    vision_extracted   INTEGER DEFAULT 0,
    ad_type            TEXT DEFAULT NULL,
    UNIQUE(ad_id, advertiser, platform)
)""",
    """\
CREATE TABLE IF NOT EXISTS analysis_results (
    id          {pk},
    advertiser  TEXT NOT NULL,
    pass_name   TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at  TEXT NOT NULL
)""",
    """\
CREATE TABLE IF NOT EXISTS brand_dna (
    id          {pk},
    advertiser  TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
)""",
    """\
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    advertiser   TEXT NOT NULL,
    competitors  TEXT NOT NULL DEFAULT '[]',
    platforms    TEXT NOT NULL DEFAULT '["google","linkedin"]',
    scenario     TEXT,
    status       TEXT NOT NULL DEFAULT 'queued',
    error        TEXT,
    created_at   TEXT NOT NULL,
    completed_at TEXT
)""",
    """\
CREATE TABLE IF NOT EXISTS intel_signals (
    id          {pk},
    advertiser  TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL
)""",
]


def init_db() -> None:
    pk = "SERIAL PRIMARY KEY" if _is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
    conn = get_connection()
    for stmt in _TABLES:
        conn.execute(stmt.format(pk=pk))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def save_ads(ads: list[dict], advertiser_type: str = "client") -> None:
    if not ads:
        return
    conn = get_connection()
    seen: set[tuple] = set()
    deduped = []
    for ad in ads:
        key = (ad.get("ad_id"), ad.get("advertiser"), ad.get("platform"))
        if key not in seen:
            seen.add(key)
            deduped.append(ad)
    ads = deduped

    _advertiser = ads[0].get("advertiser", "")
    _platform = ads[0].get("platform", "")
    if _advertiser and _platform:
        conn.execute(
            "DELETE FROM ads WHERE advertiser = ? AND platform = ?",
            (_advertiser, _platform),
        )

    keyed = [(ad.get("ad_id"), ad.get("advertiser"), ad.get("platform")) for ad in ads if ad.get("ad_id")]
    if keyed:
        conn.executemany(
            "DELETE FROM ads WHERE ad_id = ? AND advertiser = ? AND platform = ?",
            keyed,
        )
    conn.executemany(
        """
        INSERT INTO ads (
            platform, advertiser, advertiser_type, ad_id, format, headline,
            primary_text, description, cta, image_url, video_url, start_date,
            end_date, impressions_range, scraped_at, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                ad.get("platform"), ad.get("advertiser"), advertiser_type,
                ad.get("ad_id"), ad.get("format"), ad.get("headline"),
                ad.get("primary_text"), ad.get("description"), ad.get("cta"),
                ad.get("image_url"), ad.get("video_url"), ad.get("start_date"),
                ad.get("end_date"), ad.get("impressions_range"), ad.get("scraped_at"),
                json.dumps(ad),
            )
            for ad in ads
        ],
    )
    conn.commit()
    conn.close()


def save_analysis(advertiser: str, pass_name: str, result: dict) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO analysis_results (advertiser, pass_name, result_json, created_at) VALUES (?, ?, ?, ?)",
        (advertiser, pass_name, json.dumps(result), datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def save_brand_dna(advertiser: str, content: str) -> None:
    conn = get_connection()
    conn.execute("DELETE FROM brand_dna WHERE advertiser = ?", (advertiser,))
    conn.execute(
        "INSERT INTO brand_dna (advertiser, content, created_at) VALUES (?, ?, ?)",
        (advertiser, content, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def update_ad_type(ad_id: str, advertiser: str, ad_type: str) -> None:
    if not ad_id:
        return
    conn = get_connection()
    conn.execute(
        "UPDATE ads SET ad_type = ? WHERE ad_id = ? AND advertiser = ?",
        (ad_type, ad_id, advertiser),
    )
    conn.commit()
    conn.close()


def update_ad_vision(ad_id: str, advertiser: str, headline: str, primary_text: str, cta: str, visual_description: str) -> None:
    conn = get_connection()
    conn.execute(
        """UPDATE ads SET headline = ?, primary_text = ?, cta = ?,
           visual_description = ?, vision_extracted = 1
           WHERE ad_id = ? AND advertiser = ? AND vision_extracted = 0""",
        (headline, primary_text, cta, visual_description, ad_id, advertiser),
    )
    conn.commit()
    conn.close()


def create_job(job_id: str, advertiser: str, competitors: list[str], platforms: list[str], scenario: str | None) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO jobs (id, advertiser, competitors, platforms, scenario, status, created_at) VALUES (?, ?, ?, ?, ?, 'queued', ?)",
        (job_id, advertiser, json.dumps(competitors), json.dumps(platforms), scenario, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def update_job(job_id: str, status: str, error: str | None = None) -> None:
    conn = get_connection()
    completed_at = datetime.utcnow().isoformat() if status in ("complete", "failed") else None
    conn.execute(
        "UPDATE jobs SET status = ?, error = ?, completed_at = ? WHERE id = ?",
        (status, error, completed_at, job_id),
    )
    conn.commit()
    conn.close()


def save_intel_signal(advertiser: str, content: str) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO intel_signals (advertiser, content, created_at) VALUES (?, ?, ?)",
        (advertiser, content, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_brand_dna(advertiser: str) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT content, created_at FROM brand_dna WHERE advertiser = ? ORDER BY id DESC LIMIT 1",
        (advertiser,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_job(job_id: str) -> dict | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_latest_intel_signal(advertiser: str) -> dict | None:
    conn = get_connection()
    row = conn.execute(
        "SELECT content, created_at FROM intel_signals WHERE advertiser = ? ORDER BY id DESC LIMIT 1",
        (advertiser,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_ads(advertiser: str) -> list[dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM ads WHERE advertiser = ?", (advertiser,)).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_ads_platforms(advertiser: str) -> list[str]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT DISTINCT platform FROM ads WHERE advertiser = ?", (advertiser,)
    ).fetchall()
    conn.close()
    return [r["platform"] for r in rows if r["platform"]]


def get_latest_analysis(advertiser: str) -> tuple[dict, str, int]:
    """Return (analysis_by_pass, run_date, ad_count) for the most recent run."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT pass_name, result_json, created_at
        FROM analysis_results
        WHERE advertiser = ?
          AND id IN (
              SELECT MAX(id) FROM analysis_results
              WHERE advertiser = ?
              GROUP BY pass_name
          )
        """,
        (advertiser, advertiser),
    ).fetchall()
    conn.close()
    if not rows:
        return {}, "", 0

    run_date = min(r["created_at"] for r in rows)[:10]
    by_pass = {r["pass_name"]: json.loads(r["result_json"]) for r in rows}
    td = by_pass.get("type_distribution", {})
    ad_count = sum(td.values()) if isinstance(td, dict) else 0
    return by_pass, run_date, ad_count


def get_stale_market_context(advertiser: str) -> dict | None:
    """Return the most recent non-empty market_context result from DB (SYSTEMIC-04 fallback).

    Uses backend-appropriate JSON array length check.
    Returns None if no cached result exists.
    """
    if _is_postgres:
        json_filter = "jsonb_array_length((result_json::jsonb)->'competitors') > 0"
    else:
        json_filter = "json_array_length(json_extract(result_json, '$.competitors')) > 0"

    conn = get_connection()
    row = conn.execute(
        f"""SELECT result_json, created_at FROM analysis_results
            WHERE advertiser = ? AND pass_name = 'market_context'
              AND {json_filter}
            ORDER BY id DESC LIMIT 1""",
        (advertiser,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    cached = json.loads(row["result_json"])
    stale_date = row["created_at"][:10]
    summary = cached.get("market_summary", "")
    cached["market_summary"] = (
        f"[Cached from {stale_date} — live search returned no results this run] {summary}"
    )
    return cached


# ---------------------------------------------------------------------------
# Migrations (existing DBs only — init_db handles new deployments)
# ---------------------------------------------------------------------------

def migrate_columns(console=None) -> None:
    """Add columns introduced after initial deploy and deduplicate the ads table."""
    conn = get_connection()
    added = []

    if _is_postgres:
        for col, defn in [
            ("visual_description", "TEXT"),
            ("vision_extracted", "INTEGER DEFAULT 0"),
            ("ad_type", "TEXT DEFAULT NULL"),
        ]:
            conn.execute(f"ALTER TABLE ads ADD COLUMN IF NOT EXISTS {col} {defn}")
            # IF NOT EXISTS means we can't cheaply tell what was added; log all as attempted
    else:
        rows = conn.execute("PRAGMA table_info(ads)").fetchall()
        existing = {row[1] for row in rows}
        for col, defn in [
            ("visual_description", "TEXT"),
            ("vision_extracted", "INTEGER DEFAULT 0"),
            ("ad_type", "TEXT DEFAULT NULL"),
        ]:
            if col not in existing:
                conn.execute(f"ALTER TABLE ads ADD COLUMN {col} {defn}")
                added.append(col)

    cursor = conn.execute("""
        DELETE FROM ads WHERE id NOT IN (
            SELECT MAX(id) FROM ads
            WHERE ad_id IS NOT NULL
            GROUP BY ad_id, advertiser, platform
        ) AND ad_id IS NOT NULL
    """)
    deduped = cursor.rowcount if hasattr(cursor, "rowcount") else 0
    conn.commit()
    conn.close()
    if (added or deduped > 0) and console:
        msgs = []
        if added:
            msgs.append(f"added columns: {', '.join(added)}")
        if deduped > 0:
            msgs.append(f"removed {deduped} duplicate ad rows")
        console.print(f"  [dim]DB migration: {'; '.join(msgs)}[/dim]")
