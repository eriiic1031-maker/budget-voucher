"""AI 도우미: 로컬 AI가 있을 때(가짜 응답)와 없을 때(규칙 대체) 모두 같은 화면 흐름이 되는지."""
import json
from datetime import date

import pytest

from voucher import ai, db
from voucher.anomaly import detect
from voucher.assist import accuracy, last_suggestion, similar_cases
from voucher.docread import _from_text, match_annual
from voucher.insight import ask, briefing, route_rule
from voucher.pipeline import ingest_xml, post_invoice
from voucher.seed import build_xml, load_master

_n = iter(range(1, 10**6))


@pytest.fixture
def conn(tmp_path):
    ai.disable(True)
    db.init_db(tmp_path / "t.db")
    with db.connect(tmp_path / "t.db") as c:
        load_master(c)
        yield c
    ai.disable(False)


def _ingest(conn, d, sup, item, amt):
    return ingest_xml(conn, build_xml(f"{d:%Y%m%d}41000012{next(_n):08d}", d, sup, [(item, "", amt)]), "t")


def _post_as(conn, invoice_id, gl, cc):
    from voucher.classifier import Proposal
    p = Proposal.from_dict(json.loads(conn.execute("SELECT proposal_json FROM invoices WHERE id=?",
                                                   (invoice_id,)).fetchone()[0]))
    p.sap_vendor = "300001"
    for l in p.lines:
        l.gl_account, l.cost_center = gl, cc
    ok, msg = post_invoice(conn, invoice_id, "admin", p)
    assert ok, msg


def test_history_suggestion_fills_review_and_tracks_accuracy(conn):
    flower = ("1308100302", "하늘꽃집")
    r1 = _ingest(conn, date(2026, 2, 11), flower, "경조 화환", 300_000)
    assert last_suggestion(conn, r1.invoice_id)["gl_account"] is None          # 첫 거래: 추천 없음
    _post_as(conn, r1.invoice_id, "81100000", "K1100")

    r2 = _ingest(conn, date(2026, 4, 8), flower, "조문 화환", 350_000)
    s = last_suggestion(conn, r2.invoice_id)
    assert (s["gl_account"], s["cost_center"], s["source"]) == ("81100000", "K1100", "history")
    lines = json.loads(conn.execute("SELECT proposal_json FROM invoices WHERE id=?", (r2.invoice_id,)).fetchone()[0])
    assert lines["lines"][0]["gl_account"] == "81100000"                        # 분개 표에 미리 채움
    assert r2.status == "NEW_REVIEW"                                            # 확정은 사람이
    _post_as(conn, r2.invoice_id, "81100000", "K1100")
    r3 = _ingest(conn, date(2026, 6, 16), flower, "경조 화환", 300_000)
    _post_as(conn, r3.invoice_id, "81100000", "K2100")                          # 부서를 바꿔 전기
    acc = accuracy(conn)
    assert (acc["total"], acc["accepted"]) == (2, 1)


def test_ai_suggestion_used_when_available(conn, monkeypatch):
    monkeypatch.setattr(ai, "status", lambda refresh=False: ai.Status(True, "fake", ""))
    calls = {}

    def fake_chat(messages, schema=None, **kw):
        calls["prompt"] = messages[-1]["content"]
        return {"gl_account": "83000000", "cost_center": "K1200", "non_deductible": False,
                "reason": "사무용 달력 제작은 소모품비", "confidence": 0.82}
    monkeypatch.setattr(ai, "chat", fake_chat)
    r = _ingest(conn, date(2026, 10, 18), ("2118100098", "새봄문구"), "탁상 달력 제작", 1_850_000)
    s = last_suggestion(conn, r.invoice_id)
    assert (s["source"], s["gl_account"], s["confidence"]) == ("ai", "83000000", 0.82)
    assert "탁상 달력 제작" in calls["prompt"] and "83000000 소모품비" in calls["prompt"]


def test_ai_failure_falls_back(conn, monkeypatch):
    monkeypatch.setattr(ai, "status", lambda refresh=False: ai.Status(True, "fake", ""))

    def boom(*a, **k):
        raise ai.AIUnavailable("AI 메모리 부족")
    monkeypatch.setattr(ai, "chat", boom)
    text, src = briefing(conn, "2026-10", date(2026, 10, 21))
    assert src == "rule" and "총무 예산 브리핑" in text
    a = ask(conn, "이번 달 연간 항목 뭐 있어?", "2026-10", date(2026, 10, 21))
    assert a.source == "rule" and a.tool == "annual_status" and "전기 정기 안전점검" in a.text


def test_question_routing_rules():
    accts = ["차량유지비", "복리후생비", "지급임차료"]
    assert route_rule("이번 달 차량유지비 왜 초과했어?", "2026-10", accts) == \
        ("account_detail", {"month": "2026-10", "account": "차량유지비"})
    assert route_rule("올해 누적으로 늘어난 거", "2026-10", accts) == ("yoy", {"month": "2026-10", "scope": "ytd"})
    assert route_rule("7월 재산세 처리했어?", "2026-10", accts) == ("annual_status", {"month": "2026-07"})
    assert route_rule("지난달 예산 어땠어", "2026-10", accts) == ("budget_overview", {"month": "2026-09"})


def test_anomaly_split_and_duplicate(conn):
    printer = ("1018100092", "바른인쇄(주)")
    toner = ("2118100099", "온누리문구")
    for d in (13, 14, 15):
        _ingest(conn, date(2026, 10, d), printer, "홍보 브로슈어 인쇄", 1_000_000)
    _ingest(conn, date(2026, 10, 6), toner, "토너·잉크", 1_800_000)
    _ingest(conn, date(2026, 10, 14), toner, "토너·잉크", 1_800_000)
    _ingest(conn, date(2026, 10, 2), ("3128100006", "새솔주유소"), "휘발유 12가3456", 1_600_000)
    _ingest(conn, date(2026, 10, 4), ("3128100006", "새솔주유소"), "휘발유 12가3456", 1_700_000)   # 우연히 겹친 2건
    kinds = {(a.kind, a.supplier) for a in detect(conn, "2026-10")}
    assert ("분할 의심", "바른인쇄(주)") in kinds and ("중복 의심", "온누리문구") in kinds
    assert not any(s == "새솔주유소" for _, s in kinds)


def test_docread_rule_and_match(conn):
    r = _from_text("청 구 서\n한빛전기안전(주)\n전기설비 정기 안전점검\n청구 금액 1,650,000원\n지급 기한 2026.10.31")
    assert (r.amount, r.due_date, r.issuer) == (1_650_000, "2026-10-31", "한빛전기안전(주)")
    name = conn.execute("SELECT name FROM annual_items WHERE id=?", (match_annual(conn, r.item_name),)).fetchone()[0]
    assert name == "전기 정기 안전점검"
    r = _from_text("2026년 7월분 재산세(건물분) 고지서\n납부할 세액 19,610,000원\n납부기한 2026. 7. 31.")
    assert (r.amount, r.due_date) == (19_610_000, "2026-07-31")
    assert "건물" in conn.execute("SELECT name FROM annual_items WHERE id=?",
                                  (match_annual(conn, "재산세(건물분)"),)).fetchone()[0]


def test_similar_cases_ignores_numbers_and_plates(conn):
    _post = _ingest(conn, date(2026, 1, 5), ("1308100301", "모아철물"), "공구·철물 소모품", 400_000)
    _post_as(conn, _post.invoice_id, "83000000", "K1200")
    cases = similar_cases(conn, "9999999999", "철물 소모품 2026")
    assert cases and cases[0]["gl_account"] == "83000000"
