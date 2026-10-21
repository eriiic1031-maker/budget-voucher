"""증빙자료 첨부: 형식 제한, 중복 방지, 미리보기, 삭제."""
import io
from pathlib import Path

import pytest
from PIL import Image

from voucher import attachments as att
from voucher import db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(att, "root", lambda: tmp_path / "attachments")
    db.init_db(tmp_path / "t.db")
    with db.connect(tmp_path / "t.db") as c:
        c.execute("INSERT INTO invoices(approval_no, issue_date, supplier_bizno, supply_amount, tax_amount,"
                  " total_amount, items_json, created_at, updated_at) VALUES ('A1','2026-10-16','1',1,0,1,'[]','x','x')")
        yield c


def _png() -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (40, 30), "red").save(b, format="PNG")
    return b.getvalue()


def test_save_list_dedupe_delete(conn):
    png = _png()
    a1 = att.save(conn, "invoice", 1, "견적서.png", png, "admin", "3사 비교")
    a2 = att.save(conn, "invoice", 1, "견적서(복사).png", png, "admin")      # 같은 내용 → 한 번만
    assert a1 == a2
    att.save(conn, "invoice", 1, "계약서.pdf", b"%PDF-1.4 dummy", "admin")
    files = att.list_for(conn, "invoice", 1)
    assert [f.filename for f in files] == ["견적서.png", "계약서.pdf"]
    assert files[0].is_image and files[0].note == "3사 비교" and Path(files[0].stored_path).exists()
    assert att.preview_png(files[0]) == png
    assert att.counts(conn, "invoice") == {"1": 2}
    actions = [r[0] for r in conn.execute("SELECT action FROM audit_log WHERE invoice_id=1")]
    assert actions.count("ATTACH") == 2

    att.delete(conn, files[0].id, "admin")
    assert not Path(files[0].stored_path).exists()
    assert [f.filename for f in att.list_for(conn, "invoice", 1)] == ["계약서.pdf"]


def test_rejects_bad_type_and_size(conn, monkeypatch):
    with pytest.raises(att.AttachmentError):
        att.save(conn, "invoice", 1, "virus.exe", b"MZ", "admin")
    monkeypatch.setattr(att, "MAX_BYTES", 10)
    with pytest.raises(att.AttachmentError):
        att.save(conn, "invoice", 1, "big.pdf", b"x" * 11, "admin")


def test_annual_target_and_pdf_preview(conn):
    pdf = (Path(__file__).parent.parent / "docs" / "sample_전기안전점검_청구서.pdf").read_bytes()
    att.save(conn, "annual", "9:2026-10", "청구서.pdf", pdf, "admin")
    f = att.list_for(conn, "annual", "9:2026-10")[0]
    png = att.preview_png(f)
    assert png and png[:4] == b"\x89PNG"                                    # PDF 첫 쪽 → 이미지
    assert att.counts(conn, "annual") == {"9:2026-10": 1}
