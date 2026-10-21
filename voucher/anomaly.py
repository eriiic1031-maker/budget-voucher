"""AI 도우미 ④ 비정기 전표 이상 신호.

찾는 것은 규칙으로(같은 데이터면 늘 같은 결과), 설명은 문장으로 붙인다.
  - 중복 의심   : 같은 공급자·같은 품목·같은 금액(50만원 이상)이 30일 안에 또 들어옴
  - 분할 의심   : 같은 공급자·같은 품목을 3일 안에 3건 이상 비슷한 금액(또는 2건 같은 금액)으로, 각각 300만원 미만인데 합치면 300만원 이상
  - 금액 급증   : 그 공급자의 지난 12개월 평균보다 3배 이상 (100만원 이상일 때)
  - 신규 고액   : 최근 3개월 안에 처음 거래한 공급자의 300만원 이상 건
정기 전표(임차료·관리비 등)는 금액이 원래 크므로 제외한다.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from .budget import month_bounds, shift_month
from .recurring import recurring_map
from .ui import short_won

BIG = 3_000_000


@dataclass
class Anomaly:
    invoice_id: int
    date: str
    supplier: str
    item: str
    amount: int
    kind: str
    explain: str


def detect(conn: sqlite3.Connection, month: str) -> list[Anomaly]:
    start, end, _ = month_bounds(month)
    look_from = (date.fromisoformat(start) - timedelta(days=400)).isoformat()
    rows = conn.execute(
        "SELECT id, issue_date, supplier_bizno, supplier_name, items_json, total_amount, approval_no FROM invoices "
        "WHERE issue_date BETWEEN ? AND ? AND status != 'IGNORED' ORDER BY issue_date", (look_from, end)).fetchall()
    rec = recurring_map(conn, look_from, end)
    rows = [r for r in rows if r["id"] not in rec and r["total_amount"] > 0]
    import json
    item = {r["id"]: " / ".join(i["name"] for i in json.loads(r["items_json"]))[:40] for r in rows}
    by_sup: dict[str, list] = {}
    for r in rows:
        by_sup.setdefault(r["supplier_bizno"], []).append(r)
    first_seen = dict(conn.execute("SELECT supplier_bizno, MIN(issue_date) FROM invoices GROUP BY supplier_bizno"))

    found: dict[tuple, Anomaly] = {}

    def add(r, kind, explain):
        found.setdefault((r["id"], kind), Anomaly(r["id"], r["issue_date"], r["supplier_name"], item[r["id"]],
                                                  r["total_amount"], kind, explain))

    def same_item(x, y):
        return item[x["id"]].split(" (")[0] == item[y["id"]].split(" (")[0]

    for sup, lst in by_sup.items():
        cur = [r for r in lst if start <= r["issue_date"] <= end]
        past = [r for r in lst if r["issue_date"] < start]
        split_ids: set[int] = set()
        for r in cur:
            d = date.fromisoformat(r["issue_date"])
            # 분할 의심: 같은 품목·비슷한 금액을 3일 안에 나눠 받음
            group = [o for o in lst if same_item(o, r) and abs((date.fromisoformat(o["issue_date"]) - d).days) <= 3]
            amts = [o["total_amount"] for o in group]
            # 3건 이상 비슷한 금액, 또는 2건 똑같은 금액 (우연히 겹친 2건은 빼기 위해)
            looks_split = (len(group) >= 3 and max(amts) <= min(amts) * 1.5) or (len(group) == 2 and len(set(amts)) == 1)
            if looks_split and max(amts) < BIG and sum(amts) >= BIG:
                split_ids |= {o["id"] for o in group}
                add(r, "분할 의심", f"같은 품목이 3일 안에 {len(group)}건, 합계 {short_won(sum(amts))} — "
                                  f"건당 300만원 미만으로 나눠 결재 기준을 피했는지 확인하세요.")
        for r in cur:
            d = date.fromisoformat(r["issue_date"])
            # 중복 의심: 같은 품목·같은 금액이 30일 안에 또 (분할로 잡힌 건은 제외)
            if r["id"] not in split_ids and r["total_amount"] >= 500_000:
                for o in sorted(lst, key=lambda o: o["issue_date"], reverse=True):   # 가장 가까운 이전 건
                    if o["id"] != r["id"] and o["id"] not in split_ids and same_item(o, r) and \
                            o["total_amount"] == r["total_amount"] and \
                            0 < (d - date.fromisoformat(o["issue_date"])).days <= 30:
                        add(r, "중복 의심", f"{o['issue_date'][5:].replace('-', '/')}에도 같은 품목·같은 금액"
                                        f"({short_won(r['total_amount'])}) 세금계산서가 있습니다. 같은 건을 두 번 받았는지 확인하세요.")
                        break
            # 금액 급증
            hist = [o["total_amount"] for o in past if o["issue_date"] >= shift_month(month, -12) + "-01"]
            if len(hist) >= 3:
                avg = sum(hist) / len(hist)
                if r["total_amount"] >= max(3 * avg, 1_000_000):
                    add(r, "금액 급증", f"이 공급자의 지난 12개월 평균 {short_won(avg)}보다 "
                                      f"{r['total_amount'] / avg:.1f}배 큽니다. 견적·품의를 확인하세요.")
            # 신규 고액
            if first_seen.get(sup, "") >= shift_month(month, -2) + "-01" and r["total_amount"] >= BIG:
                add(r, "신규 고액", "최근 3개월 안에 처음 거래한 공급자의 300만원 이상 건입니다. 거래처 등록·계약서를 확인하세요.")
    return sorted(found.values(), key=lambda a: (a.date, a.kind), reverse=True)
