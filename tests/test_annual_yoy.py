"""연간 항목, 정기 전표 시작월, 작년 대비 증가 원인."""
from datetime import date

import pytest

from voucher import db
from voucher.annual import due_items, mark_done
from voucher.budget import forecast_month, load_lines, yoy_increase
from voucher.pipeline import ingest_xml, post_invoice
from voucher.recurring import check_month
from voucher.schedule import month_events
from voucher.seed import build_xml, load_master

_n = iter(range(1, 10**6))


@pytest.fixture
def conn(tmp_path):
    db.init_db(tmp_path / "t.db")
    with db.connect(tmp_path / "t.db") as c:
        load_master(c)
        yield c


def _post(conn, d, sup, items):
    r = ingest_xml(conn, build_xml(f"{d:%Y%m%d}41000012{next(_n):08d}", d, sup, items), "t")
    ok, msg = post_invoice(conn, r.invoice_id, "t")
    assert ok, msg


def _item_id(conn, name):
    return conn.execute("SELECT id FROM annual_items WHERE name=?", (name,)).fetchone()[0]


def test_annual_due_and_mark_done(conn):
    july = {a.name: a for a in due_items(conn, "2026-07", date(2026, 7, 10))}
    assert july["재산세(건물분)"].status == "PENDING"
    assert "재산세(건물분)" in july and "재산세(토지분)" not in july          # 토지분은 9월
    assert {a.name for a in due_items(conn, "2026-12", date(2026, 12, 1))} >= {"자동차세", "정기 소독"}
    assert due_items(conn, "2026-08", date(2026, 9, 1))[0].status == "OVERDUE"   # 기한(8/31) 지남

    mark_done(conn, _item_id(conn, "재산세(건물분)"), "2026-07", 19_610_000, "2026-07-28", "위택스", "admin")
    done = {a.name: a for a in due_items(conn, "2026-07", date(2026, 7, 29))}["재산세(건물분)"]
    assert (done.status, done.amount) == ("DONE", 19_610_000)
    lines = load_lines(conn, "2026-07-01", "2026-07-31")
    row = lines[lines.annual].iloc[0]
    assert (row.gl, row.amount, row.cc) == ("81700000", 19_610_000, "K1200")   # 세금과공과, 총무팀


def test_unchecked_annual_goes_into_forecast(conn):
    conn.execute("INSERT INTO budgets VALUES (2026, 10, '83200000', 1000000)")
    s = next(x for x in forecast_month(conn, "2026-10", date(2026, 10, 21)) if x.gl == "83200000")
    assert s.forecast >= 1_650_000                                   # 전기 정기 안전점검 예상분
    assert any("전기 정기 안전점검" in r and "연간 항목" in r for r in s.reasons)


def test_recurring_start_month(conn):
    names_feb = {r.name for r in check_month(conn, "2026-02", date(2026, 2, 28))}
    names_apr = {r.name for r in check_month(conn, "2026-04", date(2026, 4, 30))}
    assert "사내 피트니스 제휴" not in names_feb and "사내 피트니스 제휴" in names_apr
    assert "보안관제 서비스" not in names_apr


def test_yoy_increase_names_new_contract_and_big_item(conn):
    fit = ("1198100055", "튼튼헬스(주)")
    rep = ("1268100071", "튼튼설비(주)")
    _post(conn, date(2025, 10, 10), rep, [("누수 보수", "", 2_000_000)])
    _post(conn, date(2026, 10, 14), fit, [("사내 피트니스 제휴", "", 3_000_000)])
    _post(conn, date(2026, 10, 10), rep, [("누수 보수", "", 1_000_000)])
    _post(conn, date(2026, 10, 16), rep, [("대회의실 리모델링", "", 38_000_000)])
    df, now, ly = yoy_increase(conn, "2026-10", date(2026, 10, 21), "month")
    by = df.set_index("계정")
    assert by.loc["수선비", "증가"] == 37_000_000
    assert "대회의실 리모델링" in by.loc["수선비", "원인"]
    assert "사내 피트니스 제휴 신규" in by.loc["복리후생비", "원인"]


def test_calendar_events(conn):
    ev = month_events(conn, "2026-10", date(2026, 10, 21))
    flat = [(d, e["type"], e["label"]) for d, es in ev.items() for e in es]
    assert any(t == "holiday" and "한글날" in l for d, t, l in flat)
    assert any(t == "tax" and "부가세" in l and d == 26 for d, t, l in flat)   # 10/25 일요일 → 26일
    assert any(t == "annual" and "전기 정기 안전점검" in l for d, t, l in flat)
    assert any(t == "rec" for d, t, l in flat)
