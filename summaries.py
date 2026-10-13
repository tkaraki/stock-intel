"""stock-intel: rule-based per-ticker summary (verdict, reasons, hedged outlook). No AI, no I/O."""
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import scoring

SENTIMENT_THRESHOLD = 0.05
VERDICT_THRESHOLD = 0.2
BIG_WEEK_MOVE_PCT = 8.0
FLAT_DAY_PCT = 0.05
WEEK_DAYS = 7
MAX_PRICE_AGE_DAYS = 4  # weekend or holiday gap; older closes are stale
MAX_WHY = 3

_BUY_TYPES = {"purchase", "new", "increased"}
_SELL_TYPES = {"sale", "exit", "decreased"}


@dataclass
class TickerSummary:
    day_pct: float | None
    week_pct: float | None
    price_status: str  # "today" | "last_session" | "stale" | "none"
    verdict: str  # "bullish" | "bearish" | "mixed"
    why: list[str] = field(default_factory=list)
    outlook: str = ""


def _pct(new: float, old: float) -> float | None:
    return (new - old) / old * 100 if old else None


def price_changes(prices: Sequence[tuple[date, float]]) -> tuple[float | None, float | None]:
    """(day %, week %) from (date, close) pairs ordered oldest to newest.

    Day compares the newest close with the one before it. Week compares it with the latest
    close dated at least 7 calendar days earlier, or None if there isn't one.
    """
    if not prices:
        return None, None
    last_date, last = prices[-1]
    day = _pct(last, prices[-2][1]) if len(prices) >= 2 else None
    cutoff = last_date.toordinal() - WEEK_DAYS
    older = [c for d, c in prices if d.toordinal() <= cutoff]
    week = _pct(last, older[-1]) if older else None
    return day, week


def price_status(prices: Sequence[tuple[date, float]], today: date) -> str:
    """How fresh the newest close is relative to today (market time zone)."""
    if not prices:
        return "none"
    age = (today - prices[-1][0]).days
    if age <= 0:
        return "today"
    return "last_session" if age <= MAX_PRICE_AGE_DAYS else "stale"


def signal_direction(signal: Any) -> int:
    """+1 bullish, -1 bearish, 0 neutral/unknown."""
    sentiment = signal.sentiment
    if sentiment is not None and abs(sentiment) >= SENTIMENT_THRESHOLD:
        return 1 if sentiment > 0 else -1
    if signal.source == "congress" or signal.source.startswith("portfolio_"):
        if signal.signal_type in _BUY_TYPES:
            return 1
        if signal.signal_type in _SELL_TYPES:
            return -1
    return 0


def _verdict(signals: Sequence[Any], now: datetime) -> str:
    """Net direction over directional signals only; volume-only signals don't dilute it."""
    directional = [(signal_direction(s), s) for s in signals]
    directional = [(d, s) for d, s in directional if d != 0]
    scores = [
        scoring.score_signal(s.raw_metric, s.source, s.observed_at, now) for _, s in directional
    ]
    total = sum(scores)
    if total <= 0:
        return "mixed"
    net = sum(d * sc for (d, _), sc in zip(directional, scores, strict=True)) / total
    if net > VERDICT_THRESHOLD:
        return "bullish"
    if net < -VERDICT_THRESHOLD:
        return "bearish"
    return "mixed"


def _mood(sentiment: float | None) -> str:
    if sentiment is None or abs(sentiment) < SENTIMENT_THRESHOLD:
        return ""
    return "positive" if sentiment > 0 else "negative"


def _compact(n: float) -> str:
    return f"{n / 1000:.1f}k" if abs(n) >= 1000 else f"{n:.0f}"


def _describe(s: Any) -> str:
    """One plain-English line for a signal, with its real numbers."""
    payload = s.payload or {}
    if s.source == "reddit":
        mood = _mood(s.sentiment)
        line = f"Reddit buzz: {_compact(s.raw_metric)} upvotes"
        return line + (f", {mood} sentiment" if mood else "")
    if s.source == "stocktwits":
        mood = {"positive": "bullish", "negative": "bearish"}.get(_mood(s.sentiment), "mixed")
        return f"StockTwits trending: {s.raw_metric:.0f} messages, {mood}"
    if s.source == "news":
        if s.sentiment is None:
            return "News coverage"
        return f"News leaning {_mood(s.sentiment) or 'neutral'} ({s.sentiment:+.2f})"
    if s.source == "congress":
        verb = {"purchase": "purchase", "sale": "sale"}.get(s.signal_type, "trade")
        amount = payload.get("amount_range")
        return f"Congress member {verb}" + (f" ({amount})" if amount else "")
    if s.source in ("portfolio_ark", "portfolio_13f"):
        who = "ARK" if s.source == "portfolio_ark" else "A tracked 13F fund"
        if s.signal_type == "new":
            return "New position in " + ("an ARK fund" if s.source == "portfolio_ark" else
                                         "a tracked 13F fund")
        if s.signal_type == "exit":
            return f"{who} exited its position"
        if s.signal_type == "increased":
            return f"{who} increased its position {s.raw_metric:.0f}%"
        if s.signal_type == "decreased":
            return f"{who} reduced its position {s.raw_metric:.0f}%"
        return f"{who} position change ({s.raw_metric:.0f}%)"
    return f"{s.source} {s.signal_type}"


def _why(signals: Sequence[Any], verdict: str, now: datetime) -> list[str]:
    """Convergence line, then reasons backing the verdict, attention-only, and the rest."""
    want = {"bullish": 1, "bearish": -1}.get(verdict, 0)
    groups: tuple[list[str], list[str], list[str]] = ([], [], [])
    ranked = sorted(
        signals,
        key=lambda s: scoring.score_signal(s.raw_metric, s.source, s.observed_at, now),
        reverse=True,
    )
    for s in ranked:
        d = signal_direction(s)
        groups[0 if want and d == want else 1 if d == 0 else 2].append(_describe(s))
    lines: list[str] = []
    active = scoring.converging_sources(signals, now)
    directions = {signal_direction(s) for s in signals} - {0}
    agreeing = {s.source for s in signals if signal_direction(s) != 0} & active
    if want and directions == {want} and len(agreeing) >= 2:
        lines.append(f"{len(agreeing)} sources agree")
    elif len(active) >= 2:
        lines.append(f"Active on {len(active)} sources in the last 24h")
    for line in (*groups[0], *groups[1], *groups[2]):
        if line not in lines:
            lines.append(line)
    return lines[:MAX_WHY]


def _outlook(
    verdict: str, day: float | None, week: float | None, directional: bool = True
) -> str:
    if verdict == "mixed":
        if not directional:
            return "Lots of attention but no clear positive or negative signal yet."
        return "Signals are mixed, so there is no clear direction."
    up = verdict == "bullish"
    if day is None and week is None:
        if up:
            return "Sentiment is mostly positive, but without price data the direction is unclear."
        return "Sentiment is mostly negative, but without price data the direction is unclear."
    if week is not None and (week >= BIG_WEEK_MOVE_PCT if up else week <= -BIG_WEEK_MOVE_PCT):
        if up:
            return ("After a strong run-up, momentum may continue short-term, "
                    "but pullbacks are common after moves this large.")
        return ("After a sharp drop, a relief bounce is possible, "
                "but sentiment remains negative.")
    if day is None:
        return ("Sentiment is positive, but today's price move is unknown." if up
                else "Sentiment is negative, but today's price move is unknown.")
    if abs(day) < FLAT_DAY_PCT:
        return ("Sentiment leans positive while the price is flat today." if up
                else "Sentiment leans negative while the price is flat today.")
    if (day > 0) == up:
        return ("Price and sentiment both point up, so momentum may continue short-term."
                if up else
                "Price and sentiment both point down, so weakness may continue short-term.")
    if up:
        return ("Sentiment is positive while the price fell, so a rebound is possible "
                "or sentiment may not yet be priced in.")
    return ("Sentiment is negative while the price rose, so the rally may fade "
            "or the negativity may not matter.")


def summarize(
    signals: Sequence[Any],
    prices: Sequence[tuple[date, float]],
    now: datetime,
    today: date | None = None,
) -> TickerSummary:
    """prices: (date, close) oldest first; today: the market-time-zone date (default now's)."""
    status = price_status(prices, today or now.date())
    day, week = (None, None) if status == "stale" else price_changes(prices)
    verdict = _verdict(signals, now)
    directional = any(signal_direction(s) != 0 for s in signals) or not signals
    outlook = _outlook(verdict, day, week, directional)
    return TickerSummary(day, week, status, verdict, _why(signals, verdict, now), outlook)
