"""House periodic transaction report (PTR) fetcher using the House Clerk's public disclosures."""

import io
import logging
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, date, datetime, timedelta

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import CongressTrade, Signal

log = logging.getLogger(__name__)

ZIP_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PDF_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
LOOKBACK_DAYS = 30
MAX_PDFS_PER_RUN = 50
PDF_DELAY = 0.5
TIMEOUT = 30
HEADERS = {"User-Agent": "stock-intel/0.1"}

_TX_RE = re.compile(
    r"\b(S \(partial\)|P|S|E)\s+(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+"
    r"(\$[\d,]+)\s*-\s*(?:[^$]{0,60}?)(\$[\d,]+)"  # wrapped asset text may sit mid-range
)
_TICKER_RE = re.compile(r"\(([A-Z]{1,5}(?:[.\-][A-Z]{1,2})?)\)")
_TYPES = {"P": "purchase", "S": "sale", "S (partial)": "sale_partial", "E": "exchange"}


def _parse_date(text: str) -> date:
    return datetime.strptime(text.strip(), "%m/%d/%Y").date()


def parse_fd_xml(xml_bytes: bytes, today: date, days: int = LOOKBACK_DAYS) -> list[dict]:
    """PTR filings ("P") from a {YEAR}FD.xml index filed within the last `days` days."""
    cutoff = today - timedelta(days=days)
    out = []
    for m in ET.fromstring(xml_bytes).iter("Member"):
        if (m.findtext("FilingType") or "").strip() != "P":
            continue
        try:
            filed = _parse_date(m.findtext("FilingDate") or "")
        except ValueError:
            continue
        doc_id = (m.findtext("DocID") or "").strip()
        if filed < cutoff or not doc_id:
            continue
        parts = [m.findtext(k) or "" for k in ("First", "Last", "Suffix")]
        out.append(
            {
                "doc_id": doc_id,
                "member": " ".join(p.strip() for p in parts if p.strip()),
                "state_dst": (m.findtext("StateDst") or "").strip(),
                "year": (m.findtext("Year") or str(filed.year)).strip(),
                "filed_date": filed,
            }
        )
    return out


def parse_ptr_text(text: str) -> list[dict]:
    """Transaction rows from PTR PDF text: ticker (or None), type, date and amount range."""
    flat = re.sub(r"\s+", " ", text.replace("\x00", ""))
    rows, pos = [], 0
    for m in _TX_RE.finditer(flat):
        if m.start() < pos:
            continue
        before = _TICKER_RE.findall(flat[pos : m.start()][-150:])
        ticker = before[-1] if before else None
        end = m.end()
        if ticker is None:  # asset text wrapped onto the next line, after the amounts
            after = re.match(r"\s*" + _TICKER_RE.pattern, flat[end:])
            if after:
                ticker, end = after.group(1), end + after.end()
        pos = end
        try:
            tx_date = _parse_date(m.group(2))
        except ValueError:
            continue
        rows.append(
            {
                "ticker": ticker,
                "transaction_type": _TYPES[m.group(1)],
                "transaction_date": tx_date,
                "amount_range": f"{m.group(4)} - {m.group(5)}",
                "amount_low": int(m.group(4).replace("$", "").replace(",", "")),
            }
        )
    return rows


def extract_pdf_text(pdf_bytes: bytes) -> str:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return "\n".join((page.extract_text() or "") for page in pdf.pages)


def _fetch_filings(today: date) -> list[dict]:
    filings = []
    for year in sorted({today.year, (today - timedelta(days=LOOKBACK_DAYS)).year}):
        try:
            resp = requests.get(ZIP_URL.format(year=year), headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                filings += parse_fd_xml(zf.read(f"{year}FD.xml"), today)
        except (requests.RequestException, zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
            log.warning("House %s disclosure index failed: %s", year, exc)
    return filings


def run(session: Session) -> int:
    """Fetch new House PTRs and store trades plus signals; returns CongressTrade rows saved."""
    saved = 0
    try:
        today = datetime.now(UTC).date()
        known = set(session.scalars(select(CongressTrade.doc_url)))
        todo = []
        for f in sorted(_fetch_filings(today), key=lambda f: f["filed_date"], reverse=True):
            f["doc_url"] = PDF_URL.format(year=f["year"], doc_id=f["doc_id"])
            if f["doc_url"] not in known:
                todo.append(f)
        for i, f in enumerate(todo[:MAX_PDFS_PER_RUN]):
            if i:
                time.sleep(PDF_DELAY)
            try:
                resp = requests.get(f["doc_url"], headers=HEADERS, timeout=TIMEOUT)
                resp.raise_for_status()
            except requests.RequestException as exc:
                log.warning("PTR %s download failed: %s", f["doc_id"], exc)
                continue
            try:
                rows = parse_ptr_text(extract_pdf_text(resp.content))
            except Exception as exc:
                log.warning("PTR %s unreadable: %s", f["doc_id"], exc)
                rows = []
            saved += _save_filing(session, f, rows)
    except Exception:
        log.exception("congress fetcher failed")
        session.rollback()
    return saved


def _save_filing(session: Session, f: dict, rows: list[dict]) -> int:
    if not rows:  # scanned/handwritten: keep a placeholder so the filing still appears
        rows = [
            {
                "ticker": None,
                "transaction_type": "unknown",
                "transaction_date": f["filed_date"],
                "amount_range": None,
                "amount_low": 0,
            }
        ]
    seen, count = set(), 0
    for r in rows:
        if r["ticker"] and (r["ticker"], r["transaction_date"]) in seen:
            continue  # unique per (doc, ticker, date)
        seen.add((r["ticker"], r["transaction_date"]))
        session.add(
            CongressTrade(
                member=f["member"][:128],
                chamber="house",
                ticker=r["ticker"],
                transaction_type=r["transaction_type"],
                amount_range=r["amount_range"],
                transaction_date=r["transaction_date"],
                filed_date=f["filed_date"],
                doc_url=f["doc_url"],
            )
        )
        count += 1
        if r["ticker"] and r["transaction_type"] in ("purchase", "sale", "sale_partial"):
            session.add(
                Signal(
                    ticker=r["ticker"],
                    source="congress",
                    signal_type="purchase" if r["transaction_type"] == "purchase" else "sale",
                    raw_metric=r["amount_low"] / 1000,
                    payload={"member": f["member"], "doc_url": f["doc_url"]},
                )
            )
    session.commit()
    return count
