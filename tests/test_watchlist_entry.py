"""Watchlist entry price/time: pin sources, schema migration, rendering."""

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, inspect, text

import db
import queries
from app import create_app
from models import PriceHistory, WatchlistItem


def get_item(session, ticker="NVDA"):
    return session.query(WatchlistItem).filter_by(ticker=ticker).one()


def test_pin_uses_live_price(session, monkeypatch):
    when = datetime(2026, 10, 12, 18, 14, tzinfo=UTC)
    monkeypatch.setattr(queries, "live_price", lambda t: (185.06, when))
    session.add(PriceHistory(ticker="NVDA", date=date(2026, 10, 9), close=1.0))
    assert queries.pin(session, "NVDA") is True
    item = get_item(session)
    assert (item.entry_price, item.entry_at) == (185.06, when)


def test_pin_falls_back_to_stored_close(session):
    session.add_all([
        PriceHistory(ticker="NVDA", date=date(2026, 10, 8), close=1.0),
        PriceHistory(ticker="NVDA", date=date(2026, 10, 9), close=180.0),
    ])
    queries.pin(session, "NVDA")
    item = get_item(session)
    # 16:00 EDT on Oct 9 is 20:00 UTC
    assert (item.entry_price, item.entry_at) == (180.0, datetime(2026, 10, 9, 20, 0, tzinfo=UTC))


def test_pin_without_any_data(session):
    queries.pin(session, "NVDA")
    item = get_item(session)
    assert item.entry_price is None and item.entry_at is None


def test_init_db_adds_entry_at_to_old_table():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE watchlist_items (id INTEGER PRIMARY KEY, ticker VARCHAR(16) UNIQUE, "
            "added_at DATETIME, entry_price FLOAT)"
        ))
        conn.execute(text(
            "INSERT INTO watchlist_items (ticker, added_at, entry_price) "
            "VALUES ('OLD', '2026-01-01 00:00:00', 12.5)"
        ))
    for _ in range(2):  # second run proves idempotence
        db.init_db(engine)
    assert "entry_at" in {c["name"] for c in inspect(engine).get_columns("watchlist_items")}
    with engine.connect() as conn:
        row = conn.execute(text("SELECT ticker, entry_price, entry_at FROM watchlist_items")).one()
    assert tuple(row) == ("OLD", 12.5, None)
    engine.dispose()


def test_format_entry_at():
    assert queries.format_entry_at(datetime(2026, 10, 12, 18, 14, tzinfo=UTC)) == (
        "Oct 12, 2:14 PM EDT"
    )
    assert queries.format_entry_at(None) == ""


def test_watchlist_template_states():
    app = create_app({"TESTING": True, "DATABASE_URL": "sqlite:///:memory:"})
    s = app.extensions["db_session"]()
    s.add_all([
        WatchlistItem(ticker="AAA", entry_price=100.0,
                      entry_at=datetime(2026, 10, 12, 18, 14, tzinfo=UTC)),
        WatchlistItem(ticker="BBB", entry_price=50.0),
        WatchlistItem(ticker="CCC"),
        PriceHistory(ticker="AAA", date=date(2026, 10, 12), close=103.2),
    ])
    s.commit()
    html = app.test_client().get("/watchlist").get_data(as_text=True)
    assert "entry $100.00 &middot; Oct 12, 2:14 PM EDT" in html
    assert "+3.2% since entry" in html
    assert "entry $50.00</span>" in html
    assert "entry pending (set after next close)" in html


def test_since_entry_waits_for_a_close_after_a_mid_session_entry(session):
    # Pinned at 2:14 PM EDT Oct 12; the latest stored close is Oct 9's, from before the entry.
    item = WatchlistItem(ticker="NVDA", entry_price=185.0,
                         entry_at=datetime(2026, 10, 12, 18, 14, tzinfo=UTC))
    old = PriceHistory(ticker="NVDA", date=date(2026, 10, 9), close=170.0)
    assert queries.since_entry_pct(item, old) is None
    same_day = PriceHistory(ticker="NVDA", date=date(2026, 10, 12), close=203.5)
    assert queries.since_entry_pct(item, same_day) == pytest.approx(10.0)


def test_since_entry_without_entry_time_uses_latest_close(session):
    item = WatchlistItem(ticker="NVDA", entry_price=100.0)
    row = PriceHistory(ticker="NVDA", date=date(2026, 10, 9), close=110.0)
    assert queries.since_entry_pct(item, row) == pytest.approx(10.0)
    assert queries.since_entry_pct(item, None) is None


def test_watchlist_shows_pending_until_next_close(monkeypatch):
    app = create_app({"TESTING": True, "DATABASE_URL": "sqlite:///:memory:"})
    when = datetime(2026, 10, 12, 18, 14, tzinfo=UTC)
    monkeypatch.setattr(queries, "live_price", lambda t: (185.06, when))
    client = app.test_client()
    client.post("/watchlist/pin", json={"ticker": "ZZZZ"})
    assert "% updates after next close" in client.get("/watchlist").get_data(as_text=True)
