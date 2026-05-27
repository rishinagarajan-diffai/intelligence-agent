#!/usr/bin/env python3
"""Campaign Intelligence Agent — orchestrator.

Usage:
    python main.py --advertiser "Notion" \\
                   --competitors "Coda" "Confluence" "Monday.com" "Airtable" \\
                   --platforms meta google linkedin
"""

import argparse
import asyncio
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich.table import Table

load_dotenv()

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

from scrapers import meta as meta_scraper
from scrapers import google as google_scraper
from scrapers import linkedin as linkedin_scraper
from scrapers import vision_extractor
from analysis import agent as analysis_agent
from generator import markdown as md_generator
from storage import db

console = Console()

# ---------------------------------------------------------------------------
# LinkedIn company ID resolution
# ---------------------------------------------------------------------------

_LINKEDIN_IDS: dict[str, str] = {
    "notion": "10257271",
    "coda": "18480454",
    "confluence": "1117",
    "atlassian": "1117",
    "monday.com": "10902633",
    "monday": "10902633",
    "airtable": "3949382",
    "asana": "1419599",
    "clickup": "18416836",
}


def _resolve_linkedin_id(name: str) -> str | None:
    env_raw = os.environ.get("LINKEDIN_COMPANY_IDS", "")
    env_map: dict[str, str] = {}
    for pair in env_raw.split(","):
        pair = pair.strip()
        if ":" in pair:
            k, v = pair.split(":", 1)
            env_map[k.strip().lower()] = v.strip()
    return env_map.get(name.lower()) or _LINKEDIN_IDS.get(name.lower())


# ---------------------------------------------------------------------------
# Scraping helpers
# ---------------------------------------------------------------------------

async def _scrape_one(name: str, platform: str) -> list[dict]:
    loop = asyncio.get_event_loop()
    try:
        if platform == "meta":
            return await loop.run_in_executor(None, meta_scraper.scrape, name)
        elif platform == "google":
            return await loop.run_in_executor(None, google_scraper.scrape, name)
        elif platform == "linkedin":
            cid = _resolve_linkedin_id(name) or "0"
            return await loop.run_in_executor(None, linkedin_scraper.scrape, cid, name)
    except Exception as exc:
        console.print(f"  [red]✗ {platform}/{name}: {exc}[/red]")
    return []


def _parse_impressions(imp_range: str | None) -> int:
    if not imp_range:
        return 0
    try:
        return int(imp_range.split("-")[0].replace(",", "").strip())
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------

async def run(advertiser: str, competitors: list[str], platforms: list[str], scenario: str | None = None) -> None:
    console.print(Panel(
        f"[bold white]Campaign Intelligence Agent[/bold white]\n\n"
        f"  Advertiser  : [cyan]{advertiser}[/cyan]\n"
        f"  Competitors : [cyan]{', '.join(competitors) or 'none'}[/cyan]\n"
        f"  Platforms   : [yellow]{', '.join(platforms)}[/yellow]\n"
        f"  LLM         : [green]{GEMINI_MODEL}[/green] via Gemini API",
        border_style="bright_blue",
        padding=(1, 2),
    ))

    db.init_db()
    db.migrate_columns(console)

    # -----------------------------------------------------------------------
    # Phase 1 — Scrape
    # -----------------------------------------------------------------------
    console.rule("[bold bright_blue]Phase 1 — Scraping Ad Libraries")

    all_targets = [advertiser] + competitors
    scrape_jobs = [
        (name, platform)
        for name in all_targets
        for platform in platforms
    ]

    ads_by_advertiser: dict[str, list[dict]] = {name: [] for name in all_targets}
    results_table = Table(show_header=True, header_style="bold", box=None)
    results_table.add_column("Advertiser", style="cyan", min_width=16)
    results_table.add_column("Platform", style="magenta", min_width=10)
    results_table.add_column("Ads", style="green", justify="right", min_width=6)
    results_table.add_column("Status")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task_id = progress.add_task("Scraping...", total=len(scrape_jobs))

        for name, platform in scrape_jobs:
            progress.update(task_id, description=f"[cyan]{name}[/cyan] / [magenta]{platform}[/magenta]")
            ads = await _scrape_one(name, platform)
            ads_by_advertiser[name].extend(ads)

            a_type = "client" if name == advertiser else "competitor"
            if ads:
                db.save_ads(ads, a_type)

            status = "[green]✓[/green]" if ads else "[yellow]empty[/yellow]"
            results_table.add_row(name, platform, str(len(ads)), status)
            progress.advance(task_id)

    console.print(results_table)
    console.print()

    # -----------------------------------------------------------------------
    # Phase 1.5 — Vision extraction (read copy from image creatives)
    # -----------------------------------------------------------------------
    console.rule("[bold bright_blue]Phase 1.5 — Vision Extraction")

    all_ads_for_vision = list(ads_by_advertiser.values())
    all_flat = [ad for ads in all_ads_for_vision for ad in ads]
    needs = sum(
        1 for a in all_flat
        if a.get("image_url")
        and not (a.get("headline") or "").strip()
        and not (a.get("primary_text") or "").strip()
    )

    if needs == 0:
        console.print("  [dim]All ads already have copy — skipping vision extraction[/dim]\n")
    else:
        console.print(f"  [dim]{needs} ads have image-only creatives — extracting copy via {GEMINI_MODEL} vision[/dim]")
        for name in list(ads_by_advertiser.keys()):
            updated = await vision_extractor.extract_all(
                ads_by_advertiser[name], console=console
            )
            ads_by_advertiser[name] = updated
            for ad in updated:
                if ad.get("visual_description") or (ad.get("headline") or "").strip():
                    db.update_ad_vision(
                        ad.get("ad_id", ""),
                        ad.get("advertiser", name),
                        ad.get("headline", "") or "",
                        ad.get("primary_text", "") or "",
                        ad.get("cta", "") or "",
                        ad.get("visual_description", "") or "",
                    )

        extracted = sum(
            1 for ads in ads_by_advertiser.values()
            for a in ads
            if (a.get("headline") or "").strip()
        )
        console.print(f"[green]✓[/green] Vision extraction complete — {extracted} ads now have copy\n")

    # -----------------------------------------------------------------------
    # Phase 1.6 — Ad ownership filter (runs after vision so image copy is available)
    # -----------------------------------------------------------------------
    from analysis.ad_filter import filter_owned_ads

    console.print(f"  [dim]Filtering owned ads (domain check + Gemini fallback)...[/dim]")
    try:
        ads_by_advertiser[advertiser] = await filter_owned_ads(
            ads_by_advertiser[advertiser], advertiser, console
        )
    except Exception as _filter_exc:
        console.print(f"  [yellow]Warning: ownership filter failed ({_filter_exc!r}) — using unfiltered ads[/yellow]")

    total_client = len(ads_by_advertiser[advertiser])
    total_competitor = sum(len(v) for name, v in ads_by_advertiser.items() if name != advertiser)
    console.print(
        f"[green]✓[/green] Scraped [bold]{total_client}[/bold] {advertiser} ads + "
        f"[bold]{total_competitor}[/bold] competitor ads\n"
    )

    if total_client == 0:
        console.print(
            f"[yellow]Warning:[/yellow] No ads found for {advertiser}. "
            "Check that the advertiser name matches exactly how it appears in the ad library. "
            "Skipping analysis and Brand DNA generation."
        )
        return

    client_ads = ads_by_advertiser[advertiser]
    competitor_ads = {c: ads_by_advertiser[c] for c in competitors}

    # -----------------------------------------------------------------------
    # Phase 2 — Analysis
    # -----------------------------------------------------------------------
    console.rule("[bold bright_blue]Phase 2 — Analysis (6 passes)")

    analysis = await analysis_agent.run_all_passes(
        advertiser, client_ads, competitor_ads, console, scenario=scenario
    )

    # Persist ad_type classifications to DB
    type_map = analysis.get("type_map", {})
    if type_map:
        for ad_id, ad_type in type_map.items():
            db.update_ad_type(ad_id, advertiser, ad_type)
        console.print(f"  [dim]Updated ad_type for {len(type_map)} ads[/dim]")

    # Fetch the previous analysis snapshot BEFORE saving the new one —
    # get_latest_analysis() returns the highest-id row per pass, which is the previous run.
    from storage.db import get_latest_analysis
    prev_analysis, prev_date, prev_ad_count = get_latest_analysis(advertiser)

    # Save analysis passes. type_distribution is saved here (not skipped) so future
    # delta runs can reconstruct ad count from the DB.
    for pass_name, result in analysis.items():
        if pass_name == "type_map":
            continue
        db.save_analysis(advertiser, pass_name, result)

    console.print(f"[green]✓[/green] All 7 analysis passes complete\n")

    # -----------------------------------------------------------------------
    # Phase 3 — Generate brand DNA
    # -----------------------------------------------------------------------
    console.rule("[bold bright_blue]Phase 3 — Generating Brand DNA")

    from analysis.agent import _is_real_copy
    import re as _re
    def _is_byline(headline: str) -> bool:
        return bool(_re.match(r'^[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2}\s+—', headline))
    sorted_ads = sorted(
        [
            a for a in client_ads
            if (_is_real_copy(a.get("headline", "")) or _is_real_copy(a.get("primary_text", "")))
            and not _is_byline(a.get("headline", ""))
            and not (
                a.get("headline", "").strip().lower() == advertiser.lower()
                and not _is_real_copy(a.get("primary_text", ""))
            )
        ],
        key=lambda a: _parse_impressions(a.get("impressions_range")),
        reverse=True,
    )
    seen_content: set[tuple[str, str]] = set()
    deduped = []
    for a in sorted_ads:
        key = (a.get("headline", "").strip(), a.get("primary_text", "")[:100].strip())
        if key not in seen_content:
            seen_content.add(key)
            deduped.append(a)
    sample_ads = deduped[:20]

    today = date.today().isoformat()
    slug = advertiser.lower().replace(" ", "-").replace(".", "")
    output_path = Path("outputs") / f"{slug}-brand-dna-{today}.md"
    output_path.parent.mkdir(exist_ok=True)

    console.print(f"  Building Brand DNA from analysis data...")
    content = await md_generator.generate(
        advertiser, platforms, total_client, analysis, sample_ads,
        date=today, prev_analysis=prev_analysis or {},
    )

    output_path.write_text(content, encoding="utf-8")
    db.save_brand_dna(advertiser, content)

    # -----------------------------------------------------------------------
    # Delta signal report (only when a previous run exists)
    # -----------------------------------------------------------------------
    if prev_analysis:
        from generator import delta as delta_generator
        console.print(f"  [dim]Generating competitive signal delta vs {prev_date} baseline...[/dim]")
        try:
            delta_content = await delta_generator.generate(
                advertiser=advertiser,
                prev_analysis=prev_analysis,
                curr_analysis={**analysis, "type_distribution": analysis.get("type_distribution", {})},
                prev_date=prev_date,
                curr_date=today,
                prev_ads=prev_ad_count,
                curr_ads=total_client,
            )
            delta_path = Path("outputs") / f"{slug}-intel-signal-{today}.md"
            delta_path.write_text(delta_content, encoding="utf-8")
            db.save_intel_signal(advertiser, delta_content)
            console.print(f"  [dim]Signal delta written to {delta_path}[/dim]")
        except Exception as exc:
            console.print(f"  [yellow]Warning: delta generation failed ({exc!r}) — skipping[/yellow]")

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    console.print()
    console.rule("[bold bright_blue]Complete")

    summary = Table(show_header=False, box=None)
    summary.add_column("Key", style="dim", min_width=24)
    summary.add_column("Value", style="white")
    summary.add_row("Advertiser analyzed", advertiser)
    summary.add_row("Competitors analyzed", str(len(competitors)))
    summary.add_row(f"{advertiser} ads", str(total_client))
    summary.add_row("Competitor ads", str(total_competitor))
    summary.add_row("Platforms", ", ".join(platforms))
    summary.add_row("Analysis passes", "7")
    summary.add_row("Output file", str(output_path))
    console.print(summary)
    console.print()
    console.print(f"[green bold]✓[/green bold] Brand DNA written to [bold cyan]{output_path}[/bold cyan]")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not os.environ.get("GEMINI_API_KEY"):
        console.print(
            "[red]Error:[/red] GEMINI_API_KEY is not set. "
            "Add it to your .env file — get one at https://aistudio.google.com/apikey"
        )
        raise SystemExit(1)

    parser = argparse.ArgumentParser(
        description="Campaign Intelligence Agent — scrape, analyze, and profile advertising strategy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --advertiser "Notion" --competitors "Coda" "Monday.com" --platforms meta google linkedin
  python main.py --advertiser "Notion" --platforms meta
        """,
    )
    parser.add_argument(
        "--advertiser",
        required=True,
        help="Primary advertiser to analyze (e.g. 'Notion')",
    )
    parser.add_argument(
        "--competitors",
        nargs="*",
        default=[],
        metavar="NAME",
        help="Competitor advertiser names (space-separated)",
    )
    parser.add_argument(
        "--platforms",
        nargs="*",
        default=["google", "linkedin"],
        choices=["meta", "google", "linkedin"],
        help="Platforms to scrape (default: google linkedin). meta requires Ad Library API approval.",
    )
    parser.add_argument(
        "--scenario",
        default=None,
        metavar="TEXT",
        help=(
            "Scenario context for synthetic ad generation, e.g. "
            "'promoting a new free trial tier targeting operations teams' (1-2 sentences). "
            "If omitted, scenario is derived from the brand's observed voice."
        ),
    )
    args = parser.parse_args()
    if not args.competitors:
        import sys as _sys
        print("Note: no --competitors specified — competitive gap analysis will be skipped.", file=_sys.stderr)
    asyncio.run(run(args.advertiser, args.competitors, args.platforms, scenario=args.scenario))


if __name__ == "__main__":
    main()
