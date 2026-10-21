"""SQLite 저장소. 운영 시 사내 DB(PostgreSQL 등)로 교체해도 스키마는 그대로 쓸 수 있다."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    approval_no     TEXT NOT NULL UNIQUE,         -- 국세청 승인번호(24자리)
    issue_date      TEXT NOT NULL,                -- YYYY-MM-DD
    type_code       TEXT,                         -- 0101 일반, 0201 수정 ...
    supplier_bizno  TEXT NOT NULL,
    supplier_name   TEXT,
    buyer_bizno     TEXT,
    supply_amount   INTEGER NOT NULL,
    tax_amount      INTEGER NOT NULL,
    total_amount    INTEGER NOT NULL,
    items_json      TEXT NOT NULL,
    remark          TEXT,
    source          TEXT,                         -- mail / upload / hometax
    raw_path        TEXT,
    status          TEXT NOT NULL DEFAULT 'NEW',  -- NEW, REVIEW, SUBMITTED, READY, POSTED, ERROR, IGNORED
    status_reason   TEXT,
    submitted_by    TEXT,                         -- 사용자가 분개를 수정해 승인 요청한 경우
    checked_by      TEXT,                         -- 비정기 전표 담당자 확인
    checked_at      TEXT,
    check_note      TEXT,
    proposal_json   TEXT,                         -- 분류 결과(분개 라인)
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vendors (
    bizno           TEXT PRIMARY KEY,
    sap_vendor      TEXT NOT NULL,                -- SAP 공급업체 코드(LIFNR)
    name            TEXT,
    payment_terms   TEXT
);

CREATE TABLE IF NOT EXISTS account_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    priority        INTEGER NOT NULL DEFAULT 100, -- 작을수록 먼저
    supplier_bizno  TEXT,                         -- NULL이면 모든 공급자
    keyword         TEXT,                         -- 품목명/비고에 포함되면 매칭
    gl_account      TEXT NOT NULL,
    cost_center     TEXT,
    alloc_key       TEXT,                         -- allocation_keys.key (배부)
    non_deductible  INTEGER NOT NULL DEFAULT 0,   -- 부가세 불공제
    auto_post       INTEGER NOT NULL DEFAULT 0,
    memo            TEXT
);

CREATE TABLE IF NOT EXISTS allocation_keys (
    key             TEXT NOT NULL,
    cost_center     TEXT NOT NULL,
    user_id         TEXT,
    ratio           REAL NOT NULL,
    PRIMARY KEY (key, cost_center, user_id)
);

CREATE TABLE IF NOT EXISTS vehicles (
    plate           TEXT PRIMARY KEY,             -- 차량번호(공백 제거)
    cost_center     TEXT NOT NULL,
    user_id         TEXT,
    user_name       TEXT,
    non_deductible  INTEGER NOT NULL DEFAULT 1    -- 비영업용 소형승용차 = 불공제
);

CREATE TABLE IF NOT EXISTS postings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id      INTEGER NOT NULL REFERENCES invoices(id),
    sap_doc_no      TEXT NOT NULL,
    fiscal_year     TEXT NOT NULL,
    company_code    TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    posted_by       TEXT,
    posted_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recurring_vouchers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    supplier_bizno  TEXT NOT NULL,
    keyword         TEXT,                         -- 같은 공급자에 여러 건이면 품목 키워드로 구분
    expected_amount INTEGER,                      -- 합계금액 기준, NULL이면 금액 비교 안 함
    tolerance_pct   REAL NOT NULL DEFAULT 10,
    due_day         INTEGER NOT NULL DEFAULT 10,  -- 매월 이 날까지 전기되어야 함
    owner           TEXT,
    gl_account      TEXT,                         -- 예산 항목 (월말 예상에 미수신분을 반영할 계정)
    active          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS recurring_checks (
    recurring_id    INTEGER NOT NULL REFERENCES recurring_vouchers(id),
    month           TEXT NOT NULL,                -- YYYY-MM
    status          TEXT NOT NULL,                -- CONFIRMED, SKIPPED
    note            TEXT,
    checked_by      TEXT,
    checked_at      TEXT NOT NULL,
    PRIMARY KEY (recurring_id, month)
);

CREATE TABLE IF NOT EXISTS hometax_invoices (       -- 국세청(홈택스) 기준 매입 세금계산서 목록
    approval_no     TEXT PRIMARY KEY,
    write_date      TEXT NOT NULL,                -- 작성일자 YYYY-MM-DD
    issue_date      TEXT,                         -- 발행일자
    supplier_bizno  TEXT NOT NULL,
    supplier_name   TEXT,
    supply_amount   INTEGER NOT NULL,
    tax_amount      INTEGER NOT NULL,
    total_amount    INTEGER NOT NULL,
    modify_yn       INTEGER NOT NULL DEFAULT 0,
    item_name       TEXT,
    fetched_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hometax_sync_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    provider        TEXT NOT NULL,
    start_date      TEXT NOT NULL,
    end_date        TEXT NOT NULL,
    fetched         INTEGER NOT NULL DEFAULT 0,
    ingested        INTEGER NOT NULL DEFAULT 0,
    errors          TEXT,
    actor           TEXT,
    at              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id         TEXT PRIMARY KEY,             -- 로그인 아이디(사번)
    name            TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'user', -- admin(관리자) | user(사용자)
    cost_center     TEXT,                         -- 소속 부서
    password_hash   TEXT NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS cost_centers (
    code            TEXT PRIMARY KEY,
    name            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gl_accounts (
    code            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    owner           TEXT NOT NULL DEFAULT '총무'  -- 예산 주관 부서. '총무'만 총무 예산 화면에 나온다
);

CREATE TABLE IF NOT EXISTS budgets (                -- 월별 예산 (계정 단위)
    year            INTEGER NOT NULL,
    month           INTEGER NOT NULL,
    gl_account      TEXT NOT NULL,
    amount          INTEGER NOT NULL,
    PRIMARY KEY (year, month, gl_account)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id      INTEGER,
    action          TEXT NOT NULL,
    detail          TEXT,
    actor           TEXT,
    at              TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def init_db(path: Path | None = None) -> None:
    path = path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        # 이전 버전 DB 호환: 나중에 추가된 컬럼
        for table, col, ddl in [
            ("invoices", "submitted_by", "TEXT"), ("invoices", "checked_by", "TEXT"),
            ("invoices", "checked_at", "TEXT"), ("invoices", "check_note", "TEXT"),
            ("recurring_vouchers", "gl_account", "TEXT"), ("gl_accounts", "owner", "TEXT NOT NULL DEFAULT '총무'"),
        ]:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path or settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def audit(conn: sqlite3.Connection, invoice_id: int | None, action: str, detail: dict | str | None = None,
          actor: str = "system") -> None:
    if isinstance(detail, dict):
        detail = json.dumps(detail, ensure_ascii=False)
    conn.execute(
        "INSERT INTO audit_log(invoice_id, action, detail, actor, at) VALUES (?,?,?,?,?)",
        (invoice_id, action, detail, actor, now()),
    )
