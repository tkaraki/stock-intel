"""Idempotent seed data: starter watchlist and tracked portfolios."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from models import Portfolio, WatchlistItem

WATCHLIST = ["NVDA", "MU", "SNDK", "TSLA", "AAPL"]

PORTFOLIOS = [
    ("Pershing Square", "Bill Ackman", "edgar", "0001336528"),
    ("Scion Asset Management", "Michael Burry", "edgar", "0001649339"),
    ("Duquesne Family Office", "Stanley Druckenmiller", "edgar", "0001536411"),
    ("ARK Innovation ETF", "Cathie Wood", "ark", None),
]


def seed(session: Session | None = None) -> None:
    """Insert missing watchlist items and portfolios; safe to run repeatedly."""
    own = session is None
    if own:
        from db import get_session, init_db

        init_db()
        session = get_session()
    try:
        have = set(session.scalars(select(WatchlistItem.ticker)))
        session.add_all(WatchlistItem(ticker=t) for t in WATCHLIST if t not in have)
        names = set(session.scalars(select(Portfolio.name)))
        for name, manager, source, cik in PORTFOLIOS:
            if name not in names:
                session.add(Portfolio(name=name, manager=manager, source=source, cik=cik))
        session.commit()
    finally:
        if own:
            session.close()


if __name__ == "__main__":
    seed()
