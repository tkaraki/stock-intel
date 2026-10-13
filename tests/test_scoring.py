from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import scoring
from scoring import (
    CONVERGENCE_BONUS,
    CONVERGENCE_WINDOW_HOURS,
    RECENCY_HALF_LIFE_HOURS,
    UnknownSourceError,
    aggregate_ticker_score,
    converging_sources,
    recency_decay,
    score_signal,
)

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def ago(hours: float) -> datetime:
    return NOW - timedelta(hours=hours)


def sig(source: str, hours: float = 1, metric: float = 10.0):
    return SimpleNamespace(raw_metric=metric, source=source, observed_at=ago(hours))


def test_decay_is_one_at_age_zero():
    assert recency_decay(NOW, NOW) == 1.0


def test_decay_is_half_at_half_life():
    assert recency_decay(ago(RECENCY_HALF_LIFE_HOURS), NOW) == 0.5


def test_decay_near_zero_after_many_half_lives():
    assert recency_decay(ago(RECENCY_HALF_LIFE_HOURS * 8), NOW) < 0.01


def test_future_time_clamps_to_one():
    assert recency_decay(NOW + timedelta(hours=5), NOW) == 1.0


def test_naive_datetime_raises():
    with pytest.raises(ValueError):
        recency_decay(datetime(2026, 1, 15, 12, 0), NOW)


def test_score_signal_applies_source_weight(monkeypatch):
    monkeypatch.setitem(scoring.SOURCE_WEIGHTS, "reddit", 2.0)
    assert score_signal(10.0, "reddit", NOW, NOW) == 20.0


def test_unknown_source_raises_naming_source():
    with pytest.raises(UnknownSourceError, match="bogus"):
        score_signal(1.0, "bogus", NOW, NOW)


def test_empty_signals():
    assert aggregate_ticker_score([], NOW) == (0.0, set())


def test_single_source_no_bonus():
    score, sources = aggregate_ticker_score([sig("reddit", 0)], NOW)
    assert score == 10.0
    assert sources == {"reddit"}


def test_two_sources_in_window_get_bonus():
    score, sources = aggregate_ticker_score([sig("reddit", 0), sig("news", 0)], NOW)
    assert score == pytest.approx(20.0 * CONVERGENCE_BONUS)
    assert sources == {"reddit", "news"}


def test_window_boundary_is_inclusive():
    signals = [sig("reddit", 0), sig("news", CONVERGENCE_WINDOW_HOURS)]
    assert converging_sources(signals, NOW) == {"reddit", "news"}


def test_same_source_twice_no_bonus():
    score, sources = aggregate_ticker_score([sig("reddit", 0), sig("reddit", 0)], NOW)
    assert score == pytest.approx(20.0)
    assert sources == {"reddit"}


def test_two_sources_outside_window_no_bonus():
    old = CONVERGENCE_WINDOW_HOURS + 1
    signals = [sig("reddit", old), sig("news", old)]
    score, sources = aggregate_ticker_score(signals, NOW)
    expected = sum(score_signal(10.0, s.source, s.observed_at, NOW) for s in signals)
    assert score == pytest.approx(expected)
    assert sources == {"reddit", "news"}
    assert converging_sources(signals, NOW) == set()
