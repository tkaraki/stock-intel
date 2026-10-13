"""SQLAlchemy 2.0 ORM models. All datetimes are timezone-aware UTC."""

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Stores UTC datetimes; SQLite drops tzinfo, so re-attach UTC on load."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime not allowed; use timezone-aware UTC")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime()}


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), unique=True)
    added_at: Mapped[datetime] = mapped_column(default=utcnow)
    entry_price: Mapped[float | None] = mapped_column(Float)


class PriceHistory(Base):
    __tablename__ = "price_history"
    __table_args__ = (UniqueConstraint("ticker", "date", name="uq_price_ticker_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    close: Mapped[float] = mapped_column(Float)


class NewsItem(Base):
    __tablename__ = "news_items"
    __table_args__ = (
        Index("ix_news_ticker_published", "ticker", "published_at"),
        UniqueConstraint("ticker", "url", name="uq_news_ticker_url"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    title: Mapped[str] = mapped_column(String(512))
    url: Mapped[str] = mapped_column(String(1024))
    source: Mapped[str | None] = mapped_column(String(128))
    published_at: Mapped[datetime] = mapped_column(index=True)
    sentiment: Mapped[float | None] = mapped_column(Float)


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (Index("ix_signal_ticker_observed", "ticker", "observed_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    source: Mapped[str] = mapped_column(String(32))
    signal_type: Mapped[str] = mapped_column(String(64))
    raw_metric: Mapped[float] = mapped_column(Float)
    sentiment: Mapped[float | None] = mapped_column(Float)
    payload: Mapped[dict | None] = mapped_column(JSON)
    observed_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    score: Mapped[float | None] = mapped_column(Float)
    scored_at: Mapped[datetime | None] = mapped_column()


class CongressTrade(Base):
    __tablename__ = "congress_trades"
    __table_args__ = (
        UniqueConstraint("doc_url", "ticker", "transaction_date", name="uq_congress_trade"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    member: Mapped[str] = mapped_column(String(128))
    chamber: Mapped[str] = mapped_column(String(16))
    ticker: Mapped[str | None] = mapped_column(String(16), index=True)
    transaction_type: Mapped[str] = mapped_column(String(32))
    amount_range: Mapped[str | None] = mapped_column(String(64))
    transaction_date: Mapped[date] = mapped_column(Date)
    filed_date: Mapped[date] = mapped_column(Date, index=True)
    doc_url: Mapped[str] = mapped_column(String(1024))


class Portfolio(Base):
    __tablename__ = "portfolios"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    manager: Mapped[str] = mapped_column(String(128))
    source: Mapped[str] = mapped_column(String(16))  # 'edgar' | 'ark'
    cik: Mapped[str | None] = mapped_column(String(16), unique=True)

    holdings: Mapped[list["PortfolioHolding"]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan"
    )


class PortfolioHolding(Base):
    __tablename__ = "portfolio_holdings"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "as_of", "security_key", name="uq_holding_security"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id"), index=True)
    as_of: Mapped[date] = mapped_column(Date, index=True)
    ticker: Mapped[str | None] = mapped_column(String(16), index=True)
    cusip: Mapped[str | None] = mapped_column(String(16))
    # cusip if present, else ticker: the identity used for the uniqueness constraint
    security_key: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(256))
    shares: Mapped[float] = mapped_column(Float)
    value: Mapped[float] = mapped_column(Float)

    portfolio: Mapped[Portfolio] = relationship(back_populates="holdings")
