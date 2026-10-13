from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

import summaries
from summaries import price_changes, signal_direction, summarize

NOW = datetime(2026, 1, 10, 12, tzinfo=UTC)


TODAY = NOW.date()


def px(closes, end=TODAY):
    """Closes as consecutive daily (date, close) pairs ending on `end`."""
    n = len(closes)
    return [(end - timedelta(days=n - 1 - i), c) for i, c in enumerate(closes)]


def sig(source, signal_type="mention", raw=10.0, sentiment=None, payload=None, hours=1):
    return SimpleNamespace(
        source=source, signal_type=signal_type, raw_metric=raw, sentiment=sentiment,
        payload=payload, observed_at=NOW - timedelta(hours=hours),
    )


def test_price_changes_edges():
    assert price_changes([]) == (None, None)
    assert price_changes(px([100])) == (None, None)
    assert price_changes(px([100, 110])) == (pytest.approx(10.0), None)
    assert price_changes(px([0, 5])) == (None, None)  # zero base avoids division by zero
    # week = latest close dated >= 7 days before the newest (day 0 here), not 5 rows back
    d = date(2026, 1, 10)
    prices = [(d - timedelta(days=9), 50.0), (d - timedelta(days=7), 100.0),
              (d - timedelta(days=6), 1.0), (d, 110.0)]
    assert price_changes(prices)[1] == pytest.approx(10.0)
    assert price_changes(prices[2:])[1] is None  # nothing 7+ days older


def test_price_status_and_stale():
    today = NOW.date()
    for days, status in [(0, "today"), (1, "last_session"), (4, "last_session"), (5, "stale")]:
        assert summaries.price_status(px([100, 101], today - timedelta(days=days)), today) == status
    assert summaries.price_status([], today) == "none"
    s = summarize([sig("news", sentiment=0.8)], px([100, 150], today - timedelta(days=9)), NOW)
    assert (s.price_status, s.day_pct, s.week_pct) == ("stale", None, None)
    assert "unknown" in s.outlook or "without price data" in s.outlook


@pytest.mark.parametrize("s,expected", [
    (sig("news", sentiment=0.5), 1),
    (sig("stocktwits", sentiment=-0.5), -1),
    (sig("news", sentiment=0.04), 0),
    (sig("reddit"), 0),
    (sig("congress", "purchase"), 1),
    (sig("congress", "sale"), -1),
    (sig("portfolio_13f", "new"), 1),
    (sig("portfolio_ark", "increased"), 1),
    (sig("portfolio_ark", "decreased"), -1),
    (sig("portfolio_13f", "exit"), -1),
    (sig("portfolio_13f", "weird"), 0),
])
def test_signal_direction(s, expected):
    assert signal_direction(s) == expected


def test_verdicts():
    bull = [sig("news", sentiment=0.8)]
    bear = [sig("news", sentiment=-0.8)]
    mixed = [sig("news", sentiment=0.8), sig("stocktwits", sentiment=-0.8)]
    assert summarize(bull, [], NOW).verdict == "bullish"
    assert summarize(bear, [], NOW).verdict == "bearish"
    assert summarize(mixed, [], NOW).verdict == "mixed"
    assert summarize([sig("reddit")], [], NOW).verdict == "mixed"  # no direction
    heavy = [sig("reddit", raw=5000), sig("reddit", raw=4000), sig("news", sentiment=0.6, raw=3)]
    assert summarize(heavy, [], NOW).verdict == "bullish"  # volume doesn't dilute
    assert summarize([], [], NOW).verdict == "mixed"


def _outlook(sentiment, closes):
    return summarize([sig("news", sentiment=sentiment)], px(closes), NOW).outlook


def test_outlook_branches():
    flat = [100.0] * 8
    assert "run-up" in _outlook(0.8, flat[:-1] + [120.0]) and "pullbacks" in _outlook(
        0.8, flat[:-1] + [120.0])
    assert "both point up" in _outlook(0.8, flat[:-1] + [101.0])
    assert "price fell" in _outlook(0.8, [100.0, 100, 100, 100, 100, 100, 100, 99.0])
    assert "sharp drop" in _outlook(-0.8, flat[:-1] + [80.0]) and "relief bounce" in _outlook(
        -0.8, flat[:-1] + [80.0])
    assert "both point down" in _outlook(-0.8, flat[:-1] + [99.0])
    assert "price rose" in _outlook(-0.8, flat[:-1] + [101.0])
    assert "mixed" in summarize([], px(flat), NOW).outlook
    assert "without price data" in _outlook(0.8, [])
    assert "without price data" in _outlook(-0.8, [])
    for text in (_outlook(0.8, []), _outlook(0.8, flat)):
        assert "will" not in text.split() and text.count(".") == 1


def test_why_phrasing():
    why = summarize(
        [sig("reddit", raw=2400, sentiment=0.4)], [], NOW).why
    assert why == ["Reddit buzz: 2.4k upvotes, positive sentiment"]
    assert summarize([sig("reddit", raw=300)], [], NOW).why == ["Reddit buzz: 300 upvotes"]
    assert summarize([sig("stocktwits", raw=38, sentiment=0.5)], [], NOW).why == [
        "StockTwits trending: 38 messages, bullish"]
    assert summarize([sig("news", sentiment=0.71)], [], NOW).why == [
        "News leaning positive (+0.71)"]
    assert summarize([sig("congress", "purchase")], [], NOW).why == ["Congress member purchase"]
    amt = {"amount_range": "$1,001 - $15,000"}
    assert summarize([sig("congress", "sale", payload=amt)], [], NOW).why == [
        "Congress member sale ($1,001 - $15,000)"]
    assert summarize([sig("portfolio_ark", "increased", raw=18)], [], NOW).why == [
        "ARK increased its position 18%"]
    assert summarize([sig("portfolio_13f", "new", raw=100)], [], NOW).why == [
        "New position in a tracked 13F fund"]


def test_why_convergence_cap_and_order():
    signals = [
        sig("news", sentiment=0.9, raw=10),
        sig("reddit", raw=900, sentiment=None),
        sig("congress", "purchase", raw=5),
        sig("stocktwits", raw=20, sentiment=0.3),
    ]
    why = summarize(signals, [], NOW).why
    assert why[0] == "3 sources agree"  # reddit has no direction, so it doesn't count
    assert len(why) == summaries.MAX_WHY == 3
    assert not any("Reddit" in line for line in why)  # supporting reasons come first
    single = summarize([sig("news", sentiment=0.9)], [], NOW).why
    assert not any("sources" in line for line in single)


def test_why_order_support_before_buzz():
    signals = [sig("reddit", raw=5000), sig("news", sentiment=0.7, raw=3),
               sig("congress", "purchase", raw=2)]
    why = summarize(signals, [], NOW).why
    assert why[0] == "2 sources agree"
    assert why[1:] == ["News leaning positive (+0.70)", "Congress member purchase"]
    assert "Reddit" in summarize(signals[:2], [], NOW).why[-1]


def test_active_wording_when_not_agreeing():
    signals = [sig("news", sentiment=0.9), sig("stocktwits", sentiment=-0.9, raw=5)]
    why = summarize(signals, [], NOW).why
    assert why[0] == "Active on 2 sources in the last 24h"
    reddit = [sig("reddit"), sig("news", sentiment=0.9)]
    assert summarize(reddit, [], NOW).why[0] == "Active on 2 sources in the last 24h"


def test_unknown_portfolio_type_is_neutral():
    s = sig("portfolio_ark", "increase", raw=18)
    assert signal_direction(s) == 0
    assert summarize([s], [], NOW).why == ["ARK position change (18%)"]
    assert summarize([sig("congress", "weird")], [], NOW).why == ["Congress member trade"]


def test_reddit_only_attention_outlook():
    s = summarize([sig("reddit", raw=3000)], px([100.0, 101.0]), NOW)
    assert s.verdict == "mixed"
    assert s.outlook == "Lots of attention but no clear positive or negative signal yet."
    assert s.why == ["Reddit buzz: 3.0k upvotes"]


def test_flat_day_outlook():
    flat = [100.0] * 8
    assert "flat today" in _outlook(0.8, flat) and "positive" in _outlook(0.8, flat)
    assert "flat today" in _outlook(-0.8, flat) and "negative" in _outlook(-0.8, flat)
    assert "flat today" in _outlook(0.8, flat[:-1] + [100.04])
