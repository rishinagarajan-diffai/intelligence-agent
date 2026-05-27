"""Web content fetcher for RAG-based competitive landscape analysis.

Uses DuckDuckGo HTML search (no API key required) + requests to fetch
current competitor positioning context. Called by _pass_market_context()
in analysis/agent.py.

Competitor discovery is always automatic — explicit competitors are optional
overrides that add dedicated per-brand fetches on top of discovery results.
"""

import re
import requests
from bs4 import BeautifulSoup

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_TIMEOUT = 10
_MAX_CHARS = 3000  # per source


def _ddg_snippets(query: str, max_results: int = 5) -> str:
    """Search DuckDuckGo HTML and return concatenated result snippets."""
    try:
        resp = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": _UA},
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        snippets = [
            el.get_text(separator=" ", strip=True)
            for el in soup.select(".result__snippet")[:max_results]
        ]
        return " ".join(s for s in snippets if s)[:_MAX_CHARS]
    except Exception:
        return ""


def _fetch_homepage(name: str) -> str:
    """Fetch and clean homepage text for a company name."""
    slug = re.sub(r"[^a-z0-9-]", "", name.lower().replace(" ", ""))
    candidates = [
        f"https://www.{slug}.com",
        f"https://{slug}.com",
        f"https://www.{slug}.io",
        f"https://{slug}.io",
    ]
    for url in candidates:
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": _UA},
                timeout=_TIMEOUT,
                allow_redirects=True,
            )
            if resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            text = re.sub(r"\s+", " ", soup.get_text(separator=" ", strip=True))
            return text[:_MAX_CHARS]
        except Exception:
            continue
    return ""


def fetch_competitor_intel(
    advertiser: str,
    competitors: list[str],
) -> dict[str, str]:
    """
    Fetch web content for RAG-based competitive landscape analysis.

    Always runs competitor discovery searches so the market context pass
    can identify and analyze competitors automatically — no --competitors
    flag required. If explicit competitors are provided they get dedicated
    per-brand fetches on top of the discovery content.

    Returns dict: label -> raw_text_context, passed as RAG input to Gemini.
    """
    results: dict[str, str] = {}

    # Advertiser's own positioning
    advertiser_snippets = _ddg_snippets(
        f'"{advertiser}" product positioning value proposition marketing strategy'
    )
    advertiser_home = _fetch_homepage(advertiser)
    results[advertiser] = f"{advertiser_snippets}\n{advertiser_home}".strip()[:_MAX_CHARS]

    # Always run competitor discovery — two queries for breadth
    discovery = " ".join(filter(None, [
        _ddg_snippets(f'"{advertiser}" top competitors alternatives 2025 2026'),
        _ddg_snippets(f'"{advertiser}" vs comparison market landscape'),
    ]))
    if discovery.strip():
        results["__competitor_discovery__"] = discovery[:_MAX_CHARS]

    # For explicitly named competitors, fetch dedicated intel
    for comp in competitors:
        comp_snippets = _ddg_snippets(
            f'"{comp}" advertising marketing positioning what they are known for'
        )
        comp_home = _fetch_homepage(comp)
        results[comp] = f"{comp_snippets}\n{comp_home}".strip()[:_MAX_CHARS]

    return results
