"""ARK Invest (ARKK) daily holdings CSV fetcher."""

import csv
import io
import logging
from datetime import date, datetime

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

import config
from fetchers.holdings import Holding, save_snapshot
from models import Portfolio

log = logging.getLogger(__name__)

DEFAULT_URLS = [
    "https://assets.ark-funds.com/fund-documents/funds-etf-csv/ARK_INNOVATION_ETF_ARKK_HOLDINGS.csv",
    "https://ark-funds.com/wp-content/uploads/funds-etf-csv/ARK_INNOVATION_ETF_ARKK_HOLDINGS.csv",
]
# ARK returns 403 to default client user agents.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/csv,*/*",
}
TIMEOUT = 20


def _num(text: str) -> float:
    return float(text.replace("$", "").replace(",", "").replace("%", "").strip() or 0)


def parse_ark_csv(text: str) -> tuple[date, list[Holding]] | None:
    """Parse the ARK holdings CSV into (as_of, holdings); None if no data rows are found.

    Rows whose first column is not a M/D/YYYY date (header, blank lines, the trailing
    disclaimer) are ignored.
    """
    rows = []
    for rec in csv.reader(io.StringIO(text.lstrip("﻿"))):
        if len(rec) < 7:
            continue
        try:
            as_of = datetime.strptime(rec[0].strip(), "%m/%d/%Y").date()
            shares, value = _num(rec[5]), _num(rec[6])
        except ValueError:
            continue
        ticker = rec[3].strip().upper() or None
        cusip = rec[4].strip().upper() or None
        key = cusip or ticker
        if not key:
            continue
        rows.append((as_of, Holding(key, rec[2].strip(), shares, value, ticker, cusip)))
    if not rows:
        return None
    latest = max(r[0] for r in rows)
    return latest, [h for d, h in rows if d == latest]


def _download(urls: list[str]) -> str | None:
    for url in urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            if resp.status_code == 200 and resp.text.lstrip("﻿").lstrip().lower().startswith(
                "date"
            ):
                return resp.text
            log.warning("ARK CSV %s unusable (HTTP %s)", url, resp.status_code)
        except requests.RequestException as exc:
            log.warning("ARK CSV %s failed: %s", url, exc)
    return None


def run(session: Session) -> int:
    """Fetch ARKK holdings and store a new snapshot; returns holding rows saved."""
    try:
        portfolios = session.scalars(select(Portfolio).where(Portfolio.source == "ark")).all()
        if not portfolios:
            return 0
        urls = ([config.ARK_ARKK_CSV_URL] if config.ARK_ARKK_CSV_URL else []) + DEFAULT_URLS
        text = _download(urls)
        parsed = parse_ark_csv(text) if text else None
        if parsed is None:
            log.error("ARK holdings: no usable data")
            return 0
        as_of, holdings = parsed
        return sum(
            save_snapshot(session, p, as_of, holdings, "portfolio_ark") for p in portfolios
        )
    except Exception:
        log.exception("ARK fetcher failed")
        session.rollback()
        return 0
