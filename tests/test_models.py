from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from models import Portfolio, PortfolioHolding, PriceHistory, Signal, WatchlistItem
from seed import seed


def test_seed_idempotent(session):
    seed(session)
    seed(session)
    assert session.scalar(select(func.count()).select_from(WatchlistItem)) == 5
    assert session.scalar(select(func.count()).select_from(Portfolio)) == 4


def test_watchlist_ticker_unique(session):
    session.add_all([WatchlistItem(ticker="X"), WatchlistItem(ticker="X")])
    with pytest.raises(IntegrityError):
        session.commit()


def test_price_unique_ticker_date(session):
    d = date(2026, 1, 2)
    session.add_all([PriceHistory(ticker="X", date=d, close=1.0)] * 1)
    session.commit()
    session.add(PriceHistory(ticker="X", date=d, close=2.0))
    with pytest.raises(IntegrityError):
        session.commit()


def test_holding_unique_security(session):
    p = Portfolio(name="P", manager="M", source="edgar", cik="1")
    session.add(p)
    session.commit()

    def h():
        return PortfolioHolding(
            portfolio_id=p.id,
            as_of=date(2026, 1, 1),
            cusip="C1",
            security_key="C1",
            name="n",
            shares=1,
            value=1,
        )

    session.add(h())
    session.commit()
    session.add(h())
    with pytest.raises(IntegrityError):
        session.commit()


def test_datetimes_round_trip_as_utc(session):
    ts = datetime(2026, 1, 1, 12, tzinfo=UTC)
    session.add(Signal(ticker="X", source="s", signal_type="t", raw_metric=1.0, observed_at=ts))
    session.commit()
    session.expire_all()
    assert session.scalar(select(Signal.observed_at)) == ts
