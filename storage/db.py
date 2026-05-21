"""SQLite persistence layer. Use Postgres in production by swapping get_connection()."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "intelligence.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS ads (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
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
            vision_extracted   INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS analysis_results (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            advertiser  TEXT NOT NULL,
            pass_name   TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS brand_dna (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            advertiser  TEXT NOT NULL,
            content     TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()


def save_ads(ads: list[dict], advertiser_type: str = "client") -> None:
    if not ads:
        return
    conn = get_connection()
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
    conn.execute(
        "INSERT INTO brand_dna (advertiser, content, created_at) VALUES (?, ?, ?)",
        (advertiser, content, datetime.utcnow().isoformat()),
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


def migrate_vision_columns() -> None:
    """Add vision columns to existing DBs that pre-date this feature."""
    conn = get_connection()
    existing = {row[1] for row in conn.execute("PRAGMA table_info(ads)").fetchall()}
    if "visual_description" not in existing:
        conn.execute("ALTER TABLE ads ADD COLUMN visual_description TEXT")
    if "vision_extracted" not in existing:
        conn.execute("ALTER TABLE ads ADD COLUMN vision_extracted INTEGER DEFAULT 0")
    conn.commit()
    conn.close()


def get_ads(advertiser: str) -> list[dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM ads WHERE advertiser = ?", (advertiser,)).fetchall()
    conn.close()
    return [dict(row) for row in rows]
