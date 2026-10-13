"""News headline fetcher (NewsAPI) with VADER sentiment."""

import logging
from datetime import UTC, datetime
from urllib.parse import urlsplit

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

import config
from models import NewsItem, Signal, WatchlistItem

log = logging.getLogger(__name__)

URL = "https://newsapi.org/v2/everything"
HEADERS = {"User-Agent": "stock-intel/0.1 (personal research tool)"}
TIMEOUT = 10


def _parse_time(value: str | None) -> datetime:
    if value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


def run(session: Session) -> int:
    """Save new NewsItems (and a news Signal each) per watchlist ticker. Returns rows saved."""
    if not config.NEWSAPI_KEY:
        log.warning("skipping: NEWSAPI_KEY not configured")
        return 0
    saved = 0
    try:
        analyzer = SentimentIntensityAnalyzer()
        tickers = list(session.scalars(select(WatchlistItem.ticker)))
        known = set(session.execute(select(NewsItem.ticker, NewsItem.url)).all())
        for ticker in tickers:
            try:
                resp = requests.get(
                    URL,
                    params={"q": ticker, "sortBy": "publishedAt", "pageSize": 5},
                    headers={**HEADERS, "X-Api-Key": config.NEWSAPI_KEY},
                    timeout=TIMEOUT,
                )
                if resp.status_code in (401, 429):
                    log.warning("news: NewsAPI returned HTTP %s; stopping", resp.status_code)
                    break
                resp.raise_for_status()
                articles = resp.json().get("articles", [])
            except Exception:
                log.exception("news fetch failed for %s", ticker)
                continue
            for art in articles:
                url, title = art.get("url"), art.get("title")
                if not url or not title:
                    continue
                url = url[:1024]
                if urlsplit(url).scheme not in ("http", "https") or (ticker, url) in known:
                    continue
                known.add((ticker, url))
                published = _parse_time(art.get("publishedAt"))
                compound = analyzer.polarity_scores(title)["compound"]
                session.add(
                    NewsItem(
                        ticker=ticker,
                        title=title[:512],
                        url=url,
                        source=(art.get("source") or {}).get("name"),
                        published_at=published,
                        sentiment=compound,
                    )
                )
                session.add(
                    Signal(
                        ticker=ticker,
                        source="news",
                        signal_type="headline",
                        raw_metric=abs(compound) * 10,
                        sentiment=compound,
                        observed_at=published,
                        payload={"title": title, "url": url},
                    )
                )
                saved += 1
        session.commit()
    except Exception:
        log.exception("news fetcher failed")
        session.rollback()
        return 0
    log.info("news: saved %d articles", saved)
    return saved
