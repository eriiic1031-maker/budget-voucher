"""관리자 페이지용 조회 함수 (pandas DataFrame 반환)."""
from __future__ import annotations

import json
from datetime import date

import pandas as pd

from .db import connect

STATUS_LABEL = {
    "NEW": "신규", "REVIEW": "검토 대기", "SUBMITTED": "승인 요청", "READY": "전기 대기", "POSTED": "전기 완료",
    "ERROR": "오류", "IGNORED": "제외",
}

ACTION_LABEL = {
    "INGEST": "세금계산서 수신", "CLASSIFY": "자동 분류", "POST": "SAP 전기", "POST_ERROR": "전기 오류",
    "SUBMIT": "승인 요청", "IGNORE": "전기 제외", "RECURRING_MARK": "정기 전표 확인", "HOMETAX_SYNC": "홈택스 수집",
    "PARSE_ERROR": "XML 오류", "CHECK": "비정기 전표 확인", "REJECT": "반려",
}


def month_options(n: int = 12) -> list[str]:
    today = date.today()
    out, y, m = [], today.year, today.month
    for _ in range(n):
        out.append(f"{y}-{m:02d}")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return out


def prev_month(month: str, k: int = 1) -> str:
    y, m = map(int, month.split("-"))
    for _ in range(k):
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return f"{y}-{m:02d}"


def invoices(month: str | None = None, statuses: list[str] | None = None) -> pd.DataFrame:
    sql = """SELECT i.id, i.issue_date, i.status, i.supplier_name, i.supplier_bizno, i.type_code,
                    i.supply_amount, i.tax_amount, i.total_amount, i.approval_no, i.status_reason, i.source,
                    i.submitted_by, p.sap_doc_no, p.posted_at, p.posted_by
             FROM invoices i LEFT JOIN postings p ON p.invoice_id = i.id WHERE 1=1"""
    params: list = []
    if month:
        sql += " AND substr(i.issue_date, 1, 7) = ?"
        params.append(month)
    if statuses:
        sql += f" AND i.status IN ({','.join('?' * len(statuses))})"
        params += statuses
    sql += " ORDER BY i.issue_date DESC, i.id DESC"
    with connect() as conn:
        df = pd.read_sql_query(sql, conn, params=params)
    df["상태"] = df["status"].map(STATUS_LABEL)
    return df


def expense_lines(months: list[str], owner: str | None = "총무") -> pd.DataFrame:
    """전기된 전표의 비용 라인 (부서·계정·사용자별 집계용). 부가세대급금은 비용이 아니므로 제외.
    owner='총무' 이면 총무 예산 계정만 (운반비·광고비 등 다른 부서 예산은 제외)."""
    q = f"""SELECT i.id, i.issue_date, substr(i.issue_date,1,7) AS month, i.supplier_name, i.proposal_json,
                   p.sap_doc_no
            FROM invoices i JOIN postings p ON p.invoice_id = i.id
            WHERE substr(i.issue_date,1,7) IN ({','.join('?' * len(months))})"""
    with connect() as conn:
        inv = pd.read_sql_query(q, conn, params=months)
        cc = dict(conn.execute("SELECT code, name FROM cost_centers").fetchall())
        gl = dict(conn.execute("SELECT code, name FROM gl_accounts" + (" WHERE owner = ?" if owner else ""),
                               (owner,) if owner else ()).fetchall())
        users = dict(conn.execute("SELECT user_id, name FROM users").fetchall())
        users.update(dict(conn.execute("SELECT user_id, user_name FROM vehicles WHERE user_id IS NOT NULL")))
    rows = []
    for r in inv.itertuples():
        for l in json.loads(r.proposal_json or "{}").get("lines", []):
            if owner and l.get("gl_account") not in gl:
                continue
            rows.append({
                "invoice_id": r.id, "발행일": r.issue_date, "월": r.month, "공급자": r.supplier_name,
                "SAP 전표번호": r.sap_doc_no, "품목": l.get("item_name"),
                "계정코드": l.get("gl_account"), "계정": gl.get(l.get("gl_account"), l.get("gl_account")),
                "부서코드": l.get("cost_center"), "부서": cc.get(l.get("cost_center"), l.get("cost_center")),
                "사용자ID": l.get("user_id"), "사용자": users.get(l.get("user_id"), l.get("user_id")),
                "금액": l.get("amount", 0),
            })
    return pd.DataFrame(rows, columns=["invoice_id", "발행일", "월", "공급자", "SAP 전표번호", "품목", "계정코드", "계정",
                                       "부서코드", "부서", "사용자ID", "사용자", "금액"])


def recent_activity(limit: int = 8) -> pd.DataFrame:
    with connect() as conn:
        df = pd.read_sql_query(
            """SELECT a.at, a.action, a.actor, COALESCE(u.name, a.actor) AS who, i.supplier_name, i.total_amount
               FROM audit_log a LEFT JOIN invoices i ON i.id = a.invoice_id
               LEFT JOIN users u ON u.user_id = a.actor
               WHERE a.action IN ('POST','SUBMIT','RECURRING_MARK','HOMETAX_SYNC','IGNORE','POST_ERROR','REJECT')
               ORDER BY a.id DESC LIMIT ?""", conn, params=[limit])
    df["who"] = df["who"].replace({"auto": "자동 전기", "system": "시스템", "batch": "배치"})
    df["활동"] = df["action"].map(ACTION_LABEL).fillna(df["action"])
    return df


def table(name: str) -> pd.DataFrame:
    with connect() as conn:
        return pd.read_sql_query(f"SELECT * FROM {name}", conn)  # name 은 코드 상수만 넘긴다


def audit_log(invoice_id: int) -> pd.DataFrame:
    with connect() as conn:
        return pd.read_sql_query(
            "SELECT at, action, actor, detail FROM audit_log WHERE invoice_id = ? ORDER BY id DESC",
            conn, params=[invoice_id])


def name_maps() -> tuple[dict, dict]:
    with connect() as conn:
        return (dict(conn.execute("SELECT code, name FROM cost_centers").fetchall()),
                dict(conn.execute("SELECT code, name FROM gl_accounts").fetchall()))
