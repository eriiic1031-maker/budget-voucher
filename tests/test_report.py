"""월간 총무 예산 보고서: 숫자 집계, 핵심 보고 사항, PDF 생성."""
from datetime import date

import pytest

import dataclasses
import importlib

from voucher import ai, db, report, seed
from voucher.config import settings


@pytest.fixture
def conn(tmp_path, monkeypatch):
    tmp = dataclasses.replace(settings, db_path=tmp_path / "t.db")   # 예시 데이터 전체를 임시 DB에
    for mod in ("db", "attachments", "seed", "pipeline", "classifier", "sap", "hometax", "mail"):
        m = importlib.import_module(f"voucher.{mod}")
        if hasattr(m, "settings"):
            monkeypatch.setattr(m, "settings", tmp)
    seed.run()
    ai.disable(False)
    with db.connect() as c:
        yield c


def test_collect_and_pdf(conn, monkeypatch):
    monkeypatch.setattr(ai, "status", lambda refresh=False: ai.Status(False, "x", "꺼짐"))
    d = report.collect(conn, "2026-10", date(2026, 10, 21), use_ai=True)
    assert d.budget > 0 and d.forecast >= d.actual
    assert d.messages[0].startswith("10월 예산") and d.ai_summary is None        # 로컬 AI 없으면 의견 없이
    assert [s.status for s in d.accounts][0] == "OVER"                          # 심각한 계정이 먼저
    pdf = report.render_pdf(d, "총무팀 테스트", "팀장", "의견 <테스트> & 메모", ("담당", "팀장"))
    assert pdf[:5] == b"%PDF-" and len(pdf) > 20_000
    pages = report.page_images(pdf, 0.5)
    assert len(pages) >= 2 and pages[0][:4] == b"\x89PNG"
