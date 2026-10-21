import json
from datetime import date

import pytest

from voucher import db
from voucher.classifier import split_amount
from voucher.parser import parse_xml_bytes
from voucher.pipeline import ingest_xml, post_invoice
from voucher.recurring import check_month, mark
from voucher.seed import build_xml, load_master


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "t.db"
    db.init_db(path)
    with db.connect(path) as c:
        load_master(c)
        yield c


LEASE = ("1168100001", "한빛캐피탈(주)")
TEL = ("1048100002", "누리텔레콤(주)")


def _xml(no, d, sup, items, **kw):
    return build_xml(f"{d:%Y%m%d}41000012{no:08d}", d, sup, items, **kw)


def test_parse_standard_xml():
    inv = parse_xml_bytes(_xml(1, date(2026, 10, 5), LEASE, [("차량 리스료 12가 3456", "10월분", 1_500_000)]))
    assert inv.approval_no == "2026100541000012" + "00000001"
    assert inv.issue_date == "2026-10-05"
    assert (inv.supply_amount, inv.tax_amount, inv.total_amount) == (1_500_000, 150_000, 1_650_000)
    assert inv.items[0].name == "차량 리스료 12가 3456"


def test_split_amount_keeps_total():
    assert split_amount(100, [1, 1, 1]) == [33, 33, 34]
    assert sum(split_amount(-11_111, [40, 35, 25])) == -11_111


def test_vehicle_non_deductible_and_balance(conn):
    r = ingest_xml(conn, _xml(2, date(2026, 10, 5), LEASE,
                              [("차량 리스료 12가 3456", "", 1_500_000), ("차량 리스료 80바 1234", "", 800_000)]), "t")
    assert r.status == "NEW_READY"
    lines = json.loads(conn.execute("SELECT proposal_json FROM invoices WHERE id=?", (r.invoice_id,))
                       .fetchone()[0])["lines"]
    car, truck = lines
    assert (car["cost_center"], car["user_id"], car["tax_code"], car["amount"], car["tax_amount"]) == \
           ("K2100", "E1023", "V3", 1_650_000, 0)          # 승용차: 불공제 → 세액이 비용에 포함
    assert (truck["tax_code"], truck["amount"], truck["tax_amount"]) == ("V1", 800_000, 80_000)

    ok, msg = post_invoice(conn, r.invoice_id, "test")
    assert ok, msg
    payload = json.loads(conn.execute("SELECT payload_json FROM postings").fetchone()[0])
    assert sum(a["AMT_DOCCUR"] for a in payload["CURRENCYAMOUNT"]) == 0
    assert payload["DOCUMENTHEADER"]["DOC_TYPE"] == "KR"


def test_allocation_split(conn):
    r = ingest_xml(conn, _xml(3, date(2026, 10, 17), TEL, [("인터넷 요금", "", 520_000)]), "t")
    lines = json.loads(conn.execute("SELECT proposal_json FROM invoices WHERE id=?", (r.invoice_id,))
                       .fetchone()[0])["lines"]
    ratios = conn.execute("SELECT cost_center, ratio FROM allocation_keys WHERE key='OFFICE_COMMON'").fetchall()
    assert [l["cost_center"] for l in lines] == [r[0] for r in ratios]
    assert sum(l["amount"] for l in lines) == 520_000
    total = sum(r[1] for r in ratios)
    for l, r in zip(lines[:-1], ratios[:-1]):
        assert l["amount"] == int(520_000 * r[1] / total)


def test_unknown_vendor_goes_to_review(conn):
    r = ingest_xml(conn, _xml(4, date(2026, 10, 18), ("2118100098", "새봄문구"), [("A4 용지", "", 45_000)]), "t")
    assert r.status == "NEW_REVIEW"
    assert "공급업체 미등록" in r.message
    ok, _ = post_invoice(conn, r.invoice_id, "test")
    assert not ok


def test_duplicate_is_skipped(conn):
    xml = _xml(5, date(2026, 10, 17), TEL, [("인터넷 요금", "", 520_000)])
    assert ingest_xml(conn, xml, "mail").status == "NEW_READY"
    assert ingest_xml(conn, xml, "hometax").status == "DUPLICATE"


def test_modified_invoice_is_credit_memo(conn):
    r = ingest_xml(conn, _xml(6, date(2026, 10, 15), ("3128100006", "새솔주유소"),
                              [("휘발유 12가3456", "", -10_000)], type_code="0201"), "t")
    ok, msg = post_invoice(conn, r.invoice_id, "test")
    assert ok, msg
    payload = json.loads(conn.execute("SELECT payload_json FROM postings").fetchone()[0])
    assert payload["DOCUMENTHEADER"]["DOC_TYPE"] == "KG"
    assert payload["CURRENCYAMOUNT"][0]["AMT_DOCCUR"] == 11_000   # 공급업체 차변(대변 반대)


def test_recurring_statuses(conn):
    conn.execute("DELETE FROM recurring_vouchers")
    conn.executemany(
        "INSERT INTO recurring_vouchers(name, supplier_bizno, keyword, expected_amount, tolerance_pct, due_day)"
        " VALUES (?,?,?,?,?,?)", [
            ("리스", "1168100001", None, 2_530_000, 0, 10),
            ("통신", "1048100002", None, 572_000, 10, 15),
            ("임차료", "2208100003", None, 11_000_000, 0, 5),
            ("복합기", "1358100004", None, 330_000, 10, 10),
            ("정수기", "1418100005", None, 88_000, 0, 25),
        ])
    r = ingest_xml(conn, _xml(7, date(2026, 10, 5), LEASE,
                              [("차량 리스료 12가 3456", "", 1_500_000), ("차량 리스료 80바 1234", "", 800_000)]), "t")
    post_invoice(conn, r.invoice_id, "test")
    ingest_xml(conn, _xml(8, date(2026, 10, 17), TEL, [("인터넷 요금", "", 520_000)]), "t")   # 미전기
    r = ingest_xml(conn, _xml(9, date(2026, 10, 8), ("1358100004", "마루오피스렌탈"),
                              [("복합기 렌탈", "", 360_000)]), "t")
    post_invoice(conn, r.invoice_id, "test")

    st = {s.name: s.status for s in check_month(conn, "2026-10", today=date(2026, 10, 21))}
    assert st == {"리스": "DONE", "통신": "NOT_POSTED", "임차료": "OVERDUE", "복합기": "AMOUNT_DIFF",
                  "정수기": "WAITING"}

    rid = conn.execute("SELECT id FROM recurring_vouchers WHERE name='복합기'").fetchone()[0]
    mark(conn, rid, "2026-10", "CONFIRMED", "초과요금", "test")
    st = {s.name: s.status for s in check_month(conn, "2026-10", today=date(2026, 10, 21))}
    assert st["복합기"] == "CONFIRMED"
