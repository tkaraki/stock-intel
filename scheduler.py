"""Background jobs: periodic fetchers, signal re-scoring and the daily email digest.

Call start_scheduler() exactly once per process. Under Flask's reloader the app module is
imported twice (parent + child), which would run every job twice; run.py therefore uses
use_reloader=False and starts the scheduler a single time.
"""

import importlib
import logging
from datetime import UTC, datetime, timedelta
from functools import partial

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

import config
import scoring
from db import get_session
from models import Signal

log = logging.getLogger(__name__)

RESCORE_WINDOW = timedelta(hours=48)


def rescore_signals(session, now: datetime | None = None) -> int:
    """Score unscored signals and re-score recent ones (scores decay with age).

    Per-ticker aggregates are computed at query time, not stored. Returns rows updated.
    """
    now = now or datetime.now(UTC)
    cutoff = now - RESCORE_WINDOW
    signals = (
        session.query(Signal)
        .filter((Signal.score.is_(None)) | (Signal.observed_at >= cutoff))
        .all()
    )
    updated = 0
    for sig in signals:
        try:
            sig.score = scoring.score_signal(sig.raw_metric, sig.source, sig.observed_at, now)
        except scoring.UnknownSourceError as exc:
            log.warning("signal %s left unscored: %s", sig.id, exc)
            continue
        sig.scored_at = now
        updated += 1
    return updated


def _import_fetcher(name: str):
    return importlib.import_module(f"fetchers.{name}")


def run_fetch_job(name: str) -> None:
    """Run fetchers.<name>.run(session), commit, rescore, commit. Never raises."""
    session = get_session()
    try:
        count = _import_fetcher(name).run(session)
        session.commit()
        rescore_signals(session)
        session.commit()
        log.info("job fetch_%s done (%s items)", name, count)
    except Exception:
        session.rollback()
        log.exception("job fetch_%s failed", name)
    finally:
        session.close()


def send_email() -> None:
    """Send the daily digest; tolerate the module not existing yet."""
    try:
        from email_digest import send_digest
    except ImportError:
        log.warning("email_digest not available; skipping digest")
        return
    try:
        send_digest()
    except Exception:
        log.exception("job send_email failed")


def create_scheduler() -> BackgroundScheduler:
    """Build (but do not start) the scheduler with all jobs registered."""
    sched = BackgroundScheduler(timezone=config.SCHEDULER_TZ)
    now = datetime.now(UTC)
    defaults = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 300}

    def interval(job_id, name, minutes):
        sched.add_job(
            partial(run_fetch_job, name), IntervalTrigger(minutes=minutes),
            id=job_id, next_run_time=now, **defaults,
        )

    def cron(job_id, func, **fields):
        sched.add_job(func, CronTrigger(timezone=sched.timezone, **fields), id=job_id, **defaults)

    interval("fetch_reddit", "reddit", 30)
    interval("fetch_stocktwits", "stocktwits", 15)
    cron("fetch_congress", partial(run_fetch_job, "congress"), hour=6, minute=0)
    cron("fetch_news", partial(run_fetch_job, "news"), hour=6, minute=0)
    cron("fetch_prices", partial(run_fetch_job, "prices"),
         day_of_week="mon-fri", hour=18, minute=0)
    cron("fetch_ark", partial(run_fetch_job, "ark"), day_of_week="mon-fri", hour=19, minute=0)
    cron("fetch_13f", partial(run_fetch_job, "edgar"), day_of_week="sun", hour=8, minute=0)
    cron("send_email", send_email, hour=7, minute=0)
    return sched


def start_scheduler() -> BackgroundScheduler | None:
    """Create and start the scheduler unless SCHEDULER_ENABLED is false."""
    if not config.SCHEDULER_ENABLED:
        log.info("scheduler disabled via SCHEDULER_ENABLED")
        return None
    sched = create_scheduler()
    sched.start()
    return sched
