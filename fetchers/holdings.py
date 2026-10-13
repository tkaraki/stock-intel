"""Shared portfolio logic: save a holdings snapshot, diff vs the previous one, emit signals."""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import Portfolio, PortfolioHolding, Signal
from portfolio_diff import ChangeType, diff_holdings

log = logging.getLogger(__name__)


def normalize_ticker(ticker: str | None) -> str | None:
    """Upper-case and map class-share dashes to the app's dotted form (BRK-B -> BRK.B)."""
    if not ticker:
        return None
    return ticker.strip().upper().replace("-", ".") or None


@dataclass
class Holding:
    security_key: str  # cusip if present, else ticker
    name: str
    shares: float
    value: float
    ticker: str | None = None
    cusip: str | None = None


def aggregate(holdings: list[Holding]) -> list[Holding]:
    """Merge holdings sharing a security_key (shares and value summed)."""
    merged: dict[str, Holding] = {}
    for h in holdings:
        cur = merged.get(h.security_key)
        if cur is None:
            merged[h.security_key] = Holding(
                h.security_key, h.name, h.shares, h.value, h.ticker, h.cusip
            )
        else:
            cur.shares += h.shares
            cur.value += h.value
            cur.ticker = cur.ticker or h.ticker
    return list(merged.values())


def _shares_and_tickers(rows) -> tuple[dict[str, float], dict[str, str | None]]:
    shares = {r.security_key: r.shares for r in rows}
    tickers = {r.security_key: r.ticker for r in rows}
    return shares, tickers


def save_snapshot(
    session: Session,
    portfolio: Portfolio,
    as_of: date,
    holdings: list[Holding],
    signal_source: str,
    observed: date | None = None,
) -> int:
    """Store a snapshot for `as_of` if new, diff it against the latest earlier snapshot and
    emit one Signal per material change that has a ticker. Returns holding rows saved.

    Signals are stamped at 00:00 UTC on `observed` (the filing date; defaults to `as_of`) so
    backfilled changes age through decay instead of looking fresh.

    Nothing is emitted for a portfolio's first-ever snapshot (every position would be NEW) or
    when `as_of` is already stored, so re-running never duplicates signals.
    """
    exists = session.scalar(
        select(func.count())
        .select_from(PortfolioHolding)
        .where(PortfolioHolding.portfolio_id == portfolio.id, PortfolioHolding.as_of == as_of)
    )
    for h in holdings:
        h.ticker = normalize_ticker(h.ticker)
    holdings = aggregate(holdings)
    if exists or not holdings:
        return 0

    prior_as_of = session.scalar(
        select(func.max(PortfolioHolding.as_of)).where(
            PortfolioHolding.portfolio_id == portfolio.id, PortfolioHolding.as_of < as_of
        )
    )
    prior_rows = (
        session.scalars(
            select(PortfolioHolding).where(
                PortfolioHolding.portfolio_id == portfolio.id,
                PortfolioHolding.as_of == prior_as_of,
            )
        ).all()
        if prior_as_of
        else []
    )

    session.add_all(
        PortfolioHolding(
            portfolio_id=portfolio.id,
            as_of=as_of,
            ticker=h.ticker,
            cusip=h.cusip,
            security_key=h.security_key,
            name=h.name[:256],
            shares=h.shares,
            value=h.value,
        )
        for h in holdings
    )

    observed_at = datetime.combine(observed or as_of, datetime.min.time(), tzinfo=UTC)
    if prior_rows:
        prior_shares, tickers = _shares_and_tickers(prior_rows)
        cur_shares = {h.security_key: h.shares for h in holdings}
        tickers.update({h.security_key: h.ticker for h in holdings if h.ticker})
        for ch in diff_holdings(prior_shares, cur_shares):
            ticker = tickers.get(ch.ticker)
            if not ticker:
                log.debug("no ticker for %s (%s); skipping signal", ch.ticker, portfolio.name)
                continue
            is_flip = ch.change_type in (ChangeType.NEW, ChangeType.EXIT)
            session.add(
                Signal(
                    ticker=ticker,
                    source=signal_source,
                    signal_type=ch.change_type.value,
                    raw_metric=100.0 if is_flip else abs(ch.pct_change or 0.0) * 100,
                    observed_at=observed_at,
                    payload={
                        "portfolio": portfolio.name,
                        "manager": portfolio.manager,
                        "prior_shares": ch.prior_shares,
                        "current_shares": ch.current_shares,
                        "pct_change": ch.pct_change,
                        "as_of": as_of.isoformat(),
                    },
                )
            )
    session.commit()
    return len(holdings)
