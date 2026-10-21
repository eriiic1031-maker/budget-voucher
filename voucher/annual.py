"""연간 항목: 매년 같은 시기에 처리해야 하는 비용 (재산세 7·9월, 자동차세 6·12월, 보험 갱신 등).

세금·공과금처럼 세금계산서가 오지 않는 지출이 많아 메일·홈택스로는 잡히지 않는다.
→ 달마다 '처리 완료'를 체크하고 금액을 적으면 예산 실적에 들어가고,
  아직 체크 안 한 항목은 예상 금액으로 월말 예상에 들어간다.
"""
from __future__ import annotations

import calendar
import sqlite3
from dataclasses import dataclass
from datetime import date

from .db import now


def months_of(item) -> list[int]:
    return [int(x) for x in str(item["months"]).replace(" ", "").split(",") if x]


@dataclass
class AnnualDue:
    item_id: int
    name: str
    category: str | None
    gl_account: str | None
    due_date: str
    expected_amount: int | None
    status: str                 # DONE / PENDING / OVERDUE
    amount: int | None = None
    paid_date: str | None = None
    note: str | None = None
    checked_by: str | None = None
    item_note: str | None = None

    @property
    def label(self) -> str:
        return {"DONE": "처리 완료", "PENDING": "처리 예정", "OVERDUE": "기한 지남"}[self.status]


def due_items(conn: sqlite3.Connection, month: str, today: date | None = None) -> list[AnnualDue]:
    """이 달에 처리해야 하는 연간 항목."""
    today = today or date.today()
    y, m = map(int, month.split("-"))
    last = calendar.monthrange(y, m)[1]
    out = []
    for it in conn.execute("SELECT * FROM annual_items WHERE active = 1 ORDER BY due_day, name"):
        if m not in months_of(it):
            continue
        due = date(y, m, min(it["due_day"], last))
        chk = conn.execute("SELECT * FROM annual_checks WHERE item_id=? AND month=?", (it["id"], month)).fetchone()
        status = "DONE" if chk else ("OVERDUE" if today > due else "PENDING")
        out.append(AnnualDue(
            it["id"], it["name"], it["category"], it["gl_account"], due.isoformat(), it["expected_amount"], status,
            chk["amount"] if chk else None, chk["paid_date"] if chk else None, chk["note"] if chk else None,
            chk["checked_by"] if chk else None, it["note"],
        ))
    return out


def mark_done(conn: sqlite3.Connection, item_id: int, month: str, amount: int, paid_date: str,
              note: str, actor: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO annual_checks(item_id, month, amount, paid_date, note, checked_by, checked_at)"
        " VALUES (?,?,?,?,?,?,?)", (item_id, month, amount, paid_date, note or None, actor, now()))
    conn.execute("INSERT INTO audit_log(invoice_id, action, detail, actor, at) VALUES (NULL,'ANNUAL_DONE',?,?,?)",
                 (f"{item_id}:{month}:{amount}", actor, now()))


def unmark(conn: sqlite3.Connection, item_id: int, month: str) -> None:
    conn.execute("DELETE FROM annual_checks WHERE item_id=? AND month=?", (item_id, month))


def paid_rows(conn: sqlite3.Connection, start: str, end: str) -> list[sqlite3.Row]:
    """기간 안에 처리(체크)된 연간 항목 지출 — 예산 실적에 들어갈 행."""
    return conn.execute(
        """SELECT c.item_id, c.month, c.amount, COALESCE(c.paid_date, c.month || '-15') AS paid_date,
                  i.name, i.gl_account, i.category
           FROM annual_checks c JOIN annual_items i ON i.id = c.item_id
           WHERE COALESCE(c.paid_date, c.month || '-15') BETWEEN ? AND ? AND c.amount IS NOT NULL""",
        (start, end)).fetchall()
