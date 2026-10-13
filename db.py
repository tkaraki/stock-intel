"""Database engine, session factory, and table creation."""

from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

import config


def _resolve_url(url: str) -> str:
    """Anchor relative SQLite file paths to the project root and create the directory."""
    parsed = make_url(url)
    if parsed.drivername.startswith("sqlite") and parsed.database not in (None, "", ":memory:"):
        path = Path(parsed.database)
        if not path.is_absolute():
            path = config.BASE_DIR / path
        path.parent.mkdir(parents=True, exist_ok=True)
        parsed = parsed.set(database=str(path))
    return parsed.render_as_string(hide_password=False)


engine: Engine = create_engine(_resolve_url(config.DATABASE_URL))
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def get_session() -> Session:
    return SessionLocal()


def init_db(bind: Engine | None = None) -> None:
    """Create all tables if they do not exist, and add columns introduced since."""
    from models import Base, WatchlistItem

    bind = bind or engine
    Base.metadata.create_all(bind)
    # create_all never alters existing tables, so older databases miss newer columns
    table = WatchlistItem.__table__
    if "entry_at" not in {c["name"] for c in inspect(bind).get_columns(table.name)}:
        col_type = table.c.entry_at.type.compile(dialect=bind.dialect)
        with bind.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN entry_at {col_type}"))
