"""수집 → 중복제거 → 분류 → (자동)전기 흐름."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .classifier import Proposal, classify, validate
from .config import settings
from .db import audit, now
from .parser import InvoiceItem, InvoiceParseError, TaxInvoice, parse_xml_bytes
from .sap import SapPostingError, build_bapi_payload, get_client


@dataclass
class IngestResult:
    invoice_id: int | None
    status: str           # NEW_REVIEW, NEW_READY, POSTED, DUPLICATE, PARSE_ERROR
    message: str = ""


def row_to_invoice(row: sqlite3.Row) -> TaxInvoice:
    items = [InvoiceItem(**i) for i in json.loads(row["items_json"])]
    return TaxInvoice(
        approval_no=row["approval_no"], issue_date=row["issue_date"], type_code=row["type_code"],
        supplier_bizno=row["supplier_bizno"], supplier_name=row["supplier_name"],
        buyer_bizno=row["buyer_bizno"], buyer_name="",
        supply_amount=row["supply_amount"], tax_amount=row["tax_amount"], total_amount=row["total_amount"],
        remark=row["remark"] or "", items=items,
    )


def ingest_xml(conn: sqlite3.Connection, data: bytes, source: str, raw_path: str | None = None) -> IngestResult:
    try:
        inv = parse_xml_bytes(data)
    except InvoiceParseError as e:
        audit(conn, None, "PARSE_ERROR", {"source": source, "path": raw_path, "error": str(e)})
        return IngestResult(None, "PARSE_ERROR", str(e))
    return ingest_invoice(conn, inv, source, raw_path)


def ingest_invoice(conn: sqlite3.Connection, inv: TaxInvoice, source: str,
                   raw_path: str | None = None) -> IngestResult:
    """파싱된 세금계산서를 등록하고 분류한다 (XML 외 경로: 홈택스 엑셀 등)."""
    dup = conn.execute("SELECT id FROM invoices WHERE approval_no = ?", (inv.approval_no,)).fetchone()
    if dup:
        return IngestResult(dup["id"], "DUPLICATE", f"이미 등록된 승인번호 {inv.approval_no}")

    ts = now()
    cur = conn.execute(
        """INSERT INTO invoices(approval_no, issue_date, type_code, supplier_bizno, supplier_name, buyer_bizno,
               supply_amount, tax_amount, total_amount, items_json, remark, source, raw_path, status,
               created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'NEW', ?, ?)""",
        (inv.approval_no, inv.issue_date, inv.type_code, inv.supplier_bizno, inv.supplier_name, inv.buyer_bizno,
         inv.supply_amount, inv.tax_amount, inv.total_amount,
         json.dumps([i.__dict__ for i in inv.items], ensure_ascii=False),
         inv.remark, source, raw_path, ts, ts),
    )
    invoice_id = cur.lastrowid
    audit(conn, invoice_id, "INGEST", {"source": source, "approval_no": inv.approval_no})
    return process(conn, invoice_id)


def process(conn: sqlite3.Connection, invoice_id: int) -> IngestResult:
    """분류를 (재)실행한다. 규칙을 추가한 뒤 검토 대기 건에 다시 돌릴 수도 있다."""
    row = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    inv = row_to_invoice(row)
    proposal = classify(conn, inv)
    status = "READY" if proposal.auto_post else "REVIEW"
    conn.execute(
        "UPDATE invoices SET status=?, status_reason=?, proposal_json=?, updated_at=? WHERE id=?",
        (status, "\n".join(proposal.issues) or None, json.dumps(proposal.to_dict(), ensure_ascii=False),
         now(), invoice_id),
    )
    audit(conn, invoice_id, "CLASSIFY", {"status": status, "issues": proposal.issues})

    if status == "READY" and settings.auto_post:
        ok, msg = post_invoice(conn, invoice_id, actor="auto")
        return IngestResult(invoice_id, "POSTED" if ok else "ERROR", msg)
    return IngestResult(invoice_id, f"NEW_{status}", "\n".join(proposal.issues))


def submit_invoice(conn: sqlite3.Connection, invoice_id: int, proposal: Proposal, actor: str,
                   note: str = "") -> tuple[bool, str]:
    """사용자가 분개를 수정해 관리자에게 승인 요청. 검증을 통과해야 요청할 수 있다."""
    row = conn.execute("SELECT status, total_amount FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if row["status"] in ("POSTED", "IGNORED"):
        return False, "이미 처리된 건입니다"
    errs = validate(row["total_amount"], proposal, require_vendor=False)
    if errs:
        return False, "\n".join(errs)
    conn.execute(
        "UPDATE invoices SET status='SUBMITTED', status_reason=?, proposal_json=?, submitted_by=?, updated_at=?"
        " WHERE id=?",
        (f"승인 요청: {note}" if note else "승인 요청", json.dumps(proposal.to_dict(), ensure_ascii=False),
         actor, now(), invoice_id),
    )
    audit(conn, invoice_id, "SUBMIT", {"note": note}, actor)
    return True, "관리자에게 승인 요청했습니다"


def post_invoice(conn: sqlite3.Connection, invoice_id: int, actor: str,
                 proposal: Proposal | None = None) -> tuple[bool, str]:
    row = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if row["status"] == "POSTED":
        return False, "이미 전기된 건입니다"
    inv = row_to_invoice(row)
    proposal = proposal or Proposal.from_dict(json.loads(row["proposal_json"]))

    errs = validate(inv.total_amount, proposal)
    if errs:
        return False, "\n".join(errs)

    try:
        payload = build_bapi_payload(inv, proposal)
        result = get_client(conn).post(payload)
    except (SapPostingError, KeyError) as e:
        conn.execute("UPDATE invoices SET status='ERROR', status_reason=?, updated_at=? WHERE id=?",
                     (str(e), now(), invoice_id))
        audit(conn, invoice_id, "POST_ERROR", str(e), actor)
        return False, str(e)

    conn.execute(
        "INSERT INTO postings(invoice_id, sap_doc_no, fiscal_year, company_code, payload_json, posted_by, posted_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (invoice_id, result.doc_no, result.fiscal_year, result.company_code,
         json.dumps(payload, ensure_ascii=False), actor, now()),
    )
    conn.execute(
        "UPDATE invoices SET status='POSTED', status_reason=NULL, proposal_json=?, updated_at=? WHERE id=?",
        (json.dumps(proposal.to_dict(), ensure_ascii=False), now(), invoice_id),
    )
    audit(conn, invoice_id, "POST", {"doc_no": result.doc_no, "messages": result.messages}, actor)
    return True, f"SAP 전표 {result.doc_no} ({result.fiscal_year}) 생성"
