"""stock-intel: signal scoring with source weights, recency decay and convergence bonus."""
import math
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Protocol

SOURCE_WEIGHTS: dict[str, float] = {
    "reddit": 1.0,
    "stocktwits": 1.0,
    "congress": 1.0,
    "news": 1.0,
    "portfolio_13f": 1.0,
    "portfolio_ark": 1.0,
}
CONVERGENCE_BONUS = 1.5
CONVERGENCE_WINDOW_HOURS = 24
RECENCY_HALF_LIFE_HOURS = 12


class UnknownSourceError(ValueError):
    """Raised when a signal's source has no entry in SOURCE_WEIGHTS."""


class SignalLike(Protocol):
    raw_metric: float
    source: str
    observed_at: datetime


def _age_hours(signal_time: datetime, now: datetime | None) -> float:
    """Hours between signal_time and now; negative if signal_time is in the future."""
    if signal_time.tzinfo is None:
        raise ValueError("signal_time must be timezone-aware")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return (now - signal_time).total_seconds() / 3600


def recency_decay(signal_time: datetime, now: datetime | None = None) -> float:
    """Exponential decay: 1.0 at age 0, 0.5 at one half-life. Future times clamp to 1.0."""
    age = max(_age_hours(signal_time, now), 0.0)
    return 0.5 ** (age / RECENCY_HALF_LIFE_HOURS)


def scale_metric(raw_metric: float) -> float:
    """Log-scale a raw metric so sources with different units land in a similar range.

    Reddit upvotes run into the thousands while a portfolio change is a percent, so
    raw values aren't comparable: log1p(5000) ~ 8.5 vs log1p(100) ~ 4.6. The sign is kept.
    """
    return math.copysign(math.log1p(abs(raw_metric)), raw_metric)


def score_signal(
    raw_metric: float, source: str, signal_time: datetime, now: datetime | None = None
) -> float:
    """Log-scaled raw_metric, times the source weight and recency decay."""
    try:
        weight = SOURCE_WEIGHTS[source]
    except KeyError:
        valid = ", ".join(sorted(SOURCE_WEIGHTS))
        raise UnknownSourceError(f"unknown source {source!r}; valid sources: {valid}") from None
    return scale_metric(raw_metric) * weight * recency_decay(signal_time, now)


def converging_sources(signals: Iterable[SignalLike], now: datetime | None = None) -> set[str]:
    """Distinct sources with a signal inside the convergence window; empty unless 2+."""
    now = now or datetime.now(UTC)
    recent = {
        s.source
        for s in signals
        if _age_hours(s.observed_at, now) <= CONVERGENCE_WINDOW_HOURS
    }
    return recent if len(recent) >= 2 else set()


def aggregate_ticker_score(
    signals: Iterable[SignalLike], now: datetime | None = None
) -> tuple[float, set[str]]:
    """Sum of signal scores (x bonus on convergence) and the distinct sources seen."""
    now = now or datetime.now(UTC)
    signals = list(signals)
    total = sum(score_signal(s.raw_metric, s.source, s.observed_at, now) for s in signals)
    if converging_sources(signals, now):
        total *= CONVERGENCE_BONUS
    return total, {s.source for s in signals}
