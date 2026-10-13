import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

import config
import email_digest as ed
from models import (
    CongressTrade,
    Portfolio,
    PortfolioHolding,
    PriceHistory,
    Signal,
    WatchlistItem,
)

NOW = datetime(2026, 3, 11, 12, 0, tzinfo=UTC)  # 08:00 in America/New_York -> yesterday 03-10


@pytest.fixture(autouse=True)
def _tz(monkeypatch):
    monkeypatch.setattr(config, "SCHEDULER_TZ", "America/New_York")


def _sig(ticker, source="reddit", metric=10.0, hours=1.0, payload=None):
    return Signal(
        ticker=ticker, source=source, signal_type="mention", raw_metric=metric,
        observed_at=NOW - timedelta(hours=hours), payload=payload,
    )


def test_gather_top5_ordering_and_window(session):
    for i, t in enumerate(["A", "B", "C", "D", "E", "F"]):
        session.add(_sig(t, metric=10.0 * (i + 1)))
    session.add(_sig("OLD", metric=9999, hours=30))
    session.add_all([_sig("A", source="news", metric=1.0) for _ in range(1)])
    session.commit()
    top = ed.gather_digest_data(session, NOW)["top_signals"]
    assert len(top) == 5
    # Log-scaled: A converges (reddit + news) so (log1p(10) + log1p(1)) x 1.5 ~ 4.6 beats
    # F's single log1p(60) ~ 4.1; B is the weakest and drops out.
    assert [t["ticker"] for t in top] == ["A", "F", "E", "D", "C"]
    assert "OLD" not in [t["ticker"] for t in top]


def test_gather_convergence_and_samples(session):
    session.add_all([_sig("X", source=s, metric=5 + i) for i, s in
                     enumerate(["reddit", "news", "stocktwits", "congress"])])
    session.commit()
    top = ed.gather_digest_data(session, NOW)["top_signals"][0]
    assert top["converging_sources"] == ["congress", "news", "reddit", "stocktwits"]
    assert len(top["signals"]) == 3


def test_gather_watchlist_pnl(session):
    session.add(WatchlistItem(ticker="AAA", entry_price=100.0))
    session.add_all([
        PriceHistory(ticker="AAA", date=date(2026, 3, 9), close=90.0),
        PriceHistory(ticker="AAA", date=date(2026, 3, 10), close=110.0),
    ])
    session.commit()
    w = ed.gather_digest_data(session, NOW)["watchlist"][0]
    assert w["latest_close"] == 110.0 and w["pnl_pct"] == 10.0


def test_gather_congress_midweek_yesterday_only(session):
    def trade(d, url):
        return CongressTrade(
            member="M", chamber="house", ticker="ZZZ", transaction_type="buy",
            transaction_date=date(2026, 3, 1), filed_date=d, doc_url=url,
        )
    session.add_all([trade(date(2026, 3, 10), "u1"), trade(date(2026, 3, 11), "u2"),
                     trade(date(2026, 3, 9), "u3")])
    session.commit()
    trades = ed.gather_digest_data(session, NOW)["congress_trades"]
    assert len(trades) == 1


def test_gather_congress_monday_includes_weekend(session):
    def trade(d, url):
        return CongressTrade(
            member="M", chamber="house", ticker="ZZZ", transaction_type="buy",
            transaction_date=date(2026, 3, 1), filed_date=d, doc_url=url,
        )
    # Monday 2026-03-16 08:00 New York
    monday = datetime(2026, 3, 16, 12, 0, tzinfo=UTC)
    session.add_all([trade(date(2026, 3, 12), "u0"), trade(date(2026, 3, 13), "u1"),
                     trade(date(2026, 3, 14), "u2"), trade(date(2026, 3, 15), "u3"),
                     trade(date(2026, 3, 16), "u4")])
    session.commit()
    assert len(ed.gather_digest_data(session, monday)["congress_trades"]) == 3
    # Tuesday: only Monday's filings
    tuesday = datetime(2026, 3, 17, 12, 0, tzinfo=UTC)
    assert len(ed.gather_digest_data(session, tuesday)["congress_trades"]) == 1


def _portfolio(session, newer_as_of):
    pf = Portfolio(name="Fund", manager="Mgr", source="edgar", cik="1")
    session.add(pf)
    session.flush()
    for as_of, shares in [(date(2025, 12, 31), 100.0), (newer_as_of, 200.0)]:
        session.add(PortfolioHolding(
            portfolio_id=pf.id, as_of=as_of, ticker="AAPL", security_key="AAPL",
            name="Apple", shares=shares, value=1.0,
        ))
    session.commit()


def test_portfolio_changes_within_window(session):
    _portfolio(session, date(2026, 3, 8))
    pc = ed.gather_digest_data(session, NOW)["portfolio_changes"]
    assert pc[0]["changes"][0]["ticker"] == "AAPL"
    assert pc[0]["changes"][0]["change"] == "increased"


def test_portfolio_changes_outside_window(session):
    _portfolio(session, date(2026, 3, 1))
    assert ed.gather_digest_data(session, NOW)["portfolio_changes"] == []


class FakeAnthropic:
    def __init__(self, text=None, exc=None):
        self.text, self.exc, self.calls = text, exc, []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self.text)])


GOOD = {"recommendations": [
    {"ticker": "aapl", "action": "Buy", "rationale": "Reddit and news agree.", "convergence": True},
]}


def test_recs_valid():
    client = FakeAnthropic(json.dumps(GOOD))
    recs = ed.get_recommendations({"top_signals": []}, client=client)
    assert recs == [{"ticker": "AAPL", "action": "buy",
                     "rationale": "Reddit and news agree.", "convergence": True}]
    call = client.calls[0]
    assert call["max_tokens"] == 2000 and call["model"] == config.ANTHROPIC_MODEL
    assert json.loads(call["messages"][0]["content"]) == {"top_signals": []}


def test_recs_code_fenced():
    client = FakeAnthropic("```json\n" + json.dumps(GOOD) + "\n```")
    assert len(ed.get_recommendations({}, client=client)) == 1


def test_recs_malformed_returns_none():
    assert ed.get_recommendations({}, client=FakeAnthropic("not json")) is None


def test_recs_bad_items_dropped():
    bad = {"recommendations": [
        {"ticker": "X", "action": "sell", "rationale": "r"},
        {"ticker": "Y", "action": "hold"},
        "junk",
        {"ticker": "Z", "action": "watch", "rationale": "ok"},
    ]}
    recs = ed.get_recommendations({}, client=FakeAnthropic(json.dumps(bad)))
    assert [r["ticker"] for r in recs] == ["Z"] and recs[0]["convergence"] is False


def test_recs_api_error_and_missing_key(monkeypatch):
    assert ed.get_recommendations({}, client=FakeAnthropic(exc=RuntimeError("boom"))) is None
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    assert ed.get_recommendations({}) is None


def test_render_escapes_and_is_gmail_safe():
    data = {
        "top_signals": [{"ticker": "X", "score": 1.0, "sources": ["news"],
                         "converging_sources": ["news", "reddit"],
                         "signals": [{"source": "news", "type": "m",
                                      "headline": "<script>alert(1)</script>"}]}],
        "watchlist": [{"ticker": "A", "entry_price": 1.0, "latest_close": 2.0, "pnl_pct": 100.0}],
        "congress_trades": [], "portfolio_changes": [],
    }
    out = ed.render_digest_html(data, None, NOW)
    low = out.lower()
    assert "<script" not in low and "<style" not in low
    assert "&lt;script&gt;" in out
    assert "unavailable" in out and "CONVERGENCE" in out
    assert "not financial advice" in out and "max-width:600px" in out


class FakeSMTP:
    """Records what send_digest does instead of talking to a mail server."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.calls, self.sent = host, port, [], []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user))

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(ed.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(ed.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "SMTP_PORT", 587)
    monkeypatch.setattr(config, "SMTP_USER", "me@example.com")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "app-password")
    monkeypatch.setattr(config, "DIGEST_FROM_EMAIL", None)
    monkeypatch.setattr(config, "DIGEST_TO_EMAIL", "to@example.com")
    monkeypatch.setattr(config, "DIGEST_SAVE_PATH", None)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    return FakeSMTP


def test_send_digest_missing_settings(monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", None)
    monkeypatch.setattr(config, "DIGEST_SAVE_PATH", None)
    assert ed.send_digest(NOW) is False


def test_send_digest_over_smtp_with_starttls(session, smtp, monkeypatch, tmp_path):
    save = tmp_path / "d.html"
    monkeypatch.setattr(config, "DIGEST_SAVE_PATH", str(save))
    assert ed.send_digest(NOW, session=session) is True
    server = smtp.instances[0]
    assert (server.host, server.port) == ("smtp.example.com", 587)
    assert server.calls == ["starttls", ("login", "me@example.com")]
    msg = server.sent[0]
    assert msg["From"] == "me@example.com" and msg["To"] == "to@example.com"
    assert msg["Subject"] == "Stock Intel digest — 2026-03-11"
    assert msg.get_body(("html",)).get_content().strip().startswith("<")
    assert save.exists()


def test_send_digest_port_465_uses_implicit_tls(session, smtp, monkeypatch):
    monkeypatch.setattr(config, "SMTP_PORT", 465)
    monkeypatch.setattr(config, "DIGEST_FROM_EMAIL", "alias@example.com")
    assert ed.send_digest(NOW, session=session) is True
    server = smtp.instances[0]
    assert "starttls" not in server.calls
    assert server.sent[0]["From"] == "alias@example.com"


def test_send_digest_saves_file_without_smtp(session, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SMTP_HOST", None)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    save = tmp_path / "d.html"
    monkeypatch.setattr(config, "DIGEST_SAVE_PATH", str(save))
    assert ed.send_digest(NOW, session=session) is False
    assert save.exists()


def test_send_digest_never_raises(session, smtp, monkeypatch):
    def boom(self, msg):
        raise OSError("connection refused")

    monkeypatch.setattr(FakeSMTP, "send_message", boom)
    assert ed.send_digest(NOW, session=session) is False
