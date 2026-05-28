"""Re-run Brand DNA generation from existing DB data — no scraping."""

import asyncio
import json
import sqlite3
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from generator import markdown as md_generator
from storage import db


def get_latest_analysis(advertiser: str, client_id: str = "default") -> dict:
    conn = sqlite3.connect("intelligence.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT pass_name, result_json FROM analysis_results
           WHERE advertiser = ? AND client_id = ?
           AND id IN (
               SELECT MAX(id) FROM analysis_results
               WHERE advertiser = ? AND client_id = ?
               GROUP BY pass_name
           )""",
        (advertiser, client_id, advertiser, client_id),
    ).fetchall()
    conn.close()
    return {row["pass_name"]: json.loads(row["result_json"]) for row in rows}


def get_platforms(advertiser: str, client_id: str = "default") -> list[str]:
    conn = sqlite3.connect("intelligence.db")
    rows = conn.execute(
        "SELECT DISTINCT platform FROM ads WHERE advertiser = ? AND client_id = ?", (advertiser, client_id)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows if r[0]]


async def regen(advertiser: str, client_id: str = "default") -> None:
    print(f"\n=== {advertiser} ===")
    analysis = get_latest_analysis(advertiser, client_id)
    if not analysis:
        print(f"  No analysis found in DB — skipping")
        return

    ads = db.get_ads(advertiser, client_id)
    platforms = get_platforms(advertiser, client_id)
    total = len(ads)
    print(f"  {total} ads, passes: {list(analysis.keys())}, platforms: {platforms}")

    content = await md_generator.generate(
        advertiser=advertiser,
        platforms=platforms,
        total_ads=total,
        analysis=analysis,
        sample_ads=ads,
        date=date.today().isoformat(),
    )

    slug = advertiser.lower().replace(" ", "-")
    out = Path(f"outputs/{slug}-brand-dna-{date.today().isoformat()}.md")
    out.write_text(content, encoding="utf-8")
    print(f"  Written: {out}")


async def main() -> None:
    advertisers = ["HubSpot", "Descope", "ThoughtSpot", "Salesforce"]
    await asyncio.gather(*[regen(a) for a in advertisers])


if __name__ == "__main__":
    asyncio.run(main())
