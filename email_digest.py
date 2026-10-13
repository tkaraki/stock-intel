"""Daily email digest: gather data, get AI recommendations, render Gmail-safe HTML, send."""

import html
import json
import logging
import re
import smtplib
import ssl
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

import config
import scoring
from models import (
    CongressTrade,
    Portfolio,
    PortfolioHolding,
    Signal,
    WatchlistItem,
)
from portfolio_diff import diff_holdings
from queries import format_entry_at, latest_price_row, since_entry_pct

log = logging.getLogger(__name__)

TOP_N = 5
SAMPLES_PER_TICKER = 3
SIGNAL_WINDOW = timedelta(hours=24)
PORTFOLIO_WINDOW_DAYS = 7
SMTP_TIMEOUT = 30
VALID_ACTIONS = {"buy", "hold", "watch"}

SYSTEM_PROMPT = (
    "You are a concise equity research analyst. You receive JSON with the day's top signals, "
    "watchlist, congress trades and portfolio changes. For each ticker in the data's "
    "top_signals, give an action in {buy, hold, watch} and a one-to-two sentence rationale "
    "citing the data. Explicitly flag convergence (convergence=true) when the data's "
    "converging_sources lists 2 or more sources agreeing. Never invent data that was not "
    "provided. This is informational, not financial advice. Respond with JSON only, no prose, "
    'in the form {"recommendations":[{"ticker":"","action":"","rationale":"",'
    '"convergence":false}]}.'
)


# --- 1. data -----------------------------------------------------------------------------


def _as_utc(now: datetime | None) -> datetime:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now


def _top_signals(session: Session, now: datetime) -> list[dict]:
    cutoff = now - SIGNAL_WINDOW
    rows = session.scalars(select(Signal).where(Signal.observed_at >= cutoff)).all()
    by_ticker: dict[str, list[Signal]] = {}
    for s in rows:
        by_ticker.setdefault(s.ticker, []).append(s)
    ranked = []
    for ticker, sigs in by_ticker.items():
        try:
            score, sources = scoring.aggregate_ticker_score(sigs, now)
        except scoring.UnknownSourceError as exc:
            log.warning("skipping %s in digest: %s", ticker, exc)
            continue
        converging = scoring.converging_sources(sigs, now)
        samples = sorted(sigs, key=lambda s: s.raw_metric, reverse=True)[:SAMPLES_PER_TICKER]
        ranked.append(
            {
                "ticker": ticker,
                "score": round(score, 2),
                "sources": sorted(sources),
                "converging_sources": sorted(converging),
                "signals": [
                    {
                        "source": s.source,
                        "type": s.signal_type,
                        "raw_metric": s.raw_metric,
                        "sentiment": s.sentiment,
                        "headline": (s.payload or {}).get("title")
                        or (s.payload or {}).get("headline"),
                    }
                    for s in samples
                ],
            }
        )
    ranked.sort(key=lambda r: (-r["score"], r["ticker"]))
    return ranked[:TOP_N]


def _watchlist(session: Session) -> list[dict]:
    out = []
    for item in session.scalars(select(WatchlistItem).order_by(WatchlistItem.ticker)):
        row = latest_price_row(session, item.ticker)
        close = row.close if row else None
        pct = since_entry_pct(item, row)
        out.append(
            {
                "ticker": item.ticker,
                "entry_price": item.entry_price,
                "entry_at": item.entry_at,
                "latest_close": close,
                "pnl_pct": None if pct is None else round(pct, 2),
            }
        )
    return out


def _congress_since_last(session: Session, local_today: date) -> list[dict]:
    """Filings since the previous digest: Fri-Sun on Mondays, otherwise just yesterday."""
    days_back = 3 if local_today.weekday() == 0 else 1
    start = local_today - timedelta(days=days_back)
    rows = session.scalars(
        select(CongressTrade)
        .where(CongressTrade.filed_date >= start, CongressTrade.filed_date < local_today)
        .order_by(CongressTrade.filed_date, CongressTrade.member, CongressTrade.ticker)
    )
    return [
        {
            "member": t.member,
            "chamber": t.chamber,
            "ticker": t.ticker,
            "transaction_type": t.transaction_type,
            "amount_range": t.amount_range,
            "transaction_date": t.transaction_date.isoformat(),
        }
        for t in rows
    ]


def _portfolio_changes(session: Session, today: date) -> list[dict]:
    out = []
    for pf in session.scalars(select(Portfolio).order_by(Portfolio.name)):
        dates = session.scalars(
            select(PortfolioHolding.as_of)
            .where(PortfolioHolding.portfolio_id == pf.id)
            .distinct()
            .order_by(PortfolioHolding.as_of.desc())
            .limit(2)
        ).all()
        if len(dates) < 2 or (today - dates[0]).days > PORTFOLIO_WINDOW_DAYS:
            continue

        def snapshot(as_of: date, pf=pf):
            rows = session.scalars(
                select(PortfolioHolding).where(
                    PortfolioHolding.portfolio_id == pf.id, PortfolioHolding.as_of == as_of
                )
            ).all()
            return (
                {r.security_key: r.shares for r in rows},
                {r.security_key: r.ticker or r.name for r in rows},
            )

        new_sh, new_names = snapshot(dates[0])
        old_sh, old_names = snapshot(dates[1])
        names = {**old_names, **new_names}
        changes = [
            {
                "ticker": names.get(c.ticker, c.ticker),
                "change": c.change_type.value,
                "prior_shares": c.prior_shares,
                "current_shares": c.current_shares,
                "pct_change": None if c.pct_change is None else round(c.pct_change * 100, 1),
            }
            for c in diff_holdings(old_sh, new_sh)
        ]
        if changes:
            out.append(
                {
                    "portfolio": pf.name,
                    "manager": pf.manager,
                    "as_of": dates[0].isoformat(),
                    "changes": changes,
                }
            )
    return out


def gather_digest_data(session: Session, now: datetime | None = None) -> dict:
    now = _as_utc(now)
    local_today = now.astimezone(ZoneInfo(config.SCHEDULER_TZ)).date()
    return {
        "generated_at": now.isoformat(),
        "top_signals": _top_signals(session, now),
        "watchlist": _watchlist(session),
        "congress_trades": _congress_since_last(session, local_today),
        "portfolio_changes": _portfolio_changes(session, local_today),
    }


# --- 2. recommendations ------------------------------------------------------------------


def parse_recommendations(text: str) -> list[dict]:
    """Parse the model's JSON reply; strip code fences, drop malformed items."""
    text = text.strip()
    fence = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    payload = json.loads(text)
    items = payload.get("recommendations") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError("missing recommendations list")
    recs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ticker = item.get("ticker")
        action = str(item.get("action", "")).strip().lower()
        rationale = item.get("rationale")
        if not (isinstance(ticker, str) and ticker.strip()):
            continue
        if action not in VALID_ACTIONS or not isinstance(rationale, str) or not rationale.strip():
            continue
        recs.append(
            {
                "ticker": ticker.strip().upper(),
                "action": action,
                "rationale": rationale.strip(),
                "convergence": item.get("convergence") is True,
            }
        )
    return recs


def get_recommendations(data: dict, client=None) -> list[dict] | None:
    """Ask Claude for per-ticker recommendations. None on missing key, API or parse error."""
    try:
        if client is None:
            if not config.ANTHROPIC_API_KEY:
                log.warning("ANTHROPIC_API_KEY not set; digest will omit AI recommendations")
                return None
            import anthropic

            client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(data, default=str)}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "text") == "text")
        return parse_recommendations(text)
    except Exception:
        log.exception("AI recommendations failed; digest will omit them")
        return None


# --- 3. rendering ------------------------------------------------------------------------

_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
_ACTION_COLORS = {"buy": "#1a7f37", "hold": "#6e7781", "watch": "#9a6700"}


def _e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _section(title: str, body: str) -> str:
    return (
        '<tr><td style="padding:16px 24px 0 24px;">'
        f'<h2 style="margin:0 0 8px 0;font-size:16px;color:#111827;font-family:{_FONT};">'
        f"{_e(title)}</h2>{body}</td></tr>"
    )


def _p(text: str, color: str = "#374151", size: int = 14) -> str:
    return (
        f'<p style="margin:4px 0;font-size:{size}px;line-height:1.4;color:{color};'
        f'font-family:{_FONT};">{text}</p>'
    )


def _badge(text: str, color: str) -> str:
    return (
        f'<span style="display:inline-block;padding:1px 6px;border-radius:3px;'
        f'background-color:{color};color:#ffffff;font-size:11px;font-weight:bold;">'
        f"{_e(text)}</span>"
    )


def _render_recs(recs: list[dict] | None) -> str:
    if recs is None:
        return _p("AI recommendations are unavailable for this digest.", "#6b7280")
    if not recs:
        return _p("No recommendations were returned.", "#6b7280")
    parts = []
    for r in recs:
        conv = " " + _badge("CONVERGENCE", "#8250df") if r["convergence"] else ""
        parts.append(
            _p(
                f"<strong>{_e(r['ticker'])}</strong> "
                f"{_badge(r['action'].upper(), _ACTION_COLORS.get(r['action'], '#6e7781'))}"
                f"{conv}<br>{_e(r['rationale'])}"
            )
        )
    return "".join(parts)


def _render_signals(signals: list[dict]) -> str:
    if not signals:
        return _p("No signals in the last 24 hours.", "#6b7280")
    parts = []
    for s in signals:
        conv = ""
        if len(s["converging_sources"]) >= 2:
            conv = " " + _badge("CONVERGENCE: " + ", ".join(s["converging_sources"]), "#8250df")
        lines = []
        for sig in s["signals"]:
            head = f" - {_e(sig['headline'])}" if sig.get("headline") else ""
            lines.append(f"{_e(sig['source'])}/{_e(sig['type'])}{head}")
        parts.append(
            _p(
                f"<strong>{_e(s['ticker'])}</strong> score {s['score']:.1f}{conv}"
                f"<br><span style=\"color:#6b7280;font-size:12px;\">{'<br>'.join(lines)}</span>"
            )
        )
    return "".join(parts)


def _render_watchlist(items: list[dict]) -> str:
    if not items:
        return _p("Watchlist is empty.", "#6b7280")
    rows = []
    for w in items:
        pct = w["pnl_pct"]
        if pct is None:
            cell = '<span style="color:#6b7280;">n/a</span>'
        else:
            color = "#1a7f37" if pct >= 0 else "#cf222e"
            cell = f'<span style="color:{color};font-weight:bold;">{pct:+.2f}%</span>'
        close = "n/a" if w["latest_close"] is None else f"{w['latest_close']:.2f}"
        entry = "n/a" if w["entry_price"] is None else f"{w['entry_price']:.2f}"
        if w.get("entry_at") is not None:
            entry += f" ({_e(format_entry_at(w['entry_at']))})"
        rows.append(
            f'<tr><td style="padding:3px 0;font-size:14px;font-family:{_FONT};">'
            f"<strong>{_e(w['ticker'])}</strong></td>"
            f'<td align="right" style="font-size:13px;color:#6b7280;font-family:{_FONT};">'
            f"{entry} &rarr; {close}</td>"
            f'<td align="right" style="font-size:14px;font-family:{_FONT};">{cell}</td></tr>'
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        f"{''.join(rows)}</table>"
    )


def _render_congress(trades: list[dict]) -> str:
    if not trades:
        return _p("No congress trades filed since the last digest.", "#6b7280")
    return "".join(
        _p(
            f"<strong>{_e(t['member'])}</strong> ({_e(t['chamber'])}) "
            f"{_e(t['transaction_type'])} {_e(t['ticker'] or 'n/a')} "
            f"{_e(t['amount_range'] or '')}"
        )
        for t in trades
    )


def _render_portfolios(pfs: list[dict]) -> str:
    if not pfs:
        return _p("No recent portfolio changes.", "#6b7280")
    parts = []
    for pf in pfs:
        lines = []
        for c in pf["changes"]:
            pct = "" if c["pct_change"] is None else f" ({c['pct_change']:+.1f}%)"
            lines.append(f"{_e(c['ticker'])}: {_e(c['change'])}{pct}")
        parts.append(
            _p(
                f"<strong>{_e(pf['portfolio'])}</strong> as of {_e(pf['as_of'])}"
                f"<br>{'<br>'.join(lines)}"
            )
        )
    return "".join(parts)


def render_digest_html(data: dict, recs: list[dict] | None, now: datetime | None = None) -> str:
    now = _as_utc(now)
    date_str = now.astimezone(ZoneInfo(config.SCHEDULER_TZ)).strftime("%Y-%m-%d")
    sections = "".join(
        [
            _section("AI recommendations", _render_recs(recs)),
            _section("Top signals", _render_signals(data.get("top_signals", []))),
            _section("Watchlist P&L", _render_watchlist(data.get("watchlist", []))),
            _section(
                "Congress trades filed since last digest",
                _render_congress(data.get("congress_trades", [])),
            ),
            _section("Portfolio changes", _render_portfolios(data.get("portfolio_changes", []))),
        ]
    )
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Stock Intel digest - {_e(date_str)}</title></head>"
        f'<body style="margin:0;padding:0;background-color:#f3f4f6;font-family:{_FONT};">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background-color:#f3f4f6;"><tr><td align="center" style="padding:16px;">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        'style="width:100%;max-width:600px;background-color:#ffffff;border-radius:6px;">'
        '<tr><td style="padding:20px 24px;background-color:#111827;border-radius:6px 6px 0 0;">'
        f'<h1 style="margin:0;font-size:20px;color:#ffffff;font-family:{_FONT};">'
        f"Stock Intel digest</h1>"
        f'<p style="margin:4px 0 0 0;font-size:13px;color:#9ca3af;font-family:{_FONT};">'
        f"{_e(date_str)}</p></td></tr>"
        f"{sections}"
        f'<tr><td style="padding:20px 24px;font-size:12px;color:#6b7280;font-family:{_FONT};">'
        "Informational only &mdash; not financial advice.</td></tr>"
        "</table></td></tr></table></body></html>"
    )


# --- 4. send -----------------------------------------------------------------------------


def build_digest(session: Session, now: datetime | None = None, client=None) -> str:
    now = _as_utc(now)
    data = gather_digest_data(session, now)
    recs = get_recommendations(data, client=client)
    return render_digest_html(data, recs, now)


def _smtp_ready() -> bool:
    return bool(
        config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASSWORD and config.DIGEST_TO_EMAIL
    )


def _build_message(body: str, now: datetime) -> EmailMessage:
    date_str = now.astimezone(ZoneInfo(config.SCHEDULER_TZ)).strftime("%Y-%m-%d")
    msg = EmailMessage()
    msg["Subject"] = f"Stock Intel digest — {date_str}"
    # Most providers (e.g. Gmail) only send as the signed-in account, so default to it.
    msg["From"] = config.DIGEST_FROM_EMAIL or config.SMTP_USER
    msg["To"] = config.DIGEST_TO_EMAIL
    msg.set_content("This digest is formatted as HTML; open it in an HTML-capable mail client.")
    msg.add_alternative(body, subtype="html")
    return msg


def _smtp_send(msg: EmailMessage) -> None:
    """Send over SMTP: implicit TLS on port 465, otherwise upgrade with STARTTLS."""
    context = ssl.create_default_context()
    if config.SMTP_PORT == 465:
        server = smtplib.SMTP_SSL(
            config.SMTP_HOST, config.SMTP_PORT, context=context, timeout=SMTP_TIMEOUT
        )
    else:
        server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=SMTP_TIMEOUT)
    with server:
        if config.SMTP_PORT != 465:
            server.starttls(context=context)
        server.login(config.SMTP_USER, config.SMTP_PASSWORD)
        server.send_message(msg)


def send_digest(
    now: datetime | None = None, session: Session | None = None, client=None
) -> bool:
    """Build the digest, save it if DIGEST_SAVE_PATH is set, and email it over SMTP.

    Returns True if emailed; never raises.
    """
    own_session = session is None
    try:
        now = _as_utc(now)
        if not _smtp_ready() and not config.DIGEST_SAVE_PATH:
            log.warning("SMTP settings and DIGEST_SAVE_PATH not set; digest skipped")
            return False
        if own_session:
            from db import get_session

            session = get_session()
        body = build_digest(session, now, client=client)
        if config.DIGEST_SAVE_PATH:
            path = Path(config.DIGEST_SAVE_PATH)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            log.info("digest saved to %s", path)
        if not _smtp_ready():
            log.warning("SMTP settings not set; digest saved but not emailed")
            return False
        _smtp_send(_build_message(body, now))
        log.info("digest sent to %s", config.DIGEST_TO_EMAIL)
        return True
    except Exception:
        log.exception("send_digest failed")
        return False
    finally:
        if own_session and session is not None:
            session.close()

if __name__ == "__main__":
    config.setup_logging()
    from db import get_session

    with get_session() as _s:
        _html = build_digest(_s)
    _out = config.LOG_DIR / "digest_preview.html"
    _out.parent.mkdir(parents=True, exist_ok=True)
    _out.write_text(_html, encoding="utf-8")
    print(f"wrote {_out}")
