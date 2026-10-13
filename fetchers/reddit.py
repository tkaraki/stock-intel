"""Reddit (PRAW) ticker-mention fetcher."""

import logging
from datetime import UTC, datetime, timedelta

import praw
from sqlalchemy import select
from sqlalchemy.orm import Session

import config
from fetchers.tickers import extract_tickers
from models import Signal

log = logging.getLogger(__name__)

SUBREDDITS = ("wallstreetbets", "stocks", "investing")
POST_LIMIT = 50


def _client() -> praw.Reddit:
    reddit = praw.Reddit(
        client_id=config.REDDIT_CLIENT_ID,
        client_secret=config.REDDIT_CLIENT_SECRET,
        user_agent=config.REDDIT_USER_AGENT,
        check_for_async=False,
    )
    reddit.read_only = True
    return reddit


def _seen(session: Session) -> dict[tuple[str, str], Signal]:
    """Reddit signals from the last 7 days keyed by (post_id, ticker); top posts are daily."""
    since = datetime.now(UTC) - timedelta(days=7)
    rows = session.scalars(
        select(Signal).where(Signal.source == "reddit", Signal.observed_at >= since)
    ).all()
    return {(s.payload.get("post_id"), s.ticker): s for s in rows if s.payload}


def run(session: Session) -> int:
    """Save one Signal per (post, ticker) from today's top posts. Returns rows saved."""
    if not (config.REDDIT_CLIENT_ID and config.REDDIT_CLIENT_SECRET):
        log.warning("skipping: reddit not configured")
        return 0
    saved = 0
    try:
        reddit = _client()
        seen = _seen(session)
        for name in SUBREDDITS:
            try:
                for post in reddit.subreddit(name).top(time_filter="day", limit=POST_LIMIT):
                    text = f"{post.title} {getattr(post, 'selftext', '') or ''}"
                    created = getattr(post, "created_utc", None)
                    observed = (
                        datetime.fromtimestamp(created, UTC) if created else datetime.now(UTC)
                    )
                    for ticker in sorted(extract_tickers(text)):
                        prior = seen.get((post.id, ticker))
                        if prior is not None:
                            if prior.raw_metric != float(post.score):
                                prior.raw_metric = float(post.score)
                                prior.score = None  # rescore recomputes
                            continue
                        sig = Signal(
                            ticker=ticker,
                            source="reddit",
                            signal_type="mention",
                            raw_metric=float(post.score),
                            observed_at=observed,
                            payload={
                                "subreddit": name,
                                "post_id": post.id,
                                "title": post.title,
                                "url": f"https://reddit.com{post.permalink}"
                                if getattr(post, "permalink", None)
                                else post.url,
                            },
                        )
                        seen[(post.id, ticker)] = sig
                        session.add(sig)
                        saved += 1
            except Exception:
                log.exception("reddit fetch failed for r/%s", name)
        session.commit()
    except Exception:
        log.exception("reddit fetcher failed")
        session.rollback()
        return 0
    log.info("reddit: saved %d signals", saved)
    return saved
