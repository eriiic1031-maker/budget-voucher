"""대시보드 달력에 올릴 일정 모으기.

- 정기 전표 기한 (이번 달 상태 아이콘 포함)
- 연간 항목 (재산세·자동차세·보험 갱신·법정 점검 등)
- 작년 이 시기 지출 (작년 같은 달의 큰 비정기 지출 + 연간 항목) → "올해도 나올 수 있는 비용" 미리 알림
- 세무·마감 일정 (부가세 신고 마감, 전월분 홈택스 대사, 월말 결산 마감)
- 공휴일 (예시: 2025~2026년. 실제 운영 시 정부 발표 기준으로 확인)
"""
from __future__ import annotations

import calendar
import sqlite3
from datetime import date, timedelta

from .annual import due_items
from .budget import load_lines, month_bounds, shift_month
from .recurring import check_month
from .ui import short_won

HOLIDAYS = {
    "2025-01-01": "신정", "2025-01-28": "설날 연휴", "2025-01-29": "설날", "2025-01-30": "설날 연휴",
    "2025-03-01": "삼일절", "2025-03-03": "대체공휴일", "2025-05-05": "어린이날·부처님오신날", "2025-05-06": "대체공휴일",
    "2025-06-03": "대통령 선거", "2025-06-06": "현충일", "2025-08-15": "광복절", "2025-10-03": "개천절",
    "2025-10-05": "추석 연휴", "2025-10-06": "추석", "2025-10-07": "추석 연휴", "2025-10-08": "대체공휴일",
    "2025-10-09": "한글날", "2025-12-25": "성탄절",
    "2026-01-01": "신정", "2026-02-16": "설날 연휴", "2026-02-17": "설날", "2026-02-18": "설날 연휴",
    "2026-03-01": "삼일절", "2026-03-02": "대체공휴일", "2026-05-05": "어린이날", "2026-05-24": "부처님오신날",
    "2026-05-25": "대체공휴일", "2026-06-03": "지방선거", "2026-06-06": "현충일", "2026-08-15": "광복절",
    "2026-08-17": "대체공휴일", "2026-09-24": "추석 연휴", "2026-09-25": "추석", "2026-09-26": "추석 연휴",
    "2026-10-03": "개천절", "2026-10-05": "대체공휴일", "2026-10-09": "한글날", "2026-12-25": "성탄절",
}
REC_ICON = {"DONE": "✅", "CONFIRMED": "☑️", "SKIPPED": "➖", "WAITING": "⏳", "NOT_POSTED": "🟡",
            "AMOUNT_DIFF": "🟠", "OVERDUE": "🔴"}
ANN_ICON = {"DONE": "✅", "PENDING": "⏳", "OVERDUE": "🔴"}


def _business_day(d: date, backward: bool = False) -> date:
    """주말·공휴일이면 다음(또는 이전) 영업일로."""
    step = timedelta(days=-1 if backward else 1)
    while d.weekday() >= 5 or d.isoformat() in HOLIDAYS:
        d += step
    return d


def month_events(conn: sqlite3.Connection, month: str, today: date | None = None) -> dict[int, list[dict]]:
    today = today or date.today()
    y, m = map(int, month.split("-"))
    last = calendar.monthrange(y, m)[1]
    days: dict[int, list[dict]] = {}

    def add(day: int, type_: str, short: str, label: str, detail: str = ""):
        days.setdefault(day, []).append({"type": type_, "short": short, "label": label, "detail": detail})

    # 공휴일
    for iso, name in HOLIDAYS.items():
        if iso.startswith(month):
            add(int(iso[8:]), "holiday", name, name)

    # 세무·마감
    if m in (1, 4, 7, 10):
        d = _business_day(date(y, m, 25))
        kind = "확정" if m in (1, 7) else "예정"
        add(d.day, "tax", "부가세 신고 마감", f"부가세 {kind}신고·납부 마감",
            "매입 세금계산서 수취·홈택스 대사를 이 날 전에 끝내기")
    d = _business_day(date(y, m, 10))
    add(d.day, "tax", "홈택스 대사", "전월분 홈택스 매입 대사 권장일", "홈택스 대사 화면에 엑셀 올리기")
    d = _business_day(date(y, m, last), backward=True)
    add(d.day, "tax", "월말 결산 마감", "월말 결산 마감", "전기 대기·검토 대기 건 처리, 비정기 전표 확인")

    # 연간 항목 (올해)
    for a in due_items(conn, month, today):
        amt = short_won(a.amount if a.amount else a.expected_amount)
        add(int(a.due_date[8:]), "annual", f"{ANN_ICON[a.status]} {a.name}", f"{a.name} · {a.label}",
            f"{'처리 금액' if a.amount else '예상'} {amt}" + (f" · {a.item_note}" if a.item_note else ""))

    # 정기 전표 기한 (올해)
    for r in check_month(conn, month, today):
        add(int(r.due_date[8:]), "rec", f"{REC_ICON[r.status]} {r.name}", f"{r.name} 기한 · {r.label}",
            f"예상 {short_won(r.expected_amount)}" + (f" / 실제 {short_won(r.actual_amount)}" if r.actual_amount else ""))

    # 작년 이 시기 지출: 큰 비정기 지출(300만원↑)과 연간 항목
    ly_month = shift_month(month, -12)
    s, e, _ = month_bounds(ly_month)
    ly = load_lines(conn, s, e, include_pending=False)
    if not ly.empty:
        one_off = (ly[~ly.recurring & ~ly.annual].groupby(["invoice_id", "date", "supplier", "item"], as_index=False)
                   .amount.sum())
        for r in one_off[one_off.amount >= 3_000_000].sort_values("amount", ascending=False).itertuples():
            day = min(int(r.date[8:]), last)
            add(day, "ly", f"작년 {r.item}", f"작년 {r.date[5:].replace('-', '/')} {r.supplier} '{r.item}'",
                f"{short_won(r.amount)} · 올해도 필요한지 확인")
        for r in ly[ly.annual].itertuples():
            add(min(int(r.date[8:]), last), "ly", f"작년 {r.item}", f"작년 {r.date[5:].replace('-', '/')} {r.item}",
                f"{short_won(r.amount)}")
    for k in days:   # 같은 날은 공휴일 → 세무 → 연간 → 정기 → 작년 순
        order = {"holiday": 0, "tax": 1, "annual": 2, "rec": 3, "ly": 4}
        days[k].sort(key=lambda ev: order[ev["type"]])
    return days


def next_month_annual(conn: sqlite3.Connection, month: str) -> list:
    """다음 달 처리할 연간 항목 (미리 알림)."""
    nxt = shift_month(month, 1)
    return due_items(conn, nxt, date.fromisoformat(f"{nxt}-01"))
