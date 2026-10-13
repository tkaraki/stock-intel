import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import queries
from models import Base


@pytest.fixture(autouse=True)
def no_live_price(monkeypatch):
    """Keep the suite offline: pinning never hits yfinance unless a test patches it."""
    monkeypatch.setattr(queries, "live_price", lambda ticker: None)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as s:
        yield s
    engine.dispose()
