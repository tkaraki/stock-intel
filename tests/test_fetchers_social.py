"""Social/market fetchers against canned payloads; no network."""

import logging
import time
from types import SimpleNamespace

import pandas as pd
import pytest
import requests
from sqlalchemy import select

import config
from fetchers import apewisdom, news, prices, reddit, stocktwits
from fetchers.tickers import KNOWN_TICKERS, extract_tickers
from models import NewsItem, PriceHistory, Signal, WatchlistItem
from scoring import SOURCE_WEIGHTS


class FakeResp:
    def __init__(self, payload=None, status=200):
        self._payload, self.status_code = payload or {}, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._payload


def signals(session, source):
    return list(session.scalars(select(Signal).where(Signal.source == source)))


# --- tickers ---


def test_extract_tickers_basic_and_cashtag():
    assert extract_tickers("NVDA and $GME to the moon, also TSLA!") == {"NVDA", "GME", "TSLA"}


def test_extract_tickers_stopwords_need_cashtag():
    assert extract_tickers("YOLO ALL IN, CEO says AI is BIG") == set()
    assert extract_tickers("$NOW and $ALL are up") == {"NOW", "ALL"}


def test_extract_tickers_unknown_and_lowercase():
    assert extract_tickers("ZZZZ nvda Hello") == set()
    assert extract_tickers("") == set()
    assert {"GME", "AMC", "PLTR", "SOFI", "HOOD", "SNDK", "MU", "NVDA"} <= KNOWN_TICKERS


# --- reddit ---


def fake_reddit(posts_by_sub):
    def subreddit(name):
        return SimpleNamespace(top=lambda time_filter, limit: iter(posts_by_sub.get(name, [])))

    return SimpleNamespace(subreddit=subreddit)


@pytest.fixture
def reddit_creds(monkeypatch):
    monkeypatch.setattr(config, "REDDIT_CLIENT_ID", "id")
    monkeypatch.setattr(config, "REDDIT_CLIENT_SECRET", "secret")


def post(pid, title, score=100, selftext=""):
    return SimpleNamespace(
        id=pid, title=title, selftext=selftext, score=score, permalink=f"/r/x/{pid}/", url="u",
        created_utc=time.time() - 3600,
    )


def test_reddit_saves_and_dedupes(session, monkeypatch, reddit_creds):
    posts = {"wallstreetbets": [post("a1", "GME and $AMC squeeze", 500), post("a2", "no tickers")]}
    monkeypatch.setattr(reddit, "_client", lambda: fake_reddit(posts))
    assert reddit.run(session) == 2
    rows = signals(session, "reddit")
    assert {r.ticker for r in rows} == {"GME", "AMC"}
    assert all(r.raw_metric == 500 and r.score is None for r in rows)
    assert rows[0].payload["subreddit"] == "wallstreetbets" and rows[0].payload["post_id"] == "a1"
    assert rows[0].observed_at.tzinfo is not None
    assert reddit.run(session) == 0
    assert len(signals(session, "reddit")) == 2


def test_reddit_missing_creds(session, monkeypatch, caplog):
    monkeypatch.setattr(config, "REDDIT_CLIENT_ID", None)
    with caplog.at_level(logging.WARNING):
        assert reddit.run(session) == 0
    assert "skipping" in caplog.text


def test_reddit_error_returns_zero(session, monkeypatch, reddit_creds, caplog):
    def boom():
        raise RuntimeError("down")

    monkeypatch.setattr(reddit, "_client", boom)
    assert reddit.run(session) == 0
    assert "reddit fetcher failed" in caplog.text


# --- stocktwits ---


def msg(s):
    return {"entities": {"sentiment": {"basic": s} if s else None}}


def test_stocktwits_saves(session, monkeypatch):
    def get(url, **kw):
        if url.endswith("trending/symbols.json"):
            return FakeResp({"symbols": [{"symbol": "GME"}]})
        return FakeResp({"messages": [msg("Bullish"), msg("Bullish"), msg("Bearish"), msg(None)]})

    monkeypatch.setattr(stocktwits.requests, "get", get)
    assert stocktwits.run(session) == 1
    (row,) = signals(session, "stocktwits")
    assert row.ticker == "GME" and row.raw_metric == 4
    assert row.sentiment == pytest.approx(1 / 3)
    assert row.payload == {"bull": 2, "bear": 1}


def test_stocktwits_no_sentiment_is_none(session, monkeypatch):
    def get(url, **kw):
        if url.endswith("trending/symbols.json"):
            return FakeResp({"symbols": [{"symbol": "AMC"}]})
        return FakeResp({"messages": [msg(None)]})

    monkeypatch.setattr(stocktwits.requests, "get", get)
    assert stocktwits.run(session) == 1
    assert signals(session, "stocktwits")[0].sentiment is None


@pytest.mark.parametrize("status", [403, 429])
def test_stocktwits_blocked(session, monkeypatch, caplog, status):
    monkeypatch.setattr(stocktwits.requests, "get", lambda url, **kw: FakeResp(status=status))
    assert stocktwits.run(session) == 0
    assert "stocktwits" in caplog.text


def test_stocktwits_network_error(session, monkeypatch, caplog):
    def boom(url, **kw):
        raise requests.ConnectionError("x")

    monkeypatch.setattr(stocktwits.requests, "get", boom)
    assert stocktwits.run(session) == 0
    assert "stocktwits fetcher failed" in caplog.text


# --- prices ---


def frame(tickers, closes):
    idx = pd.to_datetime(["2026-01-05", "2026-01-06"])
    cols = pd.MultiIndex.from_product([tickers, ["Close"]])
    return pd.DataFrame({(t, "Close"): closes[t] for t in tickers}, index=idx, columns=cols)


def test_prices_upsert_and_entry_price(session, monkeypatch):
    session.add_all([WatchlistItem(ticker="NVDA"), WatchlistItem(ticker="MU", entry_price=50.0)])
    session.commit()
    df = frame(["NVDA", "MU"], {"NVDA": [100.0, 110.0], "MU": [60.0, 61.0]})
    monkeypatch.setattr(prices.yf, "download", lambda *a, **k: df)
    assert prices.run(session) == 4
    assert prices.run(session) == 0
    assert len(list(session.scalars(select(PriceHistory)))) == 4
    items = {i.ticker: i for i in session.scalars(select(WatchlistItem))}
    assert items["NVDA"].entry_price == 110.0
    assert items["MU"].entry_price == 50.0


def test_prices_error_returns_zero(session, monkeypatch, caplog):
    session.add(WatchlistItem(ticker="NVDA"))
    session.commit()

    def boom(*a, **k):
        raise RuntimeError("yf down")

    monkeypatch.setattr(prices.yf, "download", boom)
    assert prices.run(session) == 0
    assert "prices fetcher failed" in caplog.text


# --- news ---

ARTICLES = {
    "articles": [
        {
            "title": "NVDA delivers wonderful, amazing record growth",
            "url": "http://x/1",
            "publishedAt": "2026-01-05T12:00:00Z",
            "source": {"name": "Wire"},
        },
        {"title": "Terrible awful crash", "url": "http://x/2", "publishedAt": None},
    ]
}


def test_news_saves_news_and_signal_and_dedupes(session, monkeypatch):
    monkeypatch.setattr(config, "NEWSAPI_KEY", "k")
    session.add(WatchlistItem(ticker="NVDA"))
    session.commit()
    monkeypatch.setattr(news.requests, "get", lambda url, **kw: FakeResp(ARTICLES))
    assert news.run(session) == 2
    items = {n.url: n for n in session.scalars(select(NewsItem))}
    assert items["http://x/1"].sentiment > 0 > items["http://x/2"].sentiment
    sigs = signals(session, "news")
    assert len(sigs) == 2 and all(s.score is None for s in sigs)
    pos = next(s for s in sigs if s.payload["url"] == "http://x/1")
    assert pos.raw_metric == pytest.approx(abs(pos.sentiment) * 10)
    assert news.run(session) == 0
    assert len(signals(session, "news")) == 2


def test_news_missing_key(session, monkeypatch, caplog):
    monkeypatch.setattr(config, "NEWSAPI_KEY", None)
    with caplog.at_level(logging.WARNING):
        assert news.run(session) == 0
    assert "skipping" in caplog.text


def test_news_network_error(session, monkeypatch, caplog):
    monkeypatch.setattr(config, "NEWSAPI_KEY", "k")
    session.add(WatchlistItem(ticker="NVDA"))
    session.commit()

    def boom(url, **kw):
        raise requests.Timeout("slow")

    monkeypatch.setattr(news.requests, "get", boom)
    assert news.run(session) == 0
    assert "news fetch failed" in caplog.text


# --- apewisdom ---


def test_apewisdom_fetch_and_run(session, monkeypatch):
    payload = {"results": [{"ticker": "GME", "mentions": "10", "upvotes": 5, "rank": 1}]}
    monkeypatch.setattr(apewisdom.requests, "get", lambda url, **kw: FakeResp(payload))
    assert apewisdom.fetch() == [{"ticker": "GME", "mentions": 10, "upvotes": 5, "rank": 1}]
    assert apewisdom.run(session) == 0


def test_apewisdom_error(monkeypatch):
    def boom(url, **kw):
        raise requests.ConnectionError("x")

    monkeypatch.setattr(apewisdom.requests, "get", boom)
    assert apewisdom.fetch() == []


def test_sources_are_scoring_sources():
    assert {"reddit", "stocktwits", "news"} <= set(SOURCE_WEIGHTS)


def test_stocktwits_one_signal_per_day_updates_in_place(session, monkeypatch):
    state = {"msgs": [msg("Bullish")]}

    def get(url, **kw):
        if url.endswith("trending/symbols.json"):
            return FakeResp({"symbols": [{"symbol": "BRK-B"}]})
        return FakeResp({"messages": state["msgs"]})

    monkeypatch.setattr(stocktwits.requests, "get", get)
    assert stocktwits.run(session) == 1
    (row,) = signals(session, "stocktwits")
    assert row.ticker == "BRK.B"
    row.score = 5.0
    session.commit()
    state["msgs"] = [msg("Bearish"), msg("Bearish")]
    stocktwits.run(session)
    (row,) = signals(session, "stocktwits")
    assert row.raw_metric == 2 and row.sentiment == -1.0 and row.score is None
    assert row.payload == {"bull": 0, "bear": 2}


def test_stocktwits_new_day_new_signal(session, monkeypatch):
    from datetime import UTC, datetime, timedelta

    session.add(Signal(ticker="GME", source="stocktwits", signal_type="trending", raw_metric=1.0,
                       observed_at=datetime.now(UTC) - timedelta(days=1, minutes=1)))
    session.commit()

    def get(url, **kw):
        if url.endswith("trending/symbols.json"):
            return FakeResp({"symbols": [{"symbol": "GME"}]})
        return FakeResp({"messages": [msg(None)]})

    monkeypatch.setattr(stocktwits.requests, "get", get)
    stocktwits.run(session)
    assert len(signals(session, "stocktwits")) == 2


def test_reddit_updates_upvotes_and_ignores_old(session, monkeypatch, reddit_creds):
    from datetime import UTC, datetime, timedelta

    old = Signal(ticker="OLD", source="reddit", signal_type="mention", raw_metric=1.0,
                 observed_at=datetime.now(UTC) - timedelta(days=30), payload={"post_id": "z"})
    session.add(old)
    session.commit()
    assert ("z", "OLD") not in reddit._seen(session)
    posts = {"wallstreetbets": [post("a1", "GME", 500)]}
    monkeypatch.setattr(reddit, "_client", lambda: fake_reddit(posts))
    assert reddit.run(session) == 1
    row = next(r for r in signals(session, "reddit") if r.ticker == "GME")
    row.score = 3.0
    session.commit()
    posts["wallstreetbets"] = [post("a1", "GME", 900)]
    assert reddit.run(session) == 0
    row = next(r for r in signals(session, "reddit") if r.ticker == "GME")
    assert row.raw_metric == 900 and row.score is None


def test_prices_refreshes_latest_close(session, monkeypatch):
    session.add(WatchlistItem(ticker="NVDA", entry_price=1.0))
    session.commit()
    monkeypatch.setattr(prices.yf, "download",
                        lambda *a, **k: frame(["NVDA"], {"NVDA": [100.0, 110.0]}))
    prices.run(session)
    monkeypatch.setattr(prices.yf, "download",
                        lambda *a, **k: frame(["NVDA"], {"NVDA": [99.0, 115.0]}))
    assert prices.run(session) == 0
    closes = {p.date.isoformat(): p.close for p in session.scalars(select(PriceHistory))}
    assert closes == {"2026-01-05": 100.0, "2026-01-06": 115.0}  # only latest day refreshed


def test_news_rejects_non_http_urls_and_dedupes_per_ticker(session, monkeypatch):
    monkeypatch.setattr(config, "NEWSAPI_KEY", "k")
    session.add_all([WatchlistItem(ticker="NVDA"), WatchlistItem(ticker="MU")])
    session.commit()
    payload = {"articles": [
        {"title": "Good", "url": "https://x/ok", "publishedAt": "2026-01-05T12:00:00Z"},
        {"title": "Evil", "url": "javascript:alert(1)", "publishedAt": None},
        {"title": "Evil2", "url": "data:text/html,hi", "publishedAt": None},
    ]}
    monkeypatch.setattr(news.requests, "get", lambda url, **kw: FakeResp(payload))
    assert news.run(session) == 2  # same article stored once per ticker
    rows = list(session.scalars(select(NewsItem)))
    assert {(n.ticker, n.url) for n in rows} == {("NVDA", "https://x/ok"), ("MU", "https://x/ok")}
    assert news.run(session) == 0
