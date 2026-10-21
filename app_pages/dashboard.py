from datetime import date

import streamlit as st

from voucher.budget import forecast_month, invoice_overview
from voucher.db import connect
from voucher.hometax import last_sync
from voucher.queries import expense_lines, invoices, prev_month, recent_activity
from voucher.recurring import check_month
from voucher.ui import greeting, pct_change, short_won, time_ago

user = st.session_state.user
month = st.session_state.month
months6 = [prev_month(month, k) for k in range(5, -1, -1)]   # 오래된 → 최근

# ---------- 인사 ----------
with st.container(horizontal=True, vertical_alignment="bottom"):
    st.title(f"{greeting()}, {user.name}님 👋", anchor=False)
    st.space("stretch")
    st.caption(f"{date.today():%Y년 %m월 %d일} · {month} 기준")

# ---------- 데이터 ----------
df = invoices(month)
all_inv = invoices()
lines = expense_lines(months6)
with connect() as conn:
    rec = check_month(conn, month)
    ls = last_sync(conn)
    if user.is_admin:
        bud = forecast_month(conn, month)
        ov = invoice_overview(conn, month, owner=None)
        nonrec_open = int((~ov.recurring.astype(bool) & ov.checked_by.isna()).sum())
pending_all = all_inv[all_inv.status.isin(["REVIEW", "SUBMITTED", "READY", "ERROR"])]

if not user.is_admin:   # 사용자는 우리 부서 기준
    lines = lines[lines["부서코드"] == user.cost_center]


def task_card(icon: str, title: str, count: int, desc: str, page: str, label: str, color: str):
    with st.container(border=True, width=230, height="stretch"):
        st.markdown(f":{color}[{icon}] **{title}**")
        st.markdown(f"### :{color}[{count}건]" if count else "### 0건")
        st.caption(desc)
        st.page_link(page, label=label, icon=":material/arrow_forward:", disabled=count == 0)


# ---------- 오늘 할 일 ----------
st.subheader("오늘 할 일", divider="gray", anchor=False)
if user.is_admin:
    rec_bad = [r for r in rec if r.status in ("OVERDUE", "AMOUNT_DIFF", "NOT_POSTED")]
    tasks = [
        (":material/how_to_reg:", "승인 요청", int((pending_all.status == "SUBMITTED").sum()),
         "사용자가 분개를 고쳐 승인을 기다리는 건", "app_pages/review.py", "승인하러 가기", "violet"),
        (":material/edit_note:", "검토 대기", int(pending_all.status.isin(["REVIEW", "ERROR"]).sum()),
         "계정·거래처를 확정해야 하는 건", "app_pages/review.py", "검토하러 가기", "orange"),
        (":material/publish:", "전기 대기", int((pending_all.status == "READY").sum()),
         "자동 분류가 끝나 전기만 하면 되는 건", "app_pages/review.py", "일괄 전기", "blue"),
        (":material/warning:", "예산 경고", sum(b.status in ("OVER", "RISK") for b in bud),
         "예산 초과 또는 초과가 예상되는 항목", "app_pages/budget.py", "이유 보기", "red"),
        (":material/event_busy:", "정기 전표 확인", len(rec_bad), "기한 경과·금액 상이·미전기",
         "app_pages/recurring.py", "점검하러 가기", "red"),
        (":material/checklist:", "비정기 미확인", nonrec_open, "금액·용도·부서를 눈으로 확인할 건",
         "app_pages/recurring.py", "확인하러 가기", "blue"),
    ]
else:
    tasks = [
        (":material/edit_note:", "검토 필요", int(pending_all.status.isin(["REVIEW", "ERROR"]).sum()),
         "계정·부서를 몰라 사람 확인이 필요한 건", "app_pages/review.py", "확인하러 가기", "orange"),
        (":material/hourglass_top:", "내 승인 요청", int((pending_all.submitted_by == user.user_id).sum()),
         "관리자 승인을 기다리는 중", "app_pages/review.py", "진행 상황 보기", "violet"),
    ]

if sum(t[2] for t in tasks) == 0:
    st.success("지금은 처리할 일이 없어요. 새 세금계산서가 오면 여기에 알려드릴게요. 🎉")
with st.container(horizontal=True):
    for t in tasks:
        task_card(*t)

# ---------- 핵심 지표 ----------
# ---------- 예산 경고 (관리자) ----------
if user.is_admin:
    alerts = [b for b in bud if b.status in ("OVER", "RISK")]
    st.subheader("총무 예산", divider="gray", anchor=False)
    if not alerts:
        st.success("이번 달 예산을 넘거나 넘을 것으로 보이는 항목이 없어요.")
    with st.container(horizontal=True):
        for b in alerts[:4]:
            with st.container(border=True, width=300, height="stretch"):
                st.markdown(f"**{b.icon} {b.name}** · :{'red' if b.status == 'OVER' else 'orange'}[{b.label}]")
                st.progress(min(b.forecast / b.budget / 1.5, 1.0) if b.budget else 1.0,
                            text=f"월말 예상 {short_won(b.forecast)} / 예산 {short_won(b.budget)}")
                for r in b.reasons[:2]:
                    st.caption(r)
    if alerts:
        st.page_link("app_pages/budget.py", label=f"예산 경고 {len(alerts)}건 전체 이유 보기",
                     icon=":material/arrow_forward:")

st.subheader("총무 예산 항목 · 이번 달" if user.is_admin else f"{user.cost_center_name} · 총무 예산 항목 이번 달",
             divider="gray", anchor=False,
             help="임차료·관리비·수도광열비·통신비·렌탈·차량·복리후생·용역·소모품·수선비만 집계합니다. "
                  "운반비·광고비·교육비·IT 수수료 등 다른 부서 예산은 제외됩니다.")
by_month = lines.groupby("월")["금액"].sum().reindex(months6, fill_value=0)

# 진행 중인 달은 전월 '같은 기간(1일~오늘)'과 비교해야 공정하다
cutoff = date.today().day if month == date.today().strftime("%Y-%m") else 31
vs = "전월 같은 기간 대비" if cutoff < 31 else "전월 대비"


def until_cutoff(frame, date_col, m):
    return frame[(frame[date_col].str[:7] == m) & (frame[date_col].str[8:10].astype(int) <= cutoff)]


cur = int(by_month.iloc[-1])
prev = int(until_cutoff(lines, "발행일", months6[-2])["금액"].sum())

with st.container(horizontal=True):
    st.metric("비용 합계 (부가세 제외)", short_won(cur), pct_change(cur, prev), delta_color="inverse",
              delta_description=f"{vs} (%)", border=True, chart_data=by_month.tolist(), chart_type="bar",
              help="SAP에 전기된 비용 라인 기준. 막대는 최근 6개월")
    if user.is_admin:
        recent = all_inv[all_inv.issue_date.str[:7].isin(months6)].assign(m=lambda d: d.issue_date.str[:7])
        cnt6 = recent.groupby("m").size().reindex(months6, fill_value=0)
        prev_cnt = len(until_cutoff(all_inv, "issue_date", months6[-2]))
        st.metric("수신 세금계산서", f"{len(df)}건", len(df) - prev_cnt,
                  delta_description=f"{vs} (건)", border=True, chart_data=cnt6.tolist(), chart_type="bar")
        posted = recent[recent.status == "POSTED"]
        auto6 = (posted.assign(a=posted.posted_by.eq("auto")).groupby("m")["a"].mean().mul(100).round()
                 .reindex(months6, fill_value=0))
        st.metric("자동 전기율", f"{auto6.iloc[-1]:.0f}%", float(auto6.iloc[-1] - auto6.iloc[-2]),
                  delta_description="전월 대비 (%p)", border=True, chart_data=auto6.tolist(), chart_type="line",
                  help="사람이 고치지 않고 자동 분류 그대로 전기된 비율. 규칙이 쌓일수록 올라갑니다.")
    else:
        mine = lines[lines["사용자ID"] == user.user_id].groupby("월")["금액"].sum().reindex(months6, fill_value=0)
        mine_prev = until_cutoff(lines[lines["사용자ID"] == user.user_id], "발행일", months6[-2])["금액"].sum()
        st.metric("내 앞으로 귀속된 비용", short_won(mine.iloc[-1]), pct_change(mine.iloc[-1], mine_prev),
                  delta_color="inverse", delta_description=f"{vs} (%)", border=True,
                  chart_data=mine.tolist(), chart_type="bar", help="배정된 차량 등 사용자 지정 비용")
        cnt6 = lines.groupby("월")["invoice_id"].nunique().reindex(months6, fill_value=0)
        cnt_prev = until_cutoff(lines, "발행일", months6[-2])["invoice_id"].nunique()
        st.metric("부서 비용 건수", f"{cnt6.iloc[-1]}건", int(cnt6.iloc[-1] - cnt_prev),
                  delta_description=f"{vs} (건)", border=True, chart_data=cnt6.tolist(), chart_type="bar")

    ok = sum(r.ok for r in rec)
    with st.container(border=True, width=260, height="stretch"):
        st.markdown("**정기 전표 진행률**")
        st.markdown(f"### {ok} / {len(rec)}")
        st.progress(ok / len(rec) if rec else 0.0, text="완료 또는 확인됨")

# ---------- 차트 ----------
left, right = st.columns([3, 2])
with left, st.container(border=True, height="stretch"):
    st.markdown("**월별 비용 추이** · 계정별")
    if lines.empty:
        st.caption("표시할 비용이 없습니다.")
    else:
        trend = lines.groupby(["월", "계정"], as_index=False)["금액"].sum()
        st.bar_chart(trend, x="월", y="금액", color="계정", height=300, alt="최근 6개월 계정별 비용 추이")

with right, st.container(border=True, height="stretch"):
    this = lines[lines["월"] == month]
    key = "부서" if user.is_admin else "계정"
    st.markdown(f"**{key}별 비용** · {month}")
    if this.empty:
        st.caption("이번 달 전기된 비용이 아직 없습니다.")
    else:
        top = this.groupby(key, as_index=False)["금액"].sum().sort_values("금액", ascending=False)
        st.bar_chart(top, x=key, y="금액", horizontal=True, height=300, alt=f"이번 달 {key}별 비용")

# ---------- 정기 전표 + 최근 활동 ----------
left, right = st.columns(2)
ICON = {"DONE": "✅", "CONFIRMED": "☑️", "SKIPPED": "➖", "WAITING": "⏳",
        "NOT_POSTED": "🟡", "AMOUNT_DIFF": "🟠", "OVERDUE": "🔴"}
with left, st.container(border=True, height="stretch"):
    st.markdown("**정기 전표** · 매달 꼭 들어와야 하는 전표")
    for r in sorted(rec, key=lambda r: (r.ok, r.due_date)):
        with st.container(horizontal=True):
            st.markdown(f"{ICON[r.status]} {r.name}")
            st.space("stretch")
            st.caption(f"{r.label} · 기한 {r.due_date[5:]}")
    st.page_link("app_pages/recurring.py", label="정기 전표 점검", icon=":material/arrow_forward:")

with right, st.container(border=True, height="stretch"):
    st.markdown("**최근 활동**")
    act = recent_activity(7)
    if act.empty:
        st.caption("아직 활동이 없습니다.")
    for a in act.itertuples():
        with st.container(horizontal=True):
            what = f"· {a.supplier_name} {short_won(a.total_amount)}" if a.supplier_name else ""
            st.markdown(f"**{a.who}** {a.활동} {what}")
            st.space("stretch")
            st.caption(time_ago(a.at))
    if user.is_admin and ls:
        st.caption(f"홈택스 마지막 대사: {time_ago(ls['at'])}")
