"""ApeWisdom trending-mentions fetcher."""

import logging

import requests
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

URL = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/1"
HEADERS = {"User-Agent": "stock-intel/0.1 (personal research tool)"}
TIMEOUT = 10


def fetch() -> list[dict]:
    """Return [{ticker, mentions, upvotes, rank}, ...]; empty list on any error."""
    try:
        resp = requests.get(URL, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        return [
            {
                "ticker": r["ticker"],
                "mentions": int(r.get("mentions") or 0),
                "upvotes": int(r.get("upvotes") or 0),
                "rank": int(r.get("rank") or 0),
            }
            for r in resp.json().get("results", [])
        ]
    except Exception:
        log.exception("apewisdom fetch failed")
        return []


def run(session: Session) -> int:
    # ApeWisdom is not a scoring source yet, so this only logs and saves nothing.
    rows = fetch()
    log.info("apewisdom: fetched %d trending tickers (not stored)", len(rows))
    return 0
