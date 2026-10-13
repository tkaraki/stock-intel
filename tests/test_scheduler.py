from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import scheduler
from models import Signal

NOW = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)


def _sig(session, source="reddit", age_h=1, score=None):
    s = Signal(ticker="AAPL", source=source, signal_type="mention", raw_metric=10.0,
               observed_at=NOW - timedelta(hours=age_h), score=score)
    session.add(s)
    session.commit()
    return s


def test_jobs_registered():
    jobs = {j.id: j for j in scheduler.create_scheduler().get_jobs()}
    assert set(jobs) == {
        "fetch_reddit", "fetch_stocktwits", "fetch_congress", "fetch_news",
        "fetch_prices", "fetch_ark", "fetch_13f", "send_email",
    }
    assert jobs["fetch_reddit"].trigger.interval == timedelta(minutes=30)
    assert jobs["fetch_stocktwits"].trigger.interval == timedelta(minutes=15)
    for jid, text in {
        "fetch_congress": "hour='6'", "fetch_prices": "day_of_week='mon-fri'",
        "fetch_ark": "hour='19'", "fetch_13f": "day_of_week='sun'", "send_email": "hour='7'",
    }.items():
        assert text in str(jobs[jid].trigger)
    assert str(jobs["send_email"].trigger.timezone) == "America/New_York"


def test_rescore_new_and_recent_skips_old_scored(session):
    new_old = _sig(session, age_h=100)  # unscored, old -> scored
    recent = _sig(session, age_h=1, score=99.0)  # recent -> rescored
    old_scored = _sig(session, age_h=100, score=7.0)  # untouched
    scheduler.rescore_signals(session, NOW)
    assert new_old.score is not None and new_old.scored_at == NOW
    assert recent.score != 99.0 and recent.scored_at == NOW
    assert old_scored.score == 7.0 and old_scored.scored_at is None


def test_rescore_unknown_source(session):
    bad = _sig(session, source="bogus")
    good = _sig(session)
    scheduler.rescore_signals(session, NOW)
    assert bad.score is None
    assert good.score is not None


def test_failing_fetcher_rolls_back(session, monkeypatch):
    def boom(s):
        s.add(Signal(ticker="X", source="reddit", signal_type="m", raw_metric=1.0))
        raise RuntimeError("fail")

    monkeypatch.setattr(scheduler, "get_session", lambda: session)
    monkeypatch.setattr(scheduler, "_import_fetcher", lambda name: SimpleNamespace(run=boom))
    scheduler.run_fetch_job("reddit")  # must not raise
    assert session.query(Signal).count() == 0


def test_successful_job_commits_and_scores(session, monkeypatch):
    def ok(s):
        s.add(Signal(ticker="X", source="reddit", signal_type="m", raw_metric=1.0,
                     observed_at=datetime.now(UTC)))
        return 1

    monkeypatch.setattr(scheduler, "get_session", lambda: session)
    monkeypatch.setattr(scheduler, "_import_fetcher", lambda name: SimpleNamespace(run=ok))
    scheduler.run_fetch_job("reddit")
    assert session.query(Signal).one().score is not None
