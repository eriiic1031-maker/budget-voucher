"""매월 발행되어야 하는 정기 전표(리스료, 통신비, 임차료 등)의 발행 여부 점검."""
from __future__ import annotations

import calendar
import json
import sqlite3
from dataclasses import dataclass
from datetime import date

from .db import now

STATUS_LABEL = {
    "DONE": "전기 완료",
    "AMOUNT_DIFF": "금액 상이",
    "NOT_POSTED": "수신·미전기",
    "WAITING": "수신 대기",
    "OVERDUE": "기한 경과",
    "CONFIRMED": "수동 확인",
    "SKIPPED": "이번 달 해당 없음",
}


@dataclass
class RecurringStatus:
    recurring_id: int
    name: str
    supplier_bizno: str
    owner: str | None
    due_date: str
    expected_amount: int | None
    actual_amount: int | None
    sap_doc_no: str | None
    invoice_id: int | None
    status: str
    note: str | None = None

    @property
    def label(self) -> str:
        return STATUS_LABEL[self.status]

    @property
    def ok(self) -> bool:
        return self.status in ("DONE", "CONFIRMED", "SKIPPED")


def _month_bounds(month: str) -> tuple[str, str, int]:
    y, m = map(int, month.split("-"))
    last = calendar.monthrange(y, m)[1]
    return f"{month}-01", f"{month}-{last:02d}", last


def check_month(conn: sqlite3.Connection, month: str, today: date | None = None) -> list[RecurringStatus]:
    today = today or date.today()
    start, end, last_day = _month_bounds(month)
    result = []

    for rv in conn.execute("SELECT * FROM recurring_vouchers WHERE active = 1 AND "
                           "(start_month IS NULL OR start_month <= ?) ORDER BY due_day, name", (month,)):
        due = date.fromisoformat(f"{month}-{min(rv['due_day'], last_day):02d}")
        manual = conn.execute("SELECT * FROM recurring_checks WHERE recurring_id=? AND month=?",
                              (rv["id"], month)).fetchone()

        invs = conn.execute(
            """SELECT i.*, p.sap_doc_no FROM invoices i LEFT JOIN postings p ON p.invoice_id = i.id
               WHERE i.supplier_bizno = ? AND i.issue_date BETWEEN ? AND ? AND i.status != 'IGNORED'
               ORDER BY i.issue_date""",
            (rv["supplier_bizno"], start, end),
        ).fetchall()
        if rv["keyword"]:
            invs = [i for i in invs
                    if rv["keyword"] in (i["items_json"] + (i["remark"] or ""))]

        posted = [i for i in invs if i["sap_doc_no"]]
        pick = posted[0] if posted else (invs[0] if invs else None)
        actual = sum(i["total_amount"] for i in posted) if posted else (pick["total_amount"] if pick else None)

        if manual:
            status = manual["status"]
        elif posted:
            exp = rv["expected_amount"]
            if exp and abs(actual - exp) > abs(exp) * rv["tolerance_pct"] / 100:
                status = "AMOUNT_DIFF"
            else:
                status = "DONE"
        elif invs:
            status = "NOT_POSTED"
        else:
            status = "OVERDUE" if today > due else "WAITING"

        result.append(RecurringStatus(
            recurring_id=rv["id"], name=rv["name"], supplier_bizno=rv["supplier_bizno"], owner=rv["owner"],
            due_date=due.isoformat(), expected_amount=rv["expected_amount"], actual_amount=actual,
            sap_doc_no=posted[0]["sap_doc_no"] if posted else None,
            invoice_id=pick["id"] if pick else None, status=status,
            note=manual["note"] if manual else None,
        ))
    return result


def mark(conn: sqlite3.Connection, recurring_id: int, month: str, status: str, note: str, actor: str) -> None:
    """금액 상이·미발행 건을 담당자가 확인했거나, 이번 달은 발생하지 않는 건으로 표시."""
    assert status in ("CONFIRMED", "SKIPPED")
    conn.execute(
        "INSERT OR REPLACE INTO recurring_checks(recurring_id, month, status, note, checked_by, checked_at)"
        " VALUES (?,?,?,?,?,?)",
        (recurring_id, month, status, note, actor, now()),
    )
    conn.execute("INSERT INTO audit_log(invoice_id, action, detail, actor, at) VALUES (NULL,'RECURRING_MARK',?,?,?)",
                 (json.dumps({"recurring_id": recurring_id, "month": month, "status": status, "note": note},
                             ensure_ascii=False), actor, now()))


def unmark(conn: sqlite3.Connection, recurring_id: int, month: str) -> None:
    conn.execute("DELETE FROM recurring_checks WHERE recurring_id=? AND month=?", (recurring_id, month))


# ------------------------------------------------------------- 정기 / 비정기 구분 ----
def recurring_map(conn: sqlite3.Connection, start: str, end: str) -> dict[int, str]:
    """기간 내 세금계산서 id → 정기 전표 이름. 여기에 없는 세금계산서는 '비정기'."""
    rvs = conn.execute("SELECT id, name, supplier_bizno, keyword FROM recurring_vouchers WHERE active=1").fetchall()
    by_supplier: dict[str, list] = {}
    for rv in rvs:
        by_supplier.setdefault(rv["supplier_bizno"], []).append(rv)
    out = {}
    for inv in conn.execute("SELECT id, supplier_bizno, items_json, remark FROM invoices "
                            "WHERE issue_date BETWEEN ? AND ?", (start, end)):
        for rv in by_supplier.get(inv["supplier_bizno"], []):
            if not rv["keyword"] or rv["keyword"] in (inv["items_json"] + (inv["remark"] or "")):
                out[inv["id"]] = rv["name"]
                break
    return out


def mark_checked(conn: sqlite3.Connection, invoice_ids: list[int], actor: str, note: str = "") -> None:
    """비정기 전표를 담당자가 '내용 확인함'으로 표시 (금액·용도·부서가 맞는지 눈으로 확인)."""
    ts = now()
    conn.executemany("UPDATE invoices SET checked_by=?, checked_at=?, check_note=? WHERE id=?",
                     [(actor, ts, note or None, i) for i in invoice_ids])
    conn.executemany("INSERT INTO audit_log(invoice_id, action, detail, actor, at) VALUES (?,'CHECK',?,?,?)",
                     [(i, note or None, actor, ts) for i in invoice_ids])


def unmark_checked(conn: sqlite3.Connection, invoice_ids: list[int]) -> None:
    conn.executemany("UPDATE invoices SET checked_by=NULL, checked_at=NULL, check_note=NULL WHERE id=?",
                     [(i,) for i in invoice_ids])
