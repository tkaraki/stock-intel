"""Flask dashboard: single-user, local, no auth. Routes stay thin; queries live in queries.py."""

import secrets
from urllib.parse import urlsplit

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool

import config
import queries
from scoring import SOURCE_WEIGHTS


def _make_session_factory(url: str | None):
    """Use the shared engine by default; build a private one when a test URL is given."""
    import db

    if not url:
        db.init_db()
        return db.SessionLocal
    kwargs = {}
    if url.startswith("sqlite") and ":memory:" in url:
        kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
    engine = create_engine(url, **kwargs)
    db.init_db(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _host_ok(allowed: set[str]) -> bool:
    """Host header (sans port) must be allow-listed; blocks DNS rebinding."""
    return urlsplit(f"//{request.host}").hostname in allowed


def _origin_ok(allowed: set[str]) -> bool:
    """Origin (or Referer) host must be allow-listed for state-changing requests."""
    header = request.headers.get("Origin") or request.headers.get("Referer")
    if not header:
        return True  # non-browser clients (curl) send neither
    return urlsplit(header).hostname in allowed


def safe_url(value: str | None) -> str:
    """Only http(s) URLs may be rendered as external links; anything else becomes '#'."""
    if isinstance(value, str) and value.lower().startswith(("http://", "https://")):
        return value
    return "#"


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = config.FLASK_SECRET_KEY
    if app.config["SECRET_KEY"] == "dev-only-insecure-key":
        app.config["SECRET_KEY"] = secrets.token_hex(32)  # per-process random
    app.config["DATABASE_URL"] = None
    app.config["ALLOWED_HOSTS"] = config.ALLOWED_HOSTS
    if test_config:
        app.config.update(test_config)

    factory = _make_session_factory(app.config["DATABASE_URL"])
    Session = scoped_session(factory)
    app.extensions["db_session"] = Session
    with Session() as s:
        from seed import seed

        seed(s)
    Session.remove()

    @app.teardown_appcontext
    def _close(_exc):
        Session.remove()

    @app.before_request
    def _csrf_guard():
        allowed = {h.lower() for h in app.config["ALLOWED_HOSTS"]}
        if not _host_ok(allowed):
            abort(400)
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and not _origin_ok(allowed):
            abort(403)

    app.jinja_env.filters["money"] = lambda v: "-" if v is None else f"${v:,.2f}"
    app.jinja_env.filters["big_money"] = lambda v: "-" if v is None else f"${v:,.0f}"
    app.jinja_env.filters["num"] = lambda v: "-" if v is None else f"{v:,.0f}"
    app.jinja_env.filters["pct"] = lambda v: "-" if v is None else f"{v:+.1f}%"
    app.jinja_env.filters["entry_at"] = queries.format_entry_at
    app.jinja_env.filters["safe_url"] = safe_url
    app.jinja_env.tests["valid_ticker"] = queries.valid_ticker
    app.jinja_env.filters["sent"] = lambda v: "-" if v is None else f"{v:+.2f}"

    def is_htmx() -> bool:
        return request.headers.get("HX-Request") == "true"

    @app.get("/")
    def index():
        source = request.args.get("source") or None
        if source and source not in SOURCE_WEIGHTS:
            abort(400)
        rows = queries.feed(Session(), source)
        ctx = {"rows": rows, "active": source, "sources": list(SOURCE_WEIGHTS)}
        return render_template("_feed.html" if is_htmx() else "index.html", **ctx)

    @app.get("/watchlist")
    def watchlist_page():
        return render_template("watchlist.html", items=queries.watchlist(Session()))

    @app.post("/watchlist/pin")
    def pin():
        payload = request.get_json(silent=True) if request.is_json else None
        raw = (payload or {}).get("ticker") if isinstance(payload, dict) else None
        if raw is None:
            raw = request.form.get("ticker")
        ticker = raw if isinstance(raw, str) else ""
        ticker = ticker.strip().upper()
        if not queries.valid_ticker(ticker):
            return (jsonify(error="invalid ticker"), 400) if request.is_json else (
                "Invalid ticker (1-5 uppercase letters, optional .X suffix)", 400)
        created = queries.pin(Session(), ticker)
        from_ticker_page = (
            request.headers.get("HX-Target") == "pin-btn"
            or (isinstance(payload, dict) and payload.get("context") == "ticker")
            or request.form.get("context") == "ticker"
        )
        if from_ticker_page and (is_htmx() or not request.is_json):
            return render_template("_pinned.html")
        if is_htmx() or not request.is_json:
            return render_template("_watchlist_rows.html", items=queries.watchlist(Session()))
        return jsonify(ticker=ticker, created=created), 201 if created else 200

    @app.delete("/watchlist/<ticker>")
    def unpin(ticker):
        if not queries.valid_ticker(ticker):
            abort(400)
        if not queries.unpin(Session(), ticker):
            abort(404)
        return "", 200

    @app.get("/congress")
    def congress():
        ticker = (request.args.get("ticker") or "").strip().upper()
        member = (request.args.get("member") or "").strip()[:100]
        if ticker and not queries.valid_ticker(ticker):
            abort(400)
        page = request.args.get("page", 1, type=int) or 1
        trades, total = queries.congress_page(Session(), ticker or None, member or None, page)
        pages = max((total + queries.PAGE_SIZE - 1) // queries.PAGE_SIZE, 1)
        return render_template(
            "congress.html", trades=trades, total=total, page=page, pages=pages,
            ticker=ticker, member=member,
        )

    @app.get("/portfolios")
    def portfolios():
        return render_template("portfolios.html", views=queries.portfolio_views(Session()))

    @app.get("/ticker")
    def ticker_search():
        symbol = (request.args.get("q") or "").strip().upper()
        if not queries.valid_ticker(symbol):
            abort(400)
        return redirect(url_for("ticker_page", symbol=symbol))

    @app.get("/ticker/<symbol>")
    def ticker_page(symbol):
        if not queries.valid_ticker(symbol):
            abort(400)
        detail = queries.ticker_detail(Session(), symbol)
        return render_template("ticker.html", d=detail)

    @app.get("/api/chart/<symbol>")
    def chart(symbol):
        if not queries.valid_ticker(symbol):
            abort(400)
        days = min(max(request.args.get("days", 90, type=int) or 90, 1), 730)
        return jsonify(symbol=symbol, points=queries.chart_points(Session(), symbol, days))

    return app
