"""화면 표시용 작은 도우미 (금액·시간을 사람이 읽기 쉬운 말로)."""
from __future__ import annotations

from datetime import datetime


def won(n: float | int | None) -> str:
    return "-" if n is None else f"{int(n):,}원"


def short_won(n: float | int | None) -> str:
    """1억 2,300만원 / 2,315만원 / 8,800원"""
    if n is None:
        return "-"
    n = int(n)
    sign, a = ("-" if n < 0 else ""), abs(n)
    if a >= 100_000_000:
        eok, man = divmod(a, 100_000_000)
        man //= 10_000
        return f"{sign}{eok}억" + (f" {man:,}만원" if man else "원")
    if a >= 10_000:
        return f"{sign}{a // 10_000:,}만원"
    return f"{sign}{a:,}원"


def pct_change(cur: float, prev: float) -> float | None:
    return None if not prev else round((cur - prev) / abs(prev) * 100, 1)


def time_ago(iso: str | None, now: datetime | None = None) -> str:
    if not iso:
        return "-"
    t = datetime.fromisoformat(iso)
    s = int(((now or datetime.now()) - t).total_seconds())
    if s < 60:
        return "방금"
    if s < 3600:
        return f"{s // 60}분 전"
    if s < 86400:
        return f"{s // 3600}시간 전"
    if s < 86400 * 7:
        return f"{s // 86400}일 전"
    return t.strftime("%m/%d")


def greeting(now: datetime | None = None) -> str:
    h = (now or datetime.now()).hour
    return "좋은 아침이에요" if h < 12 else "좋은 오후예요" if h < 18 else "오늘도 수고 많으셨어요"
