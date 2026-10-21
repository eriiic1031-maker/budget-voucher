from datetime import date

import pytest

from voucher import db
from voucher.hometax import HometaxRecord, reconcile, sync
from voucher.parser import parse_xml_bytes
from voucher.pipeline import ingest_xml
from voucher.seed import build_xml, load_master


class FakeClient:
    name = "fake"

    def __init__(self, xmls):
        self.xmls = {parse_xml_bytes(x).approval_no: x for x in xmls}

    def list_purchases(self, start, end):
        out = []
        for no, x in self.xmls.items():
            i = parse_xml_bytes(x)
            if start.isoformat() <= i.issue_date <= end.isoformat():
                out.append(HometaxRecord(no, i.issue_date, i.issue_date, i.supplier_bizno, i.supplier_name,
                                         i.supply_amount, i.tax_amount, i.total_amount, i.is_modified))
        return out

    def get_xml(self, no):
        return self.xmls[no]


@pytest.fixture
def conn(tmp_path):
    db.init_db(tmp_path / "t.db")
    with db.connect(tmp_path / "t.db") as c:
        load_master(c)
        yield c


def _x(n, d, sup, amt):
    return build_xml(f"{d:%Y%m%d}41000012{n:08d}", d, sup, [("품목", "", amt)])


def test_sync_ingests_missing_and_reconciles(conn, tmp_path):
    mailed = _x(1, date(2026, 10, 5), ("1048100002", "누리텔레콤(주)"), 500_000)
    not_mailed = _x(2, date(2026, 10, 1), ("2208100003", "다온빌딩관리(주)"), 10_000_000)
    system_only = _x(3, date(2026, 10, 7), ("1048100002", "누리텔레콤(주)"), 10_000)
    ingest_xml(conn, mailed, "mail")
    ingest_xml(conn, system_only, "mail")

    client = FakeClient([mailed, not_mailed])
    assert sorted(reconcile(conn, "2026-10").recon) == ["SYSTEM_ONLY", "SYSTEM_ONLY"]  # 수집 전

    r = sync(conn, date(2026, 10, 1), date(2026, 10, 31), client=client, save_dir=tmp_path)
    assert (r.fetched, r.new_in_hometax, r.ingested, r.errors) == (2, 1, 1, [])
    assert conn.execute("SELECT source FROM invoices WHERE supplier_bizno='2208100003'").fetchone()[0] == "hometax"

    after = reconcile(conn, "2026-10")
    assert sorted(after.recon) == ["MATCH", "MATCH", "SYSTEM_ONLY"]

    # 두 번 돌려도 중복 등록 없음
    r2 = sync(conn, date(2026, 10, 1), date(2026, 10, 31), client=client, save_dir=tmp_path)
    assert r2.ingested == 0


def test_amount_diff_detected(conn):
    xml = _x(4, date(2026, 10, 5), ("1048100002", "누리텔레콤(주)"), 500_000)
    ingest_xml(conn, xml, "mail")
    conn.execute("UPDATE invoices SET total_amount = 1")   # 시스템 쪽 금액이 틀어진 경우
    sync(conn, date(2026, 10, 1), date(2026, 10, 31), client=FakeClient([xml]), auto_ingest=False)
    assert list(reconcile(conn, "2026-10").recon) == ["AMOUNT_DIFF"]


def test_popbill_client_flow_with_sdk_objects():
    """실서버 없이 팝빌 SDK의 응답 객체(JsonObject)로 요청→대기→조회→XML 흐름을 검증."""
    import json
    from popbill.base import Utils
    from voucher.hometax import PopbillHometaxClient

    def obj(d):
        return Utils.json2obj(json.dumps(d))

    calls = []

    class Svc:
        def requestJob(self, corp, typ, dtype, s, e, user):
            calls.append(("requestJob", typ, dtype, s, e))
            return "JOB1"

        def getJobState(self, corp, job, user):
            calls.append(("getJobState",))
            return obj({"jobState": 3 if len(calls) > 2 else 2, "errorCode": 1, "errorReason": ""})

        def search(self, corp, job, *a):
            page = a[6]
            calls.append(("search", page))
            rows = [{"ntsconfirmNum": f"2026100141000012000000{page}{i}", "writeDate": "20261001",
                     "issueDate": "20261001", "invoicerCorpNum": "220-81-00003", "invoicerCorpName": "다온",
                     "supplyCostTotal": "10000000", "taxTotal": "1000000", "totalAmount": "11000000",
                     "modifyYN": False, "itemName": "임차료"} for i in range(2)]
            return obj({"code": 1, "pageCount": 2, "list": rows})

        def getXML(self, corp, no, user):
            return obj({"ResultCode": 1, "retObject": "<TaxInvoice/>"})

    c = object.__new__(PopbillHometaxClient)
    c.svc, c.corp, c.user, c.poll, c.timeout, c._exc = Svc(), "1018199999", None, 0, 5, Exception
    recs = c.list_purchases(date(2026, 10, 1), date(2026, 10, 31))
    assert calls[0] == ("requestJob", "BUY", "W", "20261001", "20261031")
    assert len(recs) == 4 and [x[1] for x in calls if x[0] == "search"] == [1, 2]
    r = recs[0]
    assert (r.supplier_bizno, r.total_amount, r.write_date) == ("2208100003", 11_000_000, "2026-10-01")
    assert c.get_xml(r.approval_no) == b"<TaxInvoice/>"
