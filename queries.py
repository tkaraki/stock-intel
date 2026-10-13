"""Read-side query helpers for the dashboard. Every query is parameterised via SQLAlchemy."""

import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import config
import scoring
import summaries
from models import (
    CongressTrade,
    NewsItem,
    Portfolio,
    PortfolioHolding,
    PriceHistory,
    Signal,
    WatchlistItem,
)
from portfolio_diff import HoldingChange, diff_holdings

TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")
PAGE_SIZE = 50


def valid_ticker(value: str | None) -> bool:
    return bool(value) and TICKER_RE.fullmatch(value) is not None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _rank(signals: list[Signal], now: datetime) -> dict:
    score, sources = scoring.aggregate_ticker_score(signals, now)
    converging = scoring.converging_sources(signals, now)
    return {
        "score": score,
        "sources": sorted(sources),
        "converging": sorted(converging),
        "signals": sorted(signals, key=lambda s: s.observed_at, reverse=True),
    }


def feed(session: Session, source: str | None = None, now: datetime | None = None) -> list[dict]:
    """Tickers with signals in the last 24h, ranked by aggregate score (desc).

    The source filter narrows which signals are shown and scored.
    """
    now = now or _utcnow()
    stmt = select(Signal).where(Signal.observed_at >= now - timedelta(hours=24))
    if source:
        stmt = stmt.where(Signal.source == source)
    by_ticker: dict[str, list[Signal]] = {}
    for sig in session.scalars(stmt):
        by_ticker.setdefault(sig.ticker, []).append(sig)
    today = now.astimezone(ZoneInfo(config.SCHEDULER_TZ)).date()
    closes = recent_closes(session, list(by_ticker), today)
    rows = [
        {
            "ticker": t,
            **_rank(sigs, now),
            "summary": summaries.summarize(sigs, closes.get(t, []), now, today),
        }
        for t, sigs in by_ticker.items()
    ]
    rows.sort(key=lambda r: (-r["score"], r["ticker"]))
    return rows


def latest_close(session: Session, ticker: str) -> float | None:
    return session.scalar(
        select(PriceHistory.close)
        .where(PriceHistory.ticker == ticker)
        .order_by(PriceHistory.date.desc())
        .limit(1)
    )


def recent_closes(
    session: Session, tickers: list[str], today: date, n: int = 12
) -> dict[str, list[tuple[date, float]]]:
    """Last n (date, close) pairs per ticker, oldest first, in one query (21-day window)."""
    if not tickers:
        return {}
    rows = session.execute(
        select(PriceHistory.ticker, PriceHistory.date, PriceHistory.close)
        .where(PriceHistory.ticker.in_(tickers), PriceHistory.date >= today - timedelta(days=21))
        .order_by(PriceHistory.ticker, PriceHistory.date.asc())
    )
    out: dict[str, list[tuple[date, float]]] = {}
    for ticker, d, close in rows:
        out.setdefault(ticker, []).append((d, close))
    return {t: c[-n:] for t, c in out.items()}


def recent_news(session: Session, ticker: str, limit: int = 3) -> list[NewsItem]:
    return list(
        session.scalars(
            select(NewsItem)
            .where(NewsItem.ticker == ticker)
            .order_by(NewsItem.published_at.desc())
            .limit(limit)
        )
    )


def pnl_pct(entry: float | None, last: float | None) -> float | None:
    if not entry or last is None:
        return None
    return (last - entry) / entry * 100


def watchlist(session: Session) -> list[dict]:
    rows = []
    for item in session.scalars(select(WatchlistItem).order_by(WatchlistItem.ticker)):
        last = latest_close(session, item.ticker)
        rows.append(
            {
                "ticker": item.ticker,
                "entry_price": item.entry_price,
                "last": last,
                "pnl": pnl_pct(item.entry_price, last),
                "news": recent_news(session, item.ticker),
            }
        )
    return rows


def pin(session: Session, ticker: str) -> bool:
    """Pin a ticker; returns True if newly created."""
    if session.scalar(select(WatchlistItem.id).where(WatchlistItem.ticker == ticker)):
        return False
    session.add(WatchlistItem(ticker=ticker, entry_price=latest_close(session, ticker)))
    try:
        session.commit()
    except IntegrityError:  # concurrent pin won the race
        session.rollback()
        return False
    return True


def unpin(session: Session, ticker: str) -> bool:
    item = session.scalar(select(WatchlistItem).where(WatchlistItem.ticker == ticker))
    if item is None:
        return False
    session.delete(item)
    session.commit()
    return True


def chart_points(session: Session, ticker: str, days: int = 90) -> list[dict]:
    since = date.today() - timedelta(days=days)
    rows = session.execute(
        select(PriceHistory.date, PriceHistory.close)
        .where(PriceHistory.ticker == ticker, PriceHistory.date >= since)
        .order_by(PriceHistory.date.asc())
    )
    return [{"date": d.isoformat(), "close": c} for d, c in rows]


def congress_page(
    session: Session, ticker: str | None, member: str | None, page: int
) -> tuple[list[CongressTrade], int]:
    stmt = select(CongressTrade)
    count = select(func.count()).select_from(CongressTrade)
    if ticker:
        stmt, count = (q.where(CongressTrade.ticker == ticker) for q in (stmt, count))
    if member:
        like = f"%{member.replace('%', '').replace('_', '')}%"
        stmt, count = (q.where(CongressTrade.member.ilike(like)) for q in (stmt, count))
    total = session.scalar(count) or 0
    rows = session.scalars(
        stmt.order_by(CongressTrade.filed_date.desc(), CongressTrade.id.desc())
        .limit(PAGE_SIZE)
        .offset((max(page, 1) - 1) * PAGE_SIZE)
    )
    return list(rows), total


def _snapshot(session: Session, portfolio_id: int, as_of: date) -> list[PortfolioHolding]:
    return list(
        session.scalars(
            select(PortfolioHolding).where(
                PortfolioHolding.portfolio_id == portfolio_id, PortfolioHolding.as_of == as_of
            )
        )
    )


def portfolio_views(session: Session, top: int = 25) -> list[dict]:
    views = []
    for pf in session.scalars(select(Portfolio).order_by(Portfolio.name)):
        dates = list(
            session.scalars(
                select(PortfolioHolding.as_of)
                .where(PortfolioHolding.portfolio_id == pf.id)
                .distinct()
                .order_by(PortfolioHolding.as_of.desc())
                .limit(2)
            )
        )
        view = {"portfolio": pf, "as_of": None, "prior_as_of": None, "holdings": [],
                "changes": [], "total_value": 0.0}
        if dates:
            cur = _snapshot(session, pf.id, dates[0])
            view["as_of"] = dates[0]
            view["total_value"] = sum(h.value for h in cur)
            view["holdings"] = sorted(cur, key=lambda h: h.value, reverse=True)[:top]
            if len(dates) > 1:
                prev = _snapshot(session, pf.id, dates[1])
                view["prior_as_of"] = dates[1]
                names = {h.security_key: h.ticker or h.name for h in (*prev, *cur)}
                changes: list[HoldingChange] = diff_holdings(
                    {h.security_key: h.shares for h in prev},
                    {h.security_key: h.shares for h in cur},
                )
                view["changes"] = [(names.get(c.ticker, c.ticker), c) for c in changes]
        views.append(view)
    return views


def ticker_detail(session: Session, symbol: str, now: datetime | None = None) -> dict:
    now = now or _utcnow()
    signals = list(
        session.scalars(
            select(Signal)
            .where(Signal.ticker == symbol, Signal.observed_at >= now - timedelta(days=7))
            .order_by(Signal.observed_at.desc())
        )
    )
    rank = _rank(signals, now) if signals else None
    trades = list(
        session.scalars(
            select(CongressTrade)
            .where(CongressTrade.ticker == symbol)
            .order_by(CongressTrade.filed_date.desc())
            .limit(20)
        )
    )
    # latest snapshot per portfolio that holds the symbol
    holders = []
    for pf in session.scalars(select(Portfolio).order_by(Portfolio.name)):
        latest = session.scalar(
            select(func.max(PortfolioHolding.as_of)).where(PortfolioHolding.portfolio_id == pf.id)
        )
        if latest is None:
            continue
        hs = list(
            session.scalars(
                select(PortfolioHolding).where(
                    PortfolioHolding.portfolio_id == pf.id,
                    PortfolioHolding.as_of == latest,
                    PortfolioHolding.ticker == symbol,
                )
            )
        )
        if hs:
            holders.append(
                {"portfolio": pf, "as_of": latest,
                 "shares": sum(h.shares for h in hs), "value": sum(h.value for h in hs)}
            )
    last = latest_close(session, symbol)
    news = recent_news(session, symbol, 10)
    pinned = session.scalar(select(WatchlistItem.id).where(WatchlistItem.ticker == symbol))
    known = bool(rank or trades or holders or last is not None or news or pinned)
    return {
        "symbol": symbol, "rank": rank, "signals": rank["signals"][:20] if rank else [],
        "trades": trades, "holders": holders, "last": last, "news": news,
        "pinned": bool(pinned), "known": known,
    }
