"""총무 예산 대비 실적·월말 예상, 초과(예상) 경고와 그 이유.

월말 예상 = 정기 전표(받은 것 + 아직 안 받은 것의 예상액)
          + 큰 비정기 건(일회성, 한 번만 반영)
          + 나머지 비정기 지출의 월말 추정
나머지 비정기 지출의 남은 기간 추정 = (남은 기간 비율) × [경과율×현재 속도 + (1-경과율)×작년 같은 달(올해 증감 추세 반영)]
  → 월초에는 작년 패턴을, 월말로 갈수록 이번 달 실제 속도를 더 믿는다.
"""
from __future__ import annotations

import calendar
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from .recurring import check_month, recurring_map
from .ui import short_won

OWNER = "총무"
PENDING = ("READY", "REVIEW", "SUBMITTED", "ERROR")

STATUS_LABEL = {"OVER": "초과", "RISK": "초과 예상", "WATCH": "주의", "OK": "정상", "NO_BUDGET": "예산 없음"}
STATUS_ICON = {"OVER": "🔴", "RISK": "🟠", "WATCH": "🟡", "OK": "🟢", "NO_BUDGET": "⚪"}
SEVERITY = {"OVER": 0, "RISK": 1, "NO_BUDGET": 2, "WATCH": 3, "OK": 4}


def month_bounds(month: str) -> tuple[str, str, int]:
    y, m = map(int, month.split("-"))
    last = calendar.monthrange(y, m)[1]
    return f"{month}-01", f"{month}-{last:02d}", last


def shift_month(month: str, k: int) -> str:
    y, m = map(int, month.split("-"))
    idx = y * 12 + (m - 1) + k
    return f"{idx // 12}-{idx % 12 + 1:02d}"


def owner_accounts(conn: sqlite3.Connection, owner: str = OWNER) -> dict[str, str]:
    return dict(conn.execute("SELECT code, name FROM gl_accounts WHERE owner = ? ORDER BY code", (owner,)))


def load_lines(conn: sqlite3.Connection, start: str, end: str, include_pending: bool = True,
               owner: str | None = OWNER) -> pd.DataFrame:
    """비용 라인 (전기 완료 + 선택적으로 전기 전 건). 부가세대급금은 비용이 아니므로 없다."""
    statuses = ("POSTED",) + (PENDING if include_pending else ())
    rows = conn.execute(
        f"""SELECT i.id, i.issue_date, i.supplier_name, i.status, i.proposal_json, i.checked_by
            FROM invoices i WHERE i.issue_date BETWEEN ? AND ? AND i.status IN ({','.join('?' * len(statuses))})""",
        (start, end, *statuses)).fetchall()
    rec = recurring_map(conn, start, end)
    accounts = owner_accounts(conn, owner) if owner else dict(conn.execute("SELECT code, name FROM gl_accounts"))
    cc = dict(conn.execute("SELECT code, name FROM cost_centers"))
    out = []
    for r in rows:
        for l in json.loads(r["proposal_json"] or "{}").get("lines", []):
            gl = l.get("gl_account")
            if owner and gl not in accounts:
                continue
            out.append((r["id"], r["issue_date"], r["issue_date"][:7], r["supplier_name"], l.get("item_name"),
                        gl, accounts.get(gl, gl), l.get("cost_center"), cc.get(l.get("cost_center")),
                        l.get("user_id"), int(l.get("amount") or 0), r["status"], r["id"] in rec, rec.get(r["id"]),
                        r["checked_by"]))
    df = pd.DataFrame(out, columns=["invoice_id", "date", "month", "supplier", "item", "gl", "account", "cc",
                                    "dept", "user_id", "amount", "status", "recurring", "recurring_name",
                                    "checked_by"])
    # 빈 결과에서도 bool/int 이어야 df[df.recurring] 가 '열 선택'으로 오해되지 않는다
    return df.astype({"recurring": bool, "amount": "int64"})


def _join(values, sep: str = ", ") -> str:
    """중복 없이 이어 붙이기. 아직 분류 안 된 라인(None/NaN)은 건너뛴다."""
    return sep.join(dict.fromkeys(v for v in values if isinstance(v, str) and v))


def invoice_overview(conn: sqlite3.Connection, month: str, owner: str | None = OWNER) -> pd.DataFrame:
    """월간 세금계산서 1건 = 1행. 정기/비정기, 계정·부서, 신규 거래처, 확인 여부 (전표 점검 화면용)."""
    start, end, _ = month_bounds(month)
    lines = load_lines(conn, start, end, include_pending=True, owner=owner)
    if lines.empty:
        return pd.DataFrame(columns=["invoice_id", "date", "supplier", "item", "account", "dept", "amount",
                                     "status", "recurring", "recurring_name", "checked_by", "checked_at",
                                     "check_note", "new_vendor", "big"])
    g = lines.groupby("invoice_id")
    df = pd.DataFrame({
        "date": g.date.first(), "supplier": g.supplier.first(),
        "item": g.item.agg(lambda s: _join(s, " / ")),
        "account": g.account.agg(lambda s: _join(s) or "미분류"),
        "dept": g.dept.agg(lambda s: _join(s) or "-"),
        "amount": g.amount.sum(), "status": g.status.first(),
        "recurring": g.recurring.first(), "recurring_name": g.recurring_name.first(),
    }).reset_index()
    meta = pd.read_sql_query(
        f"""SELECT i.id AS invoice_id, i.checked_by, i.checked_at, i.check_note, i.supplier_bizno,
                   (SELECT MIN(j.issue_date) FROM invoices j WHERE j.supplier_bizno = i.supplier_bizno) AS first_seen
            FROM invoices i WHERE i.issue_date BETWEEN ? AND ?""", conn, params=[start, end])
    df = df.merge(meta, on="invoice_id", how="left")
    df["new_vendor"] = df.first_seen >= shift_month(month, -2) + "-01"   # 최근 3개월 안에 처음 거래
    df["big"] = df.amount >= 3_000_000
    return df.sort_values("date", ascending=False).reset_index(drop=True)


def budgets(conn: sqlite3.Connection, year: int, month: int | None = None) -> pd.DataFrame:
    sql, params = "SELECT * FROM budgets WHERE year = ?", [year]
    if month:
        sql += " AND month = ?"
        params.append(month)
    return pd.read_sql_query(sql, conn, params=params)


@dataclass
class BudgetStatus:
    gl: str
    name: str
    budget: int
    actual: int              # 전기 완료 + 전기 전(받은 건)
    pending: int             # 그중 전기 전 금액
    forecast: int
    ly_actual: int           # 작년 같은 달 실적
    recurring_actual: int
    recurring_remaining: int
    nonrecurring_actual: int
    status: str
    reasons: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return STATUS_LABEL[self.status]

    @property
    def icon(self) -> str:
        return STATUS_ICON[self.status]

    @property
    def use_rate(self) -> float | None:
        return self.actual / self.budget if self.budget else None

    @property
    def forecast_rate(self) -> float | None:
        return self.forecast / self.budget if self.budget else None


def elapsed_ratio(month: str, today: date) -> float:
    start, end, days = month_bounds(month)
    if today.isoformat() > end:
        return 1.0
    if today.isoformat() < start:
        return 0.0
    return today.day / days


def forecast_month(conn: sqlite3.Connection, month: str, today: date | None = None) -> list[BudgetStatus]:
    today = today or date.today()
    y, m = map(int, month.split("-"))
    f = elapsed_ratio(month, today)
    start, end, days = month_bounds(month)
    ly_month = shift_month(month, -12)

    cur = load_lines(conn, start, end, include_pending=True)
    ly = load_lines(conn, *month_bounds(ly_month)[:2], include_pending=False)
    # 올해 증감 추세: 올해 1월~지난달 비정기 / 작년 같은 기간 비정기 (계정별)
    if m > 1:
        ytd = load_lines(conn, f"{y}-01-01", month_bounds(shift_month(month, -1))[1], include_pending=False)
        ytd_ly = load_lines(conn, f"{y - 1}-01-01", month_bounds(shift_month(ly_month, -1))[1],
                            include_pending=False)
    else:
        ytd = ytd_ly = cur.iloc[0:0]
    bud = budgets(conn, y, m).set_index("gl_account")["amount"].to_dict()
    accounts = owner_accounts(conn)

    rec_status = check_month(conn, month, today)
    rv_gl = dict(conn.execute("SELECT name, gl_account FROM recurring_vouchers"))
    ly_year = load_lines(conn, f"{y - 1}-01-01", f"{y - 1}-12-31", include_pending=False)
    ly_year = ly_year[ly_year.recurring]
    ly_by_rec = ly_year.groupby(["recurring_name", "month"])["amount"].sum()

    def estimate_recurring(name: str, expected_total: int) -> tuple[int, bool]:
        """미수신 정기 전표 비용 추정. 작년 같은 달 금액 × (올해 기준액 / 작년 월평균) → 계절성(전기·가스·명절) 반영."""
        base = round(expected_total / 1.1)        # 합계(부가세 포함) → 공급가액
        if name in ly_by_rec.index.get_level_values(0):
            s = ly_by_rec.loc[name]
            if ly_month in s.index and s.mean() > 0:
                return round(s[ly_month] * base / s.mean()), True
        return base, False

    rec_remaining: dict[str, list[tuple[str, int, str]]] = {}
    rec_diff: dict[str, list[str]] = {}
    for r in rec_status:
        gl = rv_gl.get(r.name)
        if r.status in ("WAITING", "OVERDUE") and r.expected_amount:
            amt, seasonal = estimate_recurring(r.name, r.expected_amount)
            rec_remaining.setdefault(gl, []).append((r.name, amt, r.status + ("_S" if seasonal else "")))
        if r.status == "AMOUNT_DIFF" and r.expected_amount and r.actual_amount:
            rec_diff.setdefault(gl, []).append(
                f"{r.name} 예상 {short_won(r.expected_amount)} → 실제 {short_won(r.actual_amount)}")

    out = []
    for gl, name in accounts.items():
        c = cur[cur.gl == gl]
        budget = int(bud.get(gl, 0))
        actual = int(c.amount.sum())
        pending = int(c[c.status != "POSTED"].amount.sum())
        rec_act = int(c[c.recurring].amount.sum())
        nonrec = c[~c.recurring]
        rem_list = rec_remaining.get(gl, [])
        rec_rem = sum(a for _, a, _ in rem_list)

        # 비정기: 예산의 5%(최소 300만원) 이상인 큰 건은 일회성으로 보고 속도 계산에서 뺀다
        per_inv = nonrec.groupby(["invoice_id", "supplier", "item"], as_index=False)["amount"].sum()
        big = per_inv[per_inv.amount >= max(budget * 0.05, 3_000_000)] if budget else per_inv.iloc[0:0]
        rest_act = int(nonrec.amount.sum() - big.amount.sum())

        ly_c = ly[ly.gl == gl]
        ly_total = int(ly_c.amount.sum())
        ly_nonrec = int(ly_c[~ly_c.recurring].amount.sum())
        t_now = ytd[(ytd.gl == gl) & ~ytd.recurring].amount.sum()
        t_ly = ytd_ly[(ytd_ly.gl == gl) & ~ytd_ly.recurring].amount.sum()
        trend = min(max(t_now / t_ly, 0.7), 1.6) if t_ly > 0 else 1.0
        base = ly_nonrec * trend                         # 작년 같은 달 × 올해 추세

        if f >= 1:
            rest_full = rest_act
            runrate = rest_act
        elif f <= 0:
            rest_full, runrate = base, 0
        else:
            runrate = rest_act / f                        # 지금 속도로 한 달을 채우면
            rest_full = rest_act + (1 - f) * (f * runrate + (1 - f) * base)
        forecast = int(rec_act + rec_rem + big.amount.sum() + rest_full)

        if not budget:
            status = "NO_BUDGET" if actual else "OK"
        elif actual > budget:
            status = "OVER"
        elif forecast > budget * 1.02:
            status = "RISK"
        elif forecast > budget * 0.98:
            status = "WATCH"
        else:
            status = "OK"

        reasons: list[str] = []
        if status != "OK" or rem_list or len(big):
            if status == "OVER":
                reasons.append(f"이미 예산을 **{short_won(actual - budget)}** 넘었습니다 "
                               f"(예산 {short_won(budget)} / 사용 {short_won(actual)}).")
            elif status in ("RISK", "WATCH") and budget:
                reasons.append(f"월말 예상 **{short_won(forecast)}** · 예산의 {forecast / budget:.0%} "
                               f"({'+' if forecast >= budget else ''}{short_won(forecast - budget)}).")
            if (status != "OVER" and budget and 0 < f < 1 and nonrec.amount.sum() > actual * 0.5
                    and actual / budget > f + 0.1):
                reasons.append(f"기간은 {f:.0%} 지났는데 예산은 이미 {actual / budget:.0%}를 썼습니다.")
            for nm, amt, st in rem_list:
                notes = ["기한이 지났습니다 — 곧 들어올 금액" if st.startswith("OVERDUE") else "예정"]
                if st.endswith("_S"):
                    notes.append("작년 같은 달 기준 추정")
                reasons.append(f"아직 안 들어온 정기 전표 **{nm}** {short_won(amt)} ({', '.join(notes)}).")
            for d in rec_diff.get(gl, []):
                reasons.append(f"정기 전표 금액 증가: {d}.")
            for b in big.sort_values("amount", ascending=False).itertuples():
                reasons.append(f"큰 비정기 지출: {b.supplier} '{b.item}' **{short_won(b.amount)}**"
                               + (f" (예산의 {b.amount / budget:.0%})" if budget else "") + ".")
            if 0 < f < 1 and base > 0 and runrate > base * 1.15 and rest_act > 0:
                reasons.append(f"비정기 지출 속도가 작년 같은 달보다 **{runrate / base - 1:.0%} 빠릅니다** "
                               f"(이 속도면 한 달 {short_won(runrate)}, 작년 {short_won(ly_nonrec)}).")
            ly_rec = int(ly_c[ly_c.recurring].amount.sum())
            if ly_rec and rec_act + rec_rem > ly_rec * 1.05:
                names = ", ".join(sorted(set(c[c.recurring].recurring_name.dropna()) | {n for n, _, _ in rem_list}))
                reasons.append(f"정기 전표({names})가 작년 같은 달보다 "
                               f"**{(rec_act + rec_rem) / ly_rec - 1:.0%} 늘었습니다** "
                               f"({short_won(ly_rec)} → {short_won(rec_act + rec_rem)}). 단가·요금 인상 여부를 확인하세요.")
            if trend >= 1.15 and t_ly > 0:
                reasons.append(f"올해 이 계정의 비정기 지출이 작년보다 {trend - 1:.0%} 늘어나는 추세입니다.")
            if budget and ly_total and budget < ly_total * 0.97:
                reasons.append(f"예산이 작년 같은 달 실적({short_won(ly_total)})보다 "
                               f"{1 - budget / ly_total:.0%} 적게 잡혀 있습니다.")
            if pending:
                reasons.append(f"아직 전기 전(검토·대기) 금액 {short_won(pending)} 포함.")
            if status == "NO_BUDGET":
                reasons.append("이 달 예산이 편성되지 않았습니다. 기준정보 > 예산에서 입력하세요.")

        out.append(BudgetStatus(gl, name, budget, actual, pending, forecast, ly_total, rec_act, rec_rem,
                                int(nonrec.amount.sum()), status, reasons))
    return sorted(out, key=lambda s: (SEVERITY[s.status], -(s.forecast_rate or 0)))


def annual_summary(conn: sqlite3.Connection, year: int, until_month: int) -> pd.DataFrame:
    """연간 예산·실적 (1월~until_month 실적, 작년 같은 기간 비교)."""
    accounts = owner_accounts(conn)
    end = month_bounds(f"{year}-{until_month:02d}")[1]
    act = load_lines(conn, f"{year}-01-01", end, include_pending=True)
    ly = load_lines(conn, f"{year - 1}-01-01", end.replace(str(year), str(year - 1), 1), include_pending=False)
    ly_full = load_lines(conn, f"{year - 1}-01-01", f"{year - 1}-12-31", include_pending=False)
    b = budgets(conn, year)
    rows = []
    for gl, name in accounts.items():
        bg = b[b.gl_account == gl]
        rows.append({
            "계정": name, "gl": gl,
            "연간 예산": int(bg.amount.sum()),
            "누적 예산": int(bg[bg.month <= until_month].amount.sum()),
            "누적 실적": int(act[act.gl == gl].amount.sum()),
            "작년 같은 기간": int(ly[ly.gl == gl].amount.sum()),
            "작년 연간": int(ly_full[ly_full.gl == gl].amount.sum()),
        })
    df = pd.DataFrame(rows)
    df["누적 집행률"] = (df["누적 실적"] / df["누적 예산"]).where(df["누적 예산"] > 0)
    df["연간 집행률"] = (df["누적 실적"] / df["연간 예산"]).where(df["연간 예산"] > 0)
    return df
