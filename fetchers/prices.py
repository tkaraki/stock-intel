"""Price history fetcher (yfinance) for watchlist tickers."""

import logging

import pandas as pd
import yfinance as yf
from sqlalchemy import select
from sqlalchemy.orm import Session

from models import PriceHistory, WatchlistItem

log = logging.getLogger(__name__)


def _closes(df: pd.DataFrame, ticker: str) -> pd.Series:
    """Close series for one ticker from a (possibly multi-ticker) yfinance frame."""
    if isinstance(df.columns, pd.MultiIndex):
        if ticker not in df.columns.get_level_values(0):
            return pd.Series(dtype=float)
        df = df[ticker]
    if "Close" not in df.columns:
        return pd.Series(dtype=float)
    return df["Close"].dropna()


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
        session.commit()
    except Exception:
        log.exception("prices fetcher failed")
        session.rollback()
        return 0
    log.info("prices: saved %d rows", saved)
    return saved
