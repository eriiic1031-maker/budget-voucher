"""예산 예측·경고 이유, 정기/비정기 구분."""
from datetime import date

import pytest

from voucher import db
from voucher.budget import forecast_month, invoice_overview
from voucher.pipeline import ingest_xml, post_invoice
from voucher.recurring import mark_checked, recurring_map
from voucher.seed import build_xml, load_master

REPAIR = ("1268100071", "튼튼설비(주)")      # 수선비 82000000, 비정기
FOOD = ("2148100011", "푸른식탁(주)")        # 복리후생비, 정기(구내식당 위탁)
STATIONERY = ("2118100099", "온누리문구")    # 소모품비, 비정기
COURIER = ("1248100010", "한결로지스(주)")   # 운반비 - 물류팀 예산(총무 아님)
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
    return r.invoice_id


def _budget(conn, gl, amount, y=2026, m=10):
    conn.execute("INSERT OR REPLACE INTO budgets VALUES (?,?,?,?)", (y, m, gl, amount))


def test_over_budget_reason_names_big_item(conn):
    _budget(conn, "82000000", 10_000_000)
    _post(conn, date(2025, 10, 10), REPAIR, [("누수 보수", "", 8_000_000)])
    _post(conn, date(2026, 10, 16), REPAIR, [("대회의실 리모델링", "", 38_000_000)])
    s = next(x for x in forecast_month(conn, "2026-10", date(2026, 10, 21)) if x.gl == "82000000")
    assert s.status == "OVER" and s.actual == 38_000_000
    assert any("대회의실 리모델링" in r for r in s.reasons)
    assert any("넘었습니다" in r for r in s.reasons)


def test_risk_from_unreceived_recurring_and_pace(conn):
    # 복리후생비 예산 1억 7천, 작년 10월 식당 1억 4,400 / 올해 식당은 아직 미수신(기한 30일)
    _budget(conn, "81100000", 170_000_000)
    for m in range(1, 13):
        _post(conn, date(2025, m, 27), FOOD, [("구내식당 위탁", "", 160_000_000 if m != 10 else 144_000_000)])
    _post(conn, date(2026, 10, 20), ("1138100081", "달콤다과(주)"), [("창립기념 행사 케이터링", "", 30_000_000)])
    s = next(x for x in forecast_month(conn, "2026-10", date(2026, 10, 21)) if x.gl == "81100000")
    assert s.recurring_remaining > 140_000_000              # 작년 같은 달 패턴(명절 0.9) 반영 추정
    assert s.status == "RISK" and s.forecast > s.budget
    assert any("구내식당 위탁" in r and "예정" in r for r in s.reasons)
    assert any("창립기념" in r for r in s.reasons)


def test_pace_reason_when_spending_faster_than_last_year(conn):
    _budget(conn, "83000000", 6_000_000)
    for d in (5, 15, 25):
        _post(conn, date(2025, 10, d), STATIONERY, [("사무용품", "", 1_500_000)])
    for d in (2, 6, 9, 12):
        _post(conn, date(2026, 10, d), STATIONERY, [("사무용품", "", 1_400_000)])
    s = next(x for x in forecast_month(conn, "2026-10", date(2026, 10, 12)) if x.gl == "83000000")
    assert s.status in ("RISK", "OVER")
    assert any("빠릅니다" in r for r in s.reasons)


def test_non_gm_accounts_excluded_and_recurring_split(conn):
    courier = _post(conn, date(2026, 10, 3), COURIER, [("택배 운송료", "", 5_000_000)])
    food = _post(conn, date(2026, 10, 27), FOOD, [("구내식당 위탁", "", 1_000_000)])
    stat = _post(conn, date(2026, 10, 4), STATIONERY, [("사무용품", "", 300_000)])
    assert all(s.gl != "82400000" for s in forecast_month(conn, "2026-10", date(2026, 10, 28)))
    ov = invoice_overview(conn, "2026-10")                  # 기본은 총무 예산만
    assert set(ov.invoice_id) == {food, stat}
    rec = recurring_map(conn, "2026-10-01", "2026-10-31")
    assert rec.get(food) == "구내식당 위탁" and stat not in rec
    mark_checked(conn, [stat], "admin", "확인")
    ov = invoice_overview(conn, "2026-10", owner=None)
    assert ov.set_index("invoice_id").loc[stat, "checked_by"] == "admin"
    assert courier in set(ov.invoice_id)


def test_overview_with_unclassified_invoice(conn):
    """계정이 아직 없는 검토 대기 건이 섞여도 점검 화면 데이터가 깨지지 않는다."""
    r = ingest_xml(conn, build_xml("202610184100001299999999", date(2026, 10, 18), ("2118100098", "새봄문구"),
                                   [("탁상 달력 제작", "", 1_850_000)]), "t")
    ov = invoice_overview(conn, "2026-10", owner=None)
    row = ov.set_index("invoice_id").loc[r.invoice_id]
    assert row.account == "미분류" and row.status == "REVIEW" and not row.recurring
