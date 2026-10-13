from datetime import UTC, date, datetime, timedelta

import pytest

from app import create_app
from models import CongressTrade, NewsItem, PriceHistory, Signal, WatchlistItem


@pytest.fixture
def app():
    app = create_app({"TESTING": True, "DATABASE_URL": "sqlite:///:memory:"})
    s = app.extensions["db_session"]()
    now = datetime.now(UTC)

    def sig(t, src, metric, h=1):
        s.add(Signal(ticker=t, source=src, signal_type="mention", raw_metric=metric,
                     observed_at=now - timedelta(hours=h)))

    sig("AAA", "reddit", 10)
    sig("AAA", "news", 10)  # converges -> highest
    sig("BBB", "reddit", 5)
    sig("CCC", "stocktwits", 50, h=48)  # outside 24h
    s.add(CongressTrade(member="Jane Doe", chamber="house", ticker="AAA",
                        transaction_type="buy", transaction_date=date(2026, 1, 1),
                        filed_date=date(2026, 1, 5), doc_url="u1"))
    s.add(CongressTrade(member="John Roe", chamber="senate", ticker="BBB",
                        transaction_type="sell", transaction_date=date(2026, 1, 2),
                        filed_date=date(2026, 1, 9), doc_url="u2"))
    s.add(NewsItem(ticker="AAA", title="<b>Hi</b>", url="http://x/1",
                   published_at=now, sentiment=0.5))
    today = date.today()
    for i in (3, 1, 2):  # inserted out of order
        s.add(PriceHistory(ticker="AAA", date=today - timedelta(days=i), close=100 + i))
    s.commit()
    s.close()
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.mark.parametrize("path", [
    "/", "/watchlist", "/congress", "/portfolios", "/ticker/AAA", "/ticker/ZZZZ",
    "/api/chart/AAA", "/congress?ticker=AAA&member=jane",
])
def test_get_routes_ok(client, path):
    assert client.get(path).status_code == 200


def test_feed_order_and_convergence(client):
    html = client.get("/").get_data(as_text=True)
    assert html.index("AAA") < html.index("BBB")
    assert "CCC" not in html
    assert html.count("convergence x2") == 1


def test_feed_source_filter_and_partial(client):
    r = client.get("/?source=reddit", headers={"HX-Request": "true"})
    html = r.get_data(as_text=True)
    assert "<html" not in html and "BBB" in html and "convergence" not in html
    assert client.get("/?source=bogus").status_code == 400


def test_news_escaped(client):
    assert "<b>Hi</b>" not in client.get("/ticker/AAA").get_data(as_text=True)


def test_pin_json_idempotent_and_unpin(client, app):
    r = client.post("/watchlist/pin", json={"ticker": "TSM"})
    assert r.status_code == 201
    assert client.post("/watchlist/pin", json={"ticker": "TSM"}).status_code == 200
    s = app.extensions["db_session"]()
    assert s.query(WatchlistItem).filter_by(ticker="TSM").count() == 1
    assert client.delete("/watchlist/TSM").status_code == 200
    assert client.delete("/watchlist/TSM").status_code == 404


def test_pin_form_htmx(client):
    r = client.post("/watchlist/pin", data={"ticker": "BRK.B"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "BRK.B" in r.get_data(as_text=True)


@pytest.mark.parametrize("bad", ["TOOLONG", "A1", "", "AAPL.", "A;DROP"])
def test_pin_invalid(client, bad):
    assert client.post("/watchlist/pin", json={"ticker": bad}).status_code == 400
    assert client.post("/watchlist/pin", data={"ticker": bad}).status_code == 400


def test_csrf_foreign_origin_rejected(client):
    r = client.post("/watchlist/pin", json={"ticker": "TSM"},
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert client.delete("/watchlist/AAPL", headers={"Referer": "http://evil.example/x"}
                         ).status_code == 403
    ok = client.delete("/watchlist/AAPL", headers={"Origin": "http://localhost"})
    assert ok.status_code == 200


def test_chart_shape_and_order(client):
    data = client.get("/api/chart/AAA").get_json()
    assert data["symbol"] == "AAA"
    dates = [p["date"] for p in data["points"]]
    assert dates == sorted(dates) and len(dates) == 3
    assert set(data["points"][0]) == {"date", "close"}
    assert len(client.get("/api/chart/AAA?days=1").get_json()["points"]) == 1


@pytest.mark.parametrize("path", ["/api/chart/bad!", "/api/chart/aaa", "/ticker/toolongx"])
def test_bad_symbol(client, path):
    assert client.get(path).status_code == 400


def test_pin_uppercases(client, app):
    assert client.post("/watchlist/pin", json={"ticker": " tsm "}).status_code == 201
    s = app.extensions["db_session"]()
    assert s.query(WatchlistItem).filter_by(ticker="TSM").count() == 1


def test_pin_from_ticker_page_returns_small_fragment(client):
    for kwargs in (
        {"data": {"ticker": "TSM"}, "headers": {"HX-Request": "true", "HX-Target": "pin-btn"}},
        {"data": {"ticker": "TSM", "context": "ticker"}, "headers": {"HX-Request": "true"}},
    ):
        html = client.post("/watchlist/pin", **kwargs).get_data(as_text=True)
        assert "Pinned" in html and "<article" not in html and "<canvas" not in html
    # watchlist page keeps the full partial
    html = client.post("/watchlist/pin", data={"ticker": "TSM"},
                       headers={"HX-Request": "true", "HX-Target": "watchlist"})
    html = html.get_data(as_text=True)
    assert "row-TSM" in html


def test_ticker_page_pin_button_targets_itself(client):
    html = client.get("/ticker/AAA").get_data(as_text=True)
    assert 'id="pin-btn"' in html and "context" in html


def test_rebinding_host_rejected(client):
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/", headers={"Host": "evil.example:5000"}).status_code == 400
    assert client.post("/watchlist/pin", json={"ticker": "TSM"},
                       headers={"Host": "evil.example"}).status_code == 400


def test_allowed_hosts_ok(client):
    assert client.get("/", headers={"Host": "127.0.0.1:5000"}).status_code == 200
    assert client.get("/", headers={"Host": "localhost"}).status_code == 200


def test_origin_checked_against_allowlist_not_request_host(app):
    app.config["ALLOWED_HOSTS"] = ["evil.example", "localhost"]
    c = app.test_client()
    # request host is allowed, but the Origin is a different, non-allowed host
    r = c.post("/watchlist/pin", json={"ticker": "TSM"}, headers={"Origin": "http://127.0.0.1"})
    assert r.status_code == 403
    r = c.post("/watchlist/pin", json={"ticker": "TSM"}, headers={"Origin": "http://localhost:5000"})
    assert r.status_code == 201
    assert c.post("/watchlist/pin", json={"ticker": "MU"}, headers={"Origin": "null"}
                  ).status_code == 403


def test_unsafe_news_url_not_linked(app, client):
    s = app.extensions["db_session"]()
    s.add(NewsItem(ticker="AAA", title="Bad", url="javascript:alert(1)",
                   published_at=datetime.now(UTC), sentiment=0.0))
    s.commit()
    html = client.get("/ticker/AAA").get_data(as_text=True)
    assert "javascript:" not in html and 'href="http://x/1"' in html


def test_invalid_ticker_not_linked(app, client):
    s = app.extensions["db_session"]()
    s.add(Signal(ticker="bad<ticker", source="reddit", signal_type="mention", raw_metric=1,
                 observed_at=datetime.now(UTC)))
    s.commit()
    html = client.get("/").get_data(as_text=True)
    assert "bad&lt;ticker" in html and "/ticker/bad" not in html


def test_feed_renders_summary_badges(client):
    html = client.get("/").get_data(as_text=True)
    assert "BULLISH" in html or "BEARISH" in html or "MIXED" in html
    assert "% last session" in html or "% today" in html  # AAA has price history
    assert "no price data" in html  # BBB has none
    assert "Outlook:" in html and "rule-based, not financial advice" in html
