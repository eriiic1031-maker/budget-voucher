from datetime import date

import streamlit as st

from voucher.annual import due_items
from voucher.anomaly import detect
from voucher.budget import forecast_month, invoice_overview, yoy_increase
from voucher.components import month_calendar, task_cards
from voucher.db import connect
from voucher.hometax import last_sync
from voucher.queries import expense_lines, invoices, prev_month, recent_activity
from voucher.recurring import check_month
from voucher.schedule import month_events, next_month_annual
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
        annual_open = sum(a.status != "DONE" for a in due_items(conn, month))
        anomaly_n = len(detect(conn, month))
pending_all = all_inv[all_inv.status.isin(["REVIEW", "SUBMITTED", "READY", "ERROR"])]

if not user.is_admin:   # 사용자는 우리 부서 기준
    lines = lines[lines["부서코드"] == user.cost_center]


# 카드 id → (이동할 화면, 그 화면에서 미리 골라 둘 구분)
CARD_TARGET = {
    "approve": ("app_pages/review.py", "review_group_pref", "승인 요청"),
    "review": ("app_pages/review.py", "review_group_pref", "검토 대기" if user.is_admin else "검토 필요"),
    "ready": ("app_pages/review.py", "review_group_pref", "전기 대기"),
    "mine": ("app_pages/review.py", "review_group_pref", "내 승인 요청"),
    "budget": ("app_pages/budget.py", None, None),
    "rec": ("app_pages/recurring.py", "check_view", "정기 전표"),
    "nonrec": ("app_pages/recurring.py", "check_view", "비정기 전표"),
    "annual": ("app_pages/recurring.py", "check_view", "연간 항목"),
    "anomaly": ("app_pages/ai_helper.py", "ai_view", "이상 신호"),
}

# ---------- 오늘 할 일 ----------
st.subheader("오늘 할 일", divider="gray", anchor=False)
if user.is_admin:
    rec_bad = [r for r in rec if r.status in ("OVERDUE", "AMOUNT_DIFF", "NOT_POSTED")]
    cards = [
        {"id": "approve", "icon": "🙋", "title": "승인 요청", "tone": "violet", "go": "승인하러 가기",
         "count": int((pending_all.status == "SUBMITTED").sum()), "desc": "사용자가 분개를 고쳐 승인을 기다리는 건"},
        {"id": "review", "icon": "📝", "title": "검토 대기", "tone": "orange", "go": "검토하러 가기",
         "count": int(pending_all.status.isin(["REVIEW", "ERROR"]).sum()), "desc": "계정·거래처를 확정해야 하는 건"},
        {"id": "ready", "icon": "📤", "title": "전기 대기", "tone": "blue", "go": "일괄 전기",
         "count": int((pending_all.status == "READY").sum()), "desc": "자동 분류가 끝나 전기만 하면 되는 건"},
        {"id": "budget", "icon": "⚠️", "title": "예산 경고", "tone": "red", "go": "이유 보기",
         "count": sum(b.status in ("OVER", "RISK") for b in bud), "desc": "예산 초과 또는 초과가 예상되는 항목"},
        {"id": "rec", "icon": "🔁", "title": "정기 전표 확인", "tone": "red", "go": "점검하러 가기",
         "count": len(rec_bad), "desc": "기한 경과·금액 상이·미전기"},
        {"id": "nonrec", "icon": "🔎", "title": "비정기 미확인", "tone": "blue", "go": "확인하러 가기",
         "count": nonrec_open, "desc": "금액·용도·부서를 눈으로 확인할 건"},
        {"id": "annual", "icon": "🗓️", "title": "연간 항목", "tone": "violet", "go": "체크하러 가기",
         "count": annual_open, "desc": "이번 달 처리할 세금·보험·점검 (재산세 등)"},
        {"id": "anomaly", "icon": "🚨", "title": "이상 신호", "tone": "orange", "go": "확인하러 가기",
         "count": anomaly_n, "desc": "중복·분할 의심, 금액 급증, 신규 고액 거래"},
    ]
else:
    cards = [
        {"id": "review", "icon": "📝", "title": "검토 필요", "tone": "orange", "go": "확인하러 가기",
         "count": int(pending_all.status.isin(["REVIEW", "ERROR"]).sum()), "desc": "계정·부서를 몰라 사람 확인이 필요한 건"},
        {"id": "mine", "icon": "⏳", "title": "내 승인 요청", "tone": "violet", "go": "진행 상황 보기",
         "count": int((pending_all.submitted_by == user.user_id).sum()), "desc": "관리자 승인을 기다리는 중"},
    ]

if sum(c["count"] for c in cards) == 0:
    st.success("지금은 처리할 일이 없어요. 새 세금계산서가 오면 여기에 알려드릴게요. 🎉")
clicked = task_cards(cards, key="task_cards")
if clicked in CARD_TARGET:
    page, state_key, value = CARD_TARGET[clicked]
    if state_key:
        st.session_state[state_key] = value
    st.switch_page(page)

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

# ---------- 작년보다 늘어난 비용 · 달력 (관리자) ----------
if user.is_admin:
    st.subheader("작년보다 늘어난 비용", divider="gray", anchor=False,
                 help="총무 예산 계정만 · 작년 같은 기간(같은 날짜까지)과 비교 · 원인은 증가분이 큰 정기 전표/공급자/연간 항목")
    scope = st.segmented_control("비교 기간", ["이번 달", "올해 누적"], default="이번 달", required=True,
                                 key="yoy_scope", label_visibility="collapsed")
    with connect() as conn:
        yoy, tot_now, tot_ly = yoy_increase(conn, month, scope="month" if scope == "이번 달" else "ytd")
    period = "이번 달 1일~오늘" if scope == "이번 달" else "올해 1월 1일~오늘"
    st.caption(f"{period} {short_won(tot_now)} · 작년 같은 기간 {short_won(tot_ly)} "
               f"({'+' if tot_now >= tot_ly else ''}{short_won(tot_now - tot_ly)})")
    if yoy.empty:
        st.success("작년 같은 기간보다 늘어난 계정이 없어요.")
    else:
        view = yoy.assign(
            올해=yoy["올해"].map(short_won), 작년=yoy["작년"].map(short_won),
            증가=yoy.apply(lambda r: f"+{short_won(r['증가'])}"
                          + (f" ({r['증가율']:+.0%})" if r["증가율"] == r["증가율"] and r["증가율"] is not None else " (신규)"),
                          axis=1),
        ).rename(columns={"원인": "무엇 때문에 늘었나"}).drop(columns=["증가율"])
        st.table(view, hide_index=True, alt="작년 대비 늘어난 비용과 원인")   # 원인 문장이 길어도 줄바꿈되도록 표로

    with st.expander("📅 이번 달 달력 — 정기 전표 기한 · 연간 항목 · 작년 이 시기 지출 · 세무 일정", expanded=True):
        with connect() as conn:
            events = month_events(conn, month)
            nxt = next_month_annual(conn, month)
        y_, m_ = map(int, month.split("-"))
        month_calendar(y_, m_, date.today().isoformat(), events, key=f"cal_{month}")
        if nxt:
            st.caption("다음 달 연간 항목 미리 보기: " + " · ".join(
                f"{a.name} ({a.due_date[5:].replace('-', '/')}, 예상 {short_won(a.expected_amount)})" for a in nxt))

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
