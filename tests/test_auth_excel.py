import json
from datetime import date

import pytest

from voucher import db
from voucher.auth import authenticate, change_password, hash_password, upsert_user, verify_password
from voucher.classifier import Proposal
from voucher.hometax import reconcile, sync
from voucher.hometax_excel import ExcelHometaxClient, HometaxExcelError
from voucher.pipeline import ingest_xml, post_invoice, submit_invoice
from voucher.seed import build_xml, hometax_only_invoices, load_master, write_hometax_excel_sample


@pytest.fixture
def conn(tmp_path):
    db.init_db(tmp_path / "t.db")
    with db.connect(tmp_path / "t.db") as c:
        load_master(c)
        yield c


def test_password_hash_and_login(conn):
    h = hash_password("s3cret-pass")
    assert verify_password("s3cret-pass", h) and not verify_password("wrong", h)
    assert h != hash_password("s3cret-pass")          # 솔트
    upsert_user(conn, "E9", "테스터", "user", "K2100", "s3cret-pass")
    u = authenticate(conn, "E9", "s3cret-pass")
    assert u and not u.is_admin and u.cost_center_name == "영업1팀"
    assert authenticate(conn, "E9", "nope") is None
    upsert_user(conn, "E9", "테스터", "user", "K2100", active=False)
    assert authenticate(conn, "E9", "s3cret-pass") is None   # 사용 중지
    upsert_user(conn, "E9", "테스터", "admin", "K2100", active=True)
    assert change_password(conn, "E9", "s3cret-pass", "new-pass-123")
    assert authenticate(conn, "E9", "new-pass-123").is_admin


def test_user_submit_then_admin_post(conn):
    r = ingest_xml(conn, build_xml("202610194100001200000001", date(2026, 10, 19), ("6058100066", "동네주유소"),
                                   [("주유", "", 70_000)]), "t")
    p = Proposal.from_dict(json.loads(conn.execute("SELECT proposal_json FROM invoices WHERE id=?",
                                                   (r.invoice_id,)).fetchone()[0]))
    ok, msg = submit_invoice(conn, r.invoice_id, p, "E1023")
    assert not ok and "코스트센터" in msg                   # 부서가 비면 요청 불가
    for l in p.lines:
        l.cost_center = "K2100"
    ok, _ = submit_invoice(conn, r.invoice_id, p, "E1023", "출장 주유")   # 공급업체 코드는 몰라도 요청 가능
    assert ok
    ok, msg = post_invoice(conn, r.invoice_id, "admin")
    assert not ok and "공급업체" in msg                     # 관리자 전기 때는 필수
    p.sap_vendor = "200013"
    conn.execute("UPDATE invoices SET proposal_json=? WHERE id=?", (json.dumps(p.to_dict()), r.invoice_id))
    row = conn.execute("SELECT status, submitted_by FROM invoices WHERE id=?", (r.invoice_id,)).fetchone()
    assert tuple(row) == ("SUBMITTED", "E1023")
    ok, msg = post_invoice(conn, r.invoice_id, "admin")
    assert ok, msg


def test_hometax_excel_import(conn, tmp_path):
    mailed = build_xml("202610124100001200000002", date(2026, 10, 12), ("1048100002", "누리텔레콤(주)"),
                       [("인터넷/전화 요금", "10월분", 500_000)])
    ingest_xml(conn, mailed, "mail")
    path = tmp_path / "ht.xlsx"
    write_hometax_excel_sample([mailed] + [x for _, x in hometax_only_invoices()], path)

    client = ExcelHometaxClient(path.read_bytes(), path.name)
    assert len(client.records) == 3 and client.period == (date(2026, 10, 1), date(2026, 10, 13))
    rent = next(i for i in client.invoices.values() if i.supplier_bizno == "2208100003")
    assert (rent.total_amount, rent.items[0].name) == (362_560_000, "본사 임차료")

    r = sync(conn, *client.period, client=client)
    assert (r.fetched, r.new_in_hometax, r.ingested) == (3, 2, 2)
    src = conn.execute("SELECT status, source FROM invoices WHERE supplier_bizno='2208100003'").fetchone()
    assert tuple(src) == ("READY", "hometax_excel")        # 규칙대로 자동 분류됨
    assert sorted(reconcile(conn, "2026-10").recon) == ["MATCH"] * 3


def test_excel_without_approval_column_is_rejected(tmp_path):
    import pandas as pd
    p = tmp_path / "x.xlsx"
    pd.DataFrame([["아무거나", "표"], [1, 2]]).to_excel(p, header=False, index=False)
    with pytest.raises(HometaxExcelError):
        ExcelHometaxClient(p.read_bytes(), p.name)


def test_excel_html_disguised_as_xls():
    html = ("<html><body><table><tr><td>작성일자</td><td>승인번호</td><td>공급자사업자등록번호</td><td>상호</td>"
            "<td>합계금액</td><td>공급가액</td><td>세액</td><td>품목명</td></tr>"
            "<tr><td>2026-10-03</td><td>20261003-41000012-00000009</td><td>124-81-00010</td><td>한결로지스(주)</td>"
            "<td>110,000</td><td>100,000</td><td>10,000</td><td>택배 운송료</td></tr></table></body></html>")
    c = ExcelHometaxClient(html.encode("utf-8"), "목록.xls")
    inv = c.get_invoice("202610034100001200000009")
    assert (inv.supplier_bizno, inv.total_amount, inv.tax_amount) == ("1248100010", 110_000, 10_000)
