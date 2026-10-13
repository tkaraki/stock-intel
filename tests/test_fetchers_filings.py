import logging
from datetime import UTC, date, datetime

import pytest
import requests
from sqlalchemy import select

import config
from fetchers import ark, congress, edgar
from fetchers.holdings import Holding, save_snapshot
from models import CongressTrade, Portfolio, PortfolioHolding, Signal
from scoring import SOURCE_WEIGHTS

FD_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<FinancialDisclosure>
  <Member><Prefix>Hon.</Prefix><Last>Smith</Last><First>Jane</First><Suffix />
    <FilingType>P</FilingType><StateDst>CA01</StateDst><Year>2026</Year>
    <FilingDate>10/01/2026</FilingDate><DocID>20000001</DocID></Member>
  <Member><Prefix /><Last>Old</Last><First>Bob</First><Suffix>Jr</Suffix>
    <FilingType>P</FilingType><StateDst>TX02</StateDst><Year>2026</Year>
    <FilingDate>1/15/2026</FilingDate><DocID>20000002</DocID></Member>
  <Member><Prefix /><Last>Annual</Last><First>Ann</First><Suffix />
    <FilingType>C</FilingType><StateDst>NJ08</StateDst><Year>2026</Year>
    <FilingDate>10/02/2026</FilingDate><DocID>20000003</DocID></Member>
</FinancialDisclosure>"""

PTR_TEXT = """Filing ID #20034916
ID Owner Asset Transaction Date Notification Amount Cap.
Crown Castle Inc. Common Stock S 06/30/2026 07/02/2026 $1,001 - $15,000
(CCI) [ST]
SP NVIDIA Corporation - Common Stock (NVDA) [ST] P 07/01/2026 07/02/2026 $15,001 -
$50,000
Alphabet Inc. (GOOGL) [ST] S (partial) 07/03/2026 07/04/2026 $1,001 - $15,000
Treasury Bill (3-Month, Matures P 07/13/2026 07/13/2026 $15,001 -
10/15/2026) [GS] $50,000
F S: New
"""

INFO_TABLE = """<SEC-DOCUMENT>
CONFORMED PERIOD OF REPORT:	20260630
<XML>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
<infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><cusip>037833100</cusip><value>1000</value>
<shrsOrPrnAmt><sshPrnamt>100</sshPrnamt><sshPrnType>SH</sshPrnType></shrsOrPrnAmt></infoTable>
<infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><cusip>037833100</cusip><value>500</value>
<shrsOrPrnAmt><sshPrnamt>50</sshPrnamt><sshPrnType>SH</sshPrnType></shrsOrPrnAmt></infoTable>
<infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><cusip>037833100</cusip><value>9</value>
<shrsOrPrnAmt><sshPrnamt>9</sshPrnamt></shrsOrPrnAmt><putCall>Put</putCall></infoTable>
<infoTable><nameOfIssuer>MYSTERY CO</nameOfIssuer><cusip>999999999</cusip><value>7</value>
<shrsOrPrnAmt><sshPrnamt>7</sshPrnamt></shrsOrPrnAmt></infoTable>
</informationTable>
</XML>
</SEC-DOCUMENT>"""

ARK_CSV = '''date,fund,company,ticker,cusip,shares,market value ($),weight (%)
10/12/2026,ARKK,TESLA INC,TSLA,88160R101,"1,704,290","$652,231,783.00",9.73%
10/12/2026,ARKK,CASH FUND,,X9USDGSFT,"6,629,400","$6,629,400.02",0.10%
10/12/2026,ARKK,NO CUSIP CO,NOC,,"10","$100.00",0.01%

"Investors should carefully consider the investment objectives, risks and ""NAV"" ... © 2026."
'''


def _portfolio(session, source="ark", cik=None):
    p = Portfolio(name="P", manager="M", source=source, cik=cik)
    session.add(p)
    session.commit()
    return p


# --- congress ---------------------------------------------------------------------------


def test_parse_fd_xml_keeps_recent_ptrs_only():
    out = congress.parse_fd_xml(FD_XML, today=date(2026, 10, 12))
    assert [f["doc_id"] for f in out] == ["20000001"]
    assert out[0]["member"] == "Jane Smith"
    assert out[0]["filed_date"] == date(2026, 10, 1)


def test_parse_ptr_text_rows():
    rows = congress.parse_ptr_text(PTR_TEXT)
    got = [(r["ticker"], r["transaction_type"], r["amount_range"]) for r in rows]
    assert got == [
        ("CCI", "sale", "$1,001 - $15,000"),
        ("NVDA", "purchase", "$15,001 - $50,000"),
        ("GOOGL", "sale_partial", "$1,001 - $15,000"),
        (None, "purchase", "$15,001 - $50,000"),
    ]
    assert rows[0]["transaction_date"] == date(2026, 6, 30)
    assert rows[1]["amount_low"] == 15001


def test_parse_ptr_text_unparseable():
    assert congress.parse_ptr_text("scanned image, no text") == []


def test_save_filing_rows_signals_and_placeholder(session):
    f = {
        "doc_id": "1", "member": "Jane Smith", "filed_date": date(2026, 10, 1),
        "doc_url": "http://x/1.pdf",
    }  # fmt: skip
    n = congress._save_filing(session, f, congress.parse_ptr_text(PTR_TEXT))
    assert n == 4
    sigs = session.scalars(select(Signal).order_by(Signal.ticker)).all()
    assert [(s.ticker, s.signal_type) for s in sigs] == [
        ("CCI", "sale"), ("GOOGL", "sale"), ("NVDA", "purchase"),
    ]  # fmt: skip
    assert all(s.source in SOURCE_WEIGHTS and s.score is None for s in sigs)
    assert sigs[2].raw_metric == pytest.approx(15.001)

    g = dict(f, doc_url="http://x/2.pdf")
    assert congress._save_filing(session, g, []) == 1
    t = session.scalars(select(CongressTrade).where(CongressTrade.doc_url == g["doc_url"])).one()
    assert t.ticker is None and t.filed_date == date(2026, 10, 1) and t.chamber == "house"


def test_congress_run_network_error_returns_zero(session, monkeypatch, caplog):
    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(congress.requests, "get", boom)
    with caplog.at_level(logging.WARNING):
        assert congress.run(session) == 0
    assert "failed" in caplog.text


# --- edgar ------------------------------------------------------------------------------


def test_normalize_name():
    assert edgar.normalize_name("Apple Inc.") == "APPLE"
    assert edgar.normalize_name("The Coca-Cola Co") == "COCA COLA"
    assert edgar.normalize_name("ALPHABET INC CL A") == "ALPHABET"
    assert edgar.normalize_name("Berkshire Hathaway Inc. Class B") == "BERKSHIRE HATHAWAY"
    assert edgar.normalize_name("AT&T INC") == "AT AND T"


def test_build_ticker_map_and_info_table_mapping():
    tmap = edgar.build_ticker_map(
        {
            "0": {"cik_str": 1, "ticker": "AAPL", "title": "Apple Inc."},
            "1": {"cik_str": 2, "ticker": "AAPL2", "title": "APPLE CORP"},
        }
    )
    assert tmap == {"APPLE": "AAPL"}
    hs = {h.security_key: h for h in edgar.parse_info_table(INFO_TABLE, tmap)}
    assert hs["037833100"].shares == 150 and hs["037833100"].value == 1500  # aggregated
    assert len(hs) == 2  # put row ignored
    assert hs["037833100"].ticker == "AAPL"
    assert hs["999999999"].ticker is None


def test_parse_period_of_report():
    assert edgar.parse_period_of_report(INFO_TABLE) == date(2026, 6, 30)
    assert edgar.parse_period_of_report("nothing") is None


def test_load_ticker_map_uses_fresh_cache(tmp_path, monkeypatch):
    cache = tmp_path / "t.json"
    cache.write_text('{"0": {"ticker": "AAPL", "title": "Apple Inc."}}')
    monkeypatch.setattr(edgar.requests, "get", lambda *a, **k: pytest.fail("should not fetch"))
    assert edgar.load_ticker_map(cache) == {"APPLE": "AAPL"}


def test_edgar_run_skips_when_unconfigured(session, monkeypatch, caplog):
    monkeypatch.setattr(config, "SEC_EDGAR_EMAIL", None)
    with caplog.at_level(logging.WARNING):
        assert edgar.run(session) == 0
    assert "skipping" in caplog.text and "not configured" in caplog.text


def test_edgar_run_error_returns_zero(session, monkeypatch, caplog):
    monkeypatch.setattr(config, "SEC_EDGAR_COMPANY_NAME", "Test")
    monkeypatch.setattr(config, "SEC_EDGAR_EMAIL", "t@example.com")
    _portfolio(session, "edgar", "0001")

    class Boom:
        def __init__(self, *a, **k):
            raise requests.ConnectionError("down")

    monkeypatch.setattr("sec_edgar_downloader.Downloader", Boom)
    with caplog.at_level(logging.ERROR):
        assert edgar.run(session) == 0
    assert "EDGAR fetcher failed" in caplog.text


# --- ark --------------------------------------------------------------------------------


def test_parse_ark_csv_ignores_disclaimer():
    as_of, hs = ark.parse_ark_csv(ARK_CSV)
    assert as_of == date(2026, 10, 12)
    assert [h.security_key for h in hs] == ["88160R101", "X9USDGSFT", "NOC"]
    assert hs[0].shares == 1704290 and hs[0].value == pytest.approx(652231783.00)
    assert hs[1].ticker is None and hs[2].cusip is None
    assert ark.parse_ark_csv("<html>error</html>") is None


def test_ark_run_error_returns_zero(session, monkeypatch, caplog):
    _portfolio(session)

    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(ark.requests, "get", boom)
    with caplog.at_level(logging.WARNING):
        assert ark.run(session) == 0
    assert "failed" in caplog.text


# --- holdings ---------------------------------------------------------------------------


def _h(key, shares, ticker=None):
    return Holding(key, key, shares, shares * 10, ticker, None)


def test_save_snapshot_first_snapshot_emits_nothing(session):
    p = _portfolio(session)
    n = save_snapshot(session, p, date(2026, 6, 30), [_h("A", 100, "AAA")], "portfolio_ark")
    assert n == 1
    assert session.scalars(select(Signal)).all() == []


def test_save_snapshot_diff_emits_once(session):
    p = _portfolio(session)
    q1 = [_h("A", 100, "AAA"), _h("B", 100, "BBB"), _h("C", 100, "CCC"), _h("D", 100)]
    q2 = [_h("A", 150, "AAA"), _h("C", 101, "CCC"), _h("E", 5, "EEE"), _h("F", 5)]
    save_snapshot(session, p, date(2026, 3, 31), q1, "portfolio_13f")
    assert save_snapshot(session, p, date(2026, 6, 30), q2, "portfolio_13f") == 4
    sigs = {s.ticker: s for s in session.scalars(select(Signal))}
    # C within threshold, D/F have no ticker
    assert {t: (s.signal_type, s.raw_metric) for t, s in sigs.items()} == {
        "AAA": ("increased", pytest.approx(50.0)),
        "BBB": ("exit", 100.0),
        "EEE": ("new", 100.0),
    }
    assert sigs["AAA"].source == "portfolio_13f" and sigs["AAA"].score is None
    assert sigs["AAA"].payload == {
        "portfolio": "P", "manager": "M", "prior_shares": 100.0, "current_shares": 150.0,
        "pct_change": 0.5, "as_of": "2026-06-30",
    }  # fmt: skip

    # rerun: nothing saved, nothing emitted
    assert save_snapshot(session, p, date(2026, 6, 30), q2, "portfolio_13f") == 0
    assert len(session.scalars(select(Signal)).all()) == 3
    assert len(session.scalars(select(PortfolioHolding)).all()) == 8


def test_signal_observed_at_is_utc(session):
    p = _portfolio(session)
    save_snapshot(session, p, date(2026, 3, 31), [_h("A", 1, "AAA")], "portfolio_ark")
    save_snapshot(session, p, date(2026, 6, 30), [_h("A", 9, "AAA")], "portfolio_ark")
    s = session.scalars(select(Signal)).one()
    assert s.observed_at == datetime(2026, 6, 30, tzinfo=UTC)


def test_signal_observed_at_is_filing_date_not_now(session):
    p = _portfolio(session)
    save_snapshot(session, p, date(2026, 3, 31), [_h("A", 1, "AAA")], "portfolio_13f")
    save_snapshot(session, p, date(2026, 6, 30), [_h("A", 9, "AAA")], "portfolio_13f",
                  observed=date(2026, 8, 14))
    save_snapshot(session, p, date(2026, 9, 30), [_h("A", 1, "AAA")], "portfolio_ark")
    a, b = session.scalars(select(Signal).order_by(Signal.id)).all()
    assert a.observed_at == datetime(2026, 8, 14, tzinfo=UTC)
    assert b.observed_at == datetime(2026, 9, 30, tzinfo=UTC)  # defaults to as_of


def test_snapshot_normalizes_class_share_ticker(session):
    p = _portfolio(session)
    save_snapshot(session, p, date(2026, 3, 31), [_h("A", 1, "brk-b")], "portfolio_ark")
    assert session.scalars(select(PortfolioHolding)).one().ticker == "BRK.B"


def test_parse_filing_date():
    assert edgar.parse_filing_date("FILED AS OF DATE:\t\t20260814\n") == date(2026, 8, 14)
    assert edgar.parse_filing_date("nothing") is None
