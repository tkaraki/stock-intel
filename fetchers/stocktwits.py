"""StockTwits trending-symbol and bull/bear sentiment fetcher (no API key needed)."""

import logging
from datetime import UTC, datetime

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from fetchers.holdings import normalize_ticker
from models import Signal

log = logging.getLogger(__name__)

BASE_URL = "https://api.stocktwits.com/api/2"
HEADERS = {"User-Agent": "stock-intel/0.1 (personal research tool)"}
TIMEOUT = 10


class _Blocked(Exception):
    """StockTwits refused the request (403 / 429)."""


def _get(path: str) -> dict:
    resp = requests.get(f"{BASE_URL}{path}", headers=HEADERS, timeout=TIMEOUT)
    if resp.status_code in (403, 429):
        raise _Blocked(f"HTTP {resp.status_code} for {path}")
    resp.raise_for_status()
    return resp.json()


def _bull_bear(messages: list[dict]) -> tuple[int, int]:
    bull = bear = 0
    for m in messages:
        sentiment = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
        if sentiment == "Bullish":
            bull += 1
        elif sentiment == "Bearish":
            bear += 1
    return bull, bear


def run(session: Session) -> int:
    """Save at most one trending Signal per symbol per UTC day (refreshed in place).

    Returns rows touched (inserted or updated).
    """
    saved = 0
    now = datetime.now(UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        symbols = [s["symbol"] for s in _get("/trending/symbols.json").get("symbols", [])]
        for sym in symbols:
            data = _get(f"/streams/symbol/{sym}.json")
            messages = data.get("messages", [])
            bull, bear = _bull_bear(messages)
            total = bull + bear
            ticker = normalize_ticker(sym)
            if not ticker:
                continue
            sentiment = (bull - bear) / total if total else None
            existing = session.scalar(
                select(Signal).where(
                    Signal.source == "stocktwits",
                    Signal.ticker == ticker,
                    Signal.observed_at >= day_start,
                )
            )
            if existing:
                existing.raw_metric = float(len(messages))
                existing.sentiment = sentiment
                existing.payload = {"bull": bull, "bear": bear}
                existing.observed_at = now
                existing.score = None  # rescore recomputes
            else:
                session.add(
                    Signal(
                        ticker=ticker,
                        source="stocktwits",
                        signal_type="trending",
                        raw_metric=float(len(messages)),
                        sentiment=sentiment,
                        payload={"bull": bull, "bear": bear},
                        observed_at=now,
                    )
                )
            saved += 1
    except _Blocked as exc:
        log.warning("stocktwits blocked or rate limited: %s", exc)
    except Exception:
        log.exception("stocktwits fetcher failed")
    try:
        session.commit()
    except Exception:
        log.exception("stocktwits commit failed")
        session.rollback()
        return 0
    log.info("stocktwits: saved %d signals", saved)
    return saved
