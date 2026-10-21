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
    start_month     TEXT,                         -- YYYY-MM, 이 달부터 점검 (신규 계약)
    active          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS annual_items (           -- 매년 같은 시기에 처리할 항목 (재산세, 자동차세, 보험 갱신 등)
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    months          TEXT NOT NULL,                -- 처리 월, 쉼표 구분 (예: '7' / '6,12')
    due_day         INTEGER NOT NULL DEFAULT 31,  -- 납부·처리 기한일
    gl_account      TEXT,
    expected_amount INTEGER,                      -- 회당 예상 금액(원)
    category        TEXT,                         -- 세금 / 보험 / 점검 등
    note            TEXT,
    active          INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS annual_checks (
    item_id         INTEGER NOT NULL REFERENCES annual_items(id),
    month           TEXT NOT NULL,                -- YYYY-MM
    amount          INTEGER,
    paid_date       TEXT,
    note            TEXT,
    checked_by      TEXT,
    checked_at      TEXT NOT NULL,
    PRIMARY KEY (item_id, month)
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

CREATE TABLE IF NOT EXISTS attachments (            -- 증빙자료 (전표·연간 항목에 붙인 파일)
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type     TEXT NOT NULL,                -- invoice / annual
    target_key      TEXT NOT NULL,                -- invoice id / '연간항목id:YYYY-MM'
    filename        TEXT NOT NULL,
    content_type    TEXT,
    size            INTEGER NOT NULL,
    sha256          TEXT NOT NULL,
    stored_path     TEXT NOT NULL,
    note            TEXT,
    uploaded_by     TEXT,
    uploaded_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_attach_target ON attachments(target_type, target_key);

CREATE TABLE IF NOT EXISTS ai_log (                -- AI 도우미 기록 (추천 vs 최종 → 정확도, 브리핑 보관)
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    feature         TEXT NOT NULL,                -- account / briefing / qa / docread / anomaly
    invoice_id      INTEGER,
    month           TEXT,
    source          TEXT NOT NULL,                -- ai(로컬 AI) / history·rule(대체 방식)
    input_json      TEXT,
    output_json     TEXT,
    final_json      TEXT,
    accepted        INTEGER,                      -- 추천이 그대로 전기됐는가 (1/0)
    created_at      TEXT NOT NULL,
    decided_at      TEXT
);

CREATE TABLE IF NOT EXISTS app_meta (
    key             TEXT PRIMARY KEY,
    value           TEXT
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
            ("recurring_vouchers", "gl_account", "TEXT"), ("recurring_vouchers", "start_month", "TEXT"), ("gl_accounts", "owner", "TEXT NOT NULL DEFAULT '총무'"),
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


def reset_db(path: Path | None = None) -> None:
    """모든 표의 데이터를 비운다 (파일을 지우지 않아 실행 중인 앱이 있어도 안전)."""
    init_db(path)
    with connect(path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in tables:
            conn.execute(f"DELETE FROM {t}")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
            conn.execute("DELETE FROM sqlite_sequence")


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO app_meta(key, value) VALUES (?,?)", (key, value))


def audit(conn: sqlite3.Connection, invoice_id: int | None, action: str, detail: dict | str | None = None,
          actor: str = "system") -> None:
    if isinstance(detail, dict):
        detail = json.dumps(detail, ensure_ascii=False)
    conn.execute(
        "INSERT INTO audit_log(invoice_id, action, detail, actor, at) VALUES (?,?,?,?,?)",
        (invoice_id, action, detail, actor, now()),
    )
