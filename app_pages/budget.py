from datetime import date

import pandas as pd
import streamlit as st

from voucher.budget import (STATUS_ICON, STATUS_LABEL, annual_summary, budgets, elapsed_ratio, forecast_month,
                            load_lines, owner_accounts)
from voucher.db import audit, connect
from voucher.ui import short_won

if not st.session_state.user.is_admin:
    st.error("관리자만 볼 수 있는 화면입니다.")
    st.stop()

month = st.session_state.month
y, m = map(int, month.split("-"))
f = elapsed_ratio(month, date.today())

st.title("총무 예산", icon=":material/savings:", anchor=False)
st.caption(f"{month} · 기간 {f:.0%} 경과 · 총무 예산 계정만 집계합니다 (운반비·광고비 등 타 부서 예산 제외). "
           "월말 예상은 정기 전표 예정액 + 이번 달 지출 속도 + 작년 같은 달 패턴으로 계산합니다.")

view = st.segmented_control("보기", ["이번 달", "연간 누적", "예산 입력"], default="이번 달", required=True,
                            label_visibility="collapsed", key="budget_view")

# ================================================================ 이번 달 ====
if view == "이번 달":
    with connect() as conn:
        res = forecast_month(conn, month)
    tb, ta, tf = (sum(getattr(s, k) for s in res) for k in ("budget", "actual", "forecast"))
    n_over = sum(s.status == "OVER" for s in res)
    n_risk = sum(s.status == "RISK" for s in res)

    with st.container(horizontal=True):
        st.metric("이번 달 예산", short_won(tb), border=True)
        st.metric("사용", short_won(ta), border=True, help="전기 완료 + 받았지만 전기 전인 건")
        st.metric("월말 예상", short_won(tf), f"{(tf / tb - 1) * 100:+.1f}%" if tb else None,
                  delta_color="inverse", delta_description="예산 대비", border=True)
        st.metric("🔴 초과 / 🟠 초과 예상", f"{n_over} / {n_risk}", border=True)

    if tb:
        st.progress(min(ta / tb, 1.0), text=f"예산 사용률 {ta / tb:.0%} (기간 경과 {f:.0%}) · 월말 예상 {tf / tb:.0%}")

    alerts = [s for s in res if s.status in ("OVER", "RISK")]
    if alerts:
        st.subheader("예산 경고", anchor=False)
        for s in alerts:
            with st.container(border=True):
                with st.container(horizontal=True, vertical_alignment="center"):
                    st.markdown(f"#### {s.icon} {s.name}")
                    st.badge(s.label, color="red" if s.status == "OVER" else "orange")
                    st.space("stretch")
                    st.caption(f"예산 {short_won(s.budget)} · 사용 {short_won(s.actual)} · 월말 예상 "
                               f"{short_won(s.forecast)}")
                if s.budget:
                    st.progress(min(s.forecast / s.budget / 1.5, 1.0),
                                text=f"월말 예상 {s.forecast / s.budget:.0%} (막대 끝 = 예산의 150%)")
                st.markdown("**왜 그런가요?**\n" + "\n".join(f"- {r}" for r in s.reasons))
    else:
        st.success("이번 달은 예산을 넘거나 넘을 것으로 보이는 항목이 없어요. 🎉")

    watch = [s for s in res if s.status == "WATCH"]
    if watch:
        with st.expander(f"🟡 주의 {len(watch)}건 — 예산의 98~102% 예상", icon=":material/visibility:"):
            for s in watch:
                st.markdown(f"**{s.name}** · 예산 {short_won(s.budget)} · 월말 예상 {short_won(s.forecast)}\n"
                            + "\n".join(f"- {r}" for r in s.reasons))

    st.subheader("항목별 현황", anchor=False)
    table = pd.DataFrame([{
        "상태": f"{s.icon} {s.label}", "계정": s.name, "예산": s.budget, "사용": s.actual,
        "사용률": s.use_rate, "월말 예상": s.forecast, "예상률": s.forecast_rate,
        "정기 미수신 예정": s.recurring_remaining, "작년 같은 달": s.ly_actual,
    } for s in res])
    st.dataframe(table, hide_index=True, alt="총무 예산 항목별 사용 현황",
                 column_config={
                     **{c: st.column_config.NumberColumn(format="localized")
                        for c in ["예산", "사용", "월말 예상", "정기 미수신 예정", "작년 같은 달"]},
                     "사용률": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1.5),
                     "예상률": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1.5),
                 })

    chart = pd.DataFrame([{"계정": s.name, "구분": k, "금액": v} for s in res
                          for k, v in (("예산", s.budget), ("월말 예상", s.forecast), ("작년 같은 달", s.ly_actual))])
    with st.container(border=True):
        st.markdown("**예산 · 월말 예상 · 작년 같은 달**")
        st.bar_chart(chart, x="계정", y="금액", color="구분", stack=False, height=340,
                     alt="계정별 예산과 월말 예상, 작년 같은 달 실적 비교")

# ============================================================== 연간 누적 ====
elif view == "연간 누적":
    with connect() as conn:
        df = annual_summary(conn, y, m)
        lines = load_lines(conn, f"{y}-01-01", f"{y}-12-31", include_pending=True)
        ly = load_lines(conn, f"{y - 1}-01-01", f"{y - 1}-12-31", include_pending=False)
        b = budgets(conn, y)
    tot = df[["연간 예산", "누적 예산", "누적 실적", "작년 같은 기간", "작년 연간"]].sum()
    with st.container(horizontal=True):
        st.metric(f"{y}년 총무 예산", short_won(tot["연간 예산"]), border=True)
        st.metric(f"1~{m}월 사용", short_won(tot["누적 실적"]),
                  f"{(tot['누적 실적'] / tot['누적 예산'] - 1) * 100:+.1f}%" if tot["누적 예산"] else None,
                  delta_color="inverse", delta_description="같은 기간 예산 대비", border=True)
        st.metric("연간 집행률", f"{tot['누적 실적'] / tot['연간 예산']:.0%}" if tot["연간 예산"] else "-",
                  border=True, help=f"남은 예산 {short_won(tot['연간 예산'] - tot['누적 실적'])}")
        st.metric(f"{y - 1}년 총무 비용", short_won(tot["작년 연간"]), border=True,
                  help=f"작년 1~{m}월: {short_won(tot['작년 같은 기간'])}")

    months = [f"{y}-{i:02d}" for i in range(1, 13)]
    cum = pd.DataFrame({
        "월": months,
        "예산 누적": b.groupby("month").amount.sum().reindex(range(1, 13), fill_value=0).cumsum().values,
        f"{y}년 실적 누적": lines.groupby("month").amount.sum().reindex(months).cumsum().values,
        f"{y - 1}년 실적 누적": ly.groupby(ly.month.str[5:]).amount.sum().reindex([f"{i:02d}" for i in range(1, 13)])
        .cumsum().values,
    })
    cum.loc[cum["월"] > month, f"{y}년 실적 누적"] = None
    with st.container(border=True):
        st.markdown("**누적 예산 vs 실적**")
        st.line_chart(cum, x="월", height=320, alt="월별 누적 예산과 실적, 작년 실적 비교")

    st.dataframe(df.drop(columns=["gl"]), hide_index=True, alt="계정별 연간 예산 집행 현황",
                 column_config={
                     **{c: st.column_config.NumberColumn(format="localized")
                        for c in ["연간 예산", "누적 예산", "누적 실적", "작년 같은 기간", "작년 연간"]},
                     "누적 집행률": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1.5),
                     "연간 집행률": st.column_config.ProgressColumn(format="percent", min_value=0, max_value=1),
                 })

# ============================================================== 예산 입력 ====
else:
    year = st.selectbox("연도", [y + 1, y, y - 1], index=1)
    with connect() as conn:
        accounts = owner_accounts(conn)
        b = budgets(conn, year)
    grid = (b.pivot_table(index="gl_account", columns="month", values="amount", aggfunc="sum")
            .reindex(index=list(accounts), columns=range(1, 13)).fillna(0).astype(int))
    grid.columns = [f"{i}월" for i in range(1, 13)]
    grid.insert(0, "계정", [accounts[g] for g in grid.index])
    st.caption("단위: 원 · 총무 예산 계정만 표시됩니다. 계정 추가·예산 주관 변경은 기준정보 > 계정에서 합니다.")
    edited = st.data_editor(grid, key=f"budget_edit_{year}", disabled=["계정"], alt=f"{year}년 월별 예산 입력",
                            column_config={f"{i}월": st.column_config.NumberColumn(format="localized", min_value=0)
                                           for i in range(1, 13)})
    st.caption(f"연간 합계 {short_won(int(edited.drop(columns=['계정']).to_numpy().sum()))}")
    with st.container(horizontal=True):
        if st.button("저장", type="primary", icon=":material/save:"):
            rows = [(year, mi, gl, int(edited.loc[gl, f"{mi}월"] or 0)) for gl in edited.index for mi in range(1, 13)]
            with connect() as conn:
                conn.executemany("INSERT OR REPLACE INTO budgets VALUES (?,?,?,?)", rows)
                audit(conn, None, "BUDGET_SAVE", {"year": year}, st.session_state.actor)
            st.toast(f"{year}년 예산 저장 완료")
        if st.button(f"{year - 1}년 실적 × 1.05 로 채우기", icon=":material/auto_fix_high:",
                     help="작년 같은 달 실적에 5%를 더해 100만원 단위로 반올림해 바로 저장합니다."):
            with connect() as conn:
                ly = load_lines(conn, f"{year - 1}-01-01", f"{year - 1}-12-31", include_pending=False)
                ly["m"] = ly.month.str[5:7].astype(int)
                agg = ly.groupby(["m", "gl"]).amount.sum()
                rows = [(year, mi, gl, int(round(agg.get((mi, gl), 0) * 1.05, -6)))
                        for gl in accounts for mi in range(1, 13)]
                conn.executemany("INSERT OR REPLACE INTO budgets VALUES (?,?,?,?)", rows)
                audit(conn, None, "BUDGET_SAVE", {"year": year, "from": "ly*1.05"}, st.session_state.actor)
            st.rerun()
