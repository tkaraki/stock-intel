"""Application settings, loaded from .env with safe defaults, plus logging setup."""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _get(name: str, default: str | None = None) -> str | None:
    """Return an env var, treating empty strings as unset."""
    return os.getenv(name) or default


FLASK_SECRET_KEY = _get("FLASK_SECRET_KEY", "dev-only-insecure-key")
DATABASE_URL = _get("DATABASE_URL", "sqlite:///data/stock_intel.db")

REDDIT_CLIENT_ID = _get("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = _get("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT = _get("REDDIT_USER_AGENT", "stock-intel/0.1")
NEWSAPI_KEY = _get("NEWSAPI_KEY")
SEC_EDGAR_COMPANY_NAME = _get("SEC_EDGAR_COMPANY_NAME")
SEC_EDGAR_EMAIL = _get("SEC_EDGAR_EMAIL")
ARK_ARKK_CSV_URL = _get("ARK_ARKK_CSV_URL")
ANTHROPIC_API_KEY = _get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = _get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
SMTP_HOST = _get("SMTP_HOST")
SMTP_PORT = int(_get("SMTP_PORT", "587") or 587)
SMTP_USER = _get("SMTP_USER")
SMTP_PASSWORD = _get("SMTP_PASSWORD")
DIGEST_FROM_EMAIL = _get("DIGEST_FROM_EMAIL")
DIGEST_TO_EMAIL = _get("DIGEST_TO_EMAIL")
DIGEST_SAVE_PATH = _get("DIGEST_SAVE_PATH")
LOG_LEVEL = (_get("LOG_LEVEL", "INFO") or "INFO").upper()
SCHEDULER_TZ = _get("SCHEDULER_TZ", "America/New_York")
SCHEDULER_ENABLED = (_get("SCHEDULER_ENABLED", "true") or "true").lower() in (
    "1", "true", "yes", "on",
)

HOST = _get("HOST", "127.0.0.1")
ALLOWED_HOSTS = [
    h.strip().lower() for h in (_get("ALLOWED_HOSTS", "127.0.0.1,localhost") or "").split(",")
    if h.strip()
]
PORT = int(_get("PORT", "5000") or 5000)

LOG_DIR = BASE_DIR / "logs"


def setup_logging() -> None:
    """Log to console and a rotating logs/app.log. Safe to call more than once."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if getattr(root, "_stock_intel_configured", False):
        return
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(
        LOG_DIR / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    console = logging.StreamHandler()
    for handler in (file_handler, console):
        handler.setFormatter(fmt)
        root.addHandler(handler)
    root.setLevel(LOG_LEVEL)
    root._stock_intel_configured = True  # type: ignore[attr-defined]
