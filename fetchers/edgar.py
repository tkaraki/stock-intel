"""SEC EDGAR 13F-HR fetcher: institutional holdings snapshots and quarter-over-quarter changes."""

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

import config
from fetchers.holdings import Holding, aggregate, save_snapshot
from models import Portfolio

log = logging.getLogger(__name__)

DATA_DIR = config.BASE_DIR / "data"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
TICKERS_CACHE = DATA_DIR / "company_tickers.json"
TICKERS_MAX_AGE = 7 * 24 * 3600
TIMEOUT = 30
FILINGS_PER_MANAGER = 2

_SUFFIXES = {
    "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED", "PLC",
    "LLC", "LP", "NV", "SA", "AG", "HOLDINGS", "HLDGS", "GROUP", "COM", "NEW", "SHS", "ORD",
    "ADR", "ADS", "SPONSORED", "THE", "CL", "CLASS", "A", "B", "C",
}  # fmt: skip


def normalize_name(name: str) -> str:
    """Upper-case issuer name without punctuation, share-class text or corporate suffixes."""
    text = re.sub(r"[^A-Z0-9 ]", " ", name.upper().replace("&", " AND "))
    tokens = text.split()
    while tokens and tokens[0] == "THE":
        tokens.pop(0)
    while tokens and tokens[-1] in _SUFFIXES and len(tokens) > 1:
        tokens.pop()
    return " ".join(tokens)


def build_ticker_map(company_tickers: dict) -> dict[str, str]:
    """normalized issuer name -> ticker from SEC company_tickers.json (first entry wins)."""
    out: dict[str, str] = {}
    for entry in company_tickers.values():
        key = normalize_name(str(entry.get("title", "")))
        ticker = str(entry.get("ticker", "")).upper()
        if key and ticker:
            out.setdefault(key, ticker)
    return out


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def parse_period_of_report(submission: str) -> date | None:
    m = re.search(r"CONFORMED PERIOD OF REPORT:\s*(\d{8})", submission)
    return datetime.strptime(m.group(1), "%Y%m%d").date() if m else None


def parse_filing_date(submission: str) -> date | None:
    m = re.search(r"FILED AS OF DATE:\s*(\d{8})", submission)
    return datetime.strptime(m.group(1), "%Y%m%d").date() if m else None


def parse_info_table(submission: str, ticker_map: dict[str, str] | None = None) -> list[Holding]:
    """Holdings from the infoTable XML inside a full-submission.txt (or bare XML).

    Duplicate cusips are aggregated; put/call option rows are ignored.
    """
    ticker_map = ticker_map or {}
    holdings: list[Holding] = []
    blocks = re.findall(r"<XML>(.*?)</XML>", submission, re.S | re.I) or [submission]
    for block in blocks:
        try:
            root = ET.fromstring(block.strip().encode("utf-8"))
        except ET.ParseError:
            continue
        for node in root.iter():
            if _local(node.tag) != "infoTable":
                continue
            f = {_local(c.tag): c for c in node.iter()}

            def text(tag: str, f=f) -> str:
                return (f[tag].text or "").strip() if tag in f else ""

            if text("putCall"):
                continue
            cusip = text("cusip").upper()
            name = text("nameOfIssuer")
            try:
                shares = float(text("sshPrnamt").replace(",", ""))
                value = float(text("value").replace(",", ""))
            except ValueError:
                continue
            if not cusip:
                continue
            holdings.append(
                Holding(cusip, name, shares, value, ticker_map.get(normalize_name(name)), cusip)
            )
    return aggregate(holdings)


def _user_agent() -> str:
    return f"{config.SEC_EDGAR_COMPANY_NAME} {config.SEC_EDGAR_EMAIL}"


def load_ticker_map(
    cache: Path = TICKERS_CACHE, max_age: float = TICKERS_MAX_AGE
) -> dict[str, str]:
    """Name->ticker map, using a cache file refreshed weekly (stale cache used on fetch failure)."""
    fresh = cache.exists() and time.time() - cache.stat().st_mtime < max_age
    if not fresh:
        try:
            resp = requests.get(TICKERS_URL, headers={"User-Agent": _user_agent()}, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(data))
        except (requests.RequestException, ValueError, OSError) as exc:
            log.warning("could not refresh SEC ticker list: %s", exc)
    try:
        return build_ticker_map(json.loads(cache.read_text()))
    except (OSError, ValueError):
        return {}


def _download(downloader, cik: str) -> list[Path]:
    """Download the latest 13F-HR filings and return all full-submission.txt paths for the CIK."""
    downloader.get("13F-HR", cik, limit=FILINGS_PER_MANAGER)
    root = DATA_DIR / "sec-edgar-filings"
    paths: list[Path] = []
    if root.exists():
        for d in root.iterdir():
            if d.name.lstrip("0") == cik.lstrip("0"):
                paths.extend((d / "13F-HR").glob("*/full-submission.txt"))
    return paths


def load_filings(
    paths: list[Path], ticker_map: dict[str, str], limit: int = FILINGS_PER_MANAGER
) -> list[tuple[date, list[Holding], date | None]]:
    """Parse filings, newest `limit` by period of report, returned oldest first.

    Each item is (period of report, holdings, filing date or None).
    """
    parsed = []
    for p in paths:
        text = p.read_text(errors="replace")
        as_of = parse_period_of_report(text)
        if as_of:
            parsed.append((as_of, text))
    parsed.sort(key=lambda t: t[0], reverse=True)
    return [
        (d, parse_info_table(t, ticker_map), parse_filing_date(t))
        for d, t in reversed(parsed[:limit])
    ]


def run(session: Session) -> int:
    """Fetch and store the latest 13F snapshots for tracked managers; returns rows saved."""
    if not (config.SEC_EDGAR_COMPANY_NAME and config.SEC_EDGAR_EMAIL):
        log.warning("skipping: SEC_EDGAR_COMPANY_NAME/SEC_EDGAR_EMAIL not configured")
        return 0
    saved = 0
    try:
        from sec_edgar_downloader import Downloader

        portfolios = session.scalars(
            select(Portfolio).where(Portfolio.source == "edgar", Portfolio.cik.is_not(None))
        ).all()
        if not portfolios:
            return 0
        downloader = Downloader(
            config.SEC_EDGAR_COMPANY_NAME, config.SEC_EDGAR_EMAIL, download_folder=DATA_DIR
        )
        ticker_map = load_ticker_map()
        for p in portfolios:
            try:
                paths = _download(downloader, p.cik)
                for as_of, holdings, filed in load_filings(paths, ticker_map):
                    saved += save_snapshot(
                        session, p, as_of, holdings, "portfolio_13f", observed=filed
                    )
            except Exception:
                log.exception("13F fetch failed for %s", p.name)
                session.rollback()
    except Exception:
        log.exception("EDGAR fetcher failed")
        session.rollback()
    return saved
