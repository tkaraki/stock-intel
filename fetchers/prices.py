"""Price history fetcher (yfinance) for watchlist tickers."""

import logging
import math
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import PriceHistory, WatchlistItem

log = logging.getLogger(__name__)

EXCHANGE_TZ = ZoneInfo("America/New_York")  # US market close is fixed, whatever SCHEDULER_TZ is
LIVE_TIMEOUT = 8  # hard wall-clock cap (s) on the live quote; yfinance's own timeout is not one
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="live-price")


def _closes(df: pd.DataFrame, ticker: str) -> pd.Series:
    """Close series for one ticker from a (possibly multi-ticker) yfinance frame."""
    if isinstance(df.columns, pd.MultiIndex):
        if ticker not in df.columns.get_level_values(0):
            return pd.Series(dtype=float)
        df = df[ticker]
    if "Close" not in df.columns:
        return pd.Series(dtype=float)
    return df["Close"].dropna()


def close_time(day: date) -> datetime:
    """Market close (16:00 exchange time) of a trading day, as UTC."""
    local = datetime.combine(day, time(16, 0), tzinfo=EXCHANGE_TZ)
    return local.astimezone(UTC)


def live_price(ticker: str) -> tuple[float, datetime] | None:
    """Latest 1-minute close and its UTC bar time; None on any failure (never raises).

    Outside market hours this is the previous session's final bar.
    """
    try:
        future = _pool.submit(
            yf.download,
            ticker, period="1d", interval="1m", group_by="ticker", progress=False,
            auto_adjust=True, timeout=5,
        )
        try:
            df = future.result(timeout=LIVE_TIMEOUT)
        except FutureTimeout:
            log.warning("prices: live price for %s timed out", ticker)
            return None
        if df is None or df.empty:
            log.warning("prices: no live data for %s", ticker)
            return None
        closes = _closes(df, ticker)
        if closes.empty:
            log.warning("prices: no live data for %s", ticker)
            return None
        ts = pd.Timestamp(closes.index[-1])
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        price = float(closes.iloc[-1])
        if not math.isfinite(price) or price <= 0:
            log.warning("prices: invalid live price %r for %s", price, ticker)
            return None
        return price, ts.to_pydatetime()
    except Exception:
        log.warning("prices: live price failed for %s", ticker, exc_info=True)
        return None


def run(session: Session) -> int:
    """Store new daily closes for the watchlist; backfill missing entry prices."""
    saved = 0
    try:
        items = list(session.scalars(select(WatchlistItem)))
        if not items:
            return 0
        tickers = [i.ticker for i in items]
        df = yf.download(
            tickers, period="5d", group_by="ticker", progress=False, auto_adjust=True, timeout=10
        )
        if df is None or df.empty:
            log.warning("prices: yfinance returned no data")
            return 0
        for item in items:
            closes = _closes(df, item.ticker)
            if closes.empty:
                log.warning("prices: no data for %s", item.ticker)
                continue
            existing = {
                row.date: row
                for row in session.scalars(
                    select(PriceHistory).where(PriceHistory.ticker == item.ticker)
                )
            }
            latest_day = closes.index[-1].date()
            for ts, close in closes.items():
                day = ts.date()
                if day in existing:
                    # a mid-day run stores a partial close; refresh the latest day
                    if day == latest_day:
                        existing[day].close = float(close)
                    continue
                session.add(PriceHistory(ticker=item.ticker, date=day, close=float(close)))
                saved += 1
            if item.entry_price is None:
                item.entry_price = float(closes.iloc[-1])
                item.entry_at = close_time(latest_day)
        session.commit()
    except Exception:
        log.exception("prices fetcher failed")
        session.rollback()
        return 0
    log.info("prices: saved %d rows", saved)
    return saved
