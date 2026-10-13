"""Database engine, session factory, and table creation."""

from pathlib import Path

from sqlalchemy import create_engine
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
    """Create all tables if they do not exist."""
    from models import Base

    Base.metadata.create_all(bind or engine)
