import streamlit as st

from voucher.queries import expense_lines, name_maps, prev_month
from voucher.ui import pct_change, short_won

user = st.session_state.user
month = st.session_state.month
cc_names, _ = name_maps()

st.title("부서별 비용", icon=":material/pie_chart:", anchor=False)
st.caption("총무 예산 항목(임차료·관리비·수도광열비·통신비·렌탈·차량·복리후생·용역·소모품·수선비)만 부서별로 보여줍니다.")

if user.is_admin:
    codes = list(cc_names)
    cc = st.segmented_control("부서", codes, default=user.cost_center if user.cost_center in codes else codes[0],
                              format_func=lambda c: cc_names.get(c, c), required=True)
else:
    cc = user.cost_center
    st.caption(f"{cc_names.get(cc, cc)}에 배부된 총무 비용입니다. 다른 부서 비용은 총무팀에 문의하세요.")

lines = expense_lines([prev_month(month), month])
lines = lines[lines["부서코드"] == cc]
cur, prev = lines[lines["월"] == month], lines[lines["월"] == prev_month(month)]

with st.container(horizontal=True):
    st.metric(f"{month} 비용", short_won(cur["금액"].sum()), pct_change(cur["금액"].sum(), prev["금액"].sum()),
              delta_color="inverse", delta_description="전월 대비 (%)", border=True)
    st.metric("건수", f"{cur.invoice_id.nunique()}건", int(cur.invoice_id.nunique() - prev.invoice_id.nunique()),
              delta_description="전월 대비 (건)", border=True)
    top = cur.groupby("계정")["금액"].sum().sort_values(ascending=False)
    st.metric("가장 큰 항목", top.index[0] if len(top) else "-", None, border=True,
              help=short_won(top.iloc[0]) if len(top) else None)

if cur.empty:
    st.info("이번 달 전기된 비용이 아직 없습니다.")
    st.stop()

left, right = st.columns([2, 3])
with left, st.container(border=True, height="stretch"):
    st.markdown("**계정별**")
    st.bar_chart(top.reset_index(), x="계정", y="금액", horizontal=True, alt="계정별 비용")
with right, st.container(border=True, height="stretch"):
    st.markdown("**사용자별** (차량 등 개인에게 귀속된 비용)")
    by_user = cur.dropna(subset=["사용자ID"]).groupby(["사용자", "계정"], as_index=False)["금액"].sum()
    if by_user.empty:
        st.caption("개인에게 귀속된 비용이 없습니다.")
    else:
        st.bar_chart(by_user, x="사용자", y="금액", color="계정", horizontal=True, alt="사용자별 비용")

st.markdown("**상세 내역**")
st.dataframe(
    cur.sort_values("발행일", ascending=False), hide_index=True, placeholder="-", alt="부서 비용 상세 내역",
    column_order=["발행일", "공급자", "품목", "계정", "사용자", "금액", "SAP 전표번호"],
    column_config={"금액": st.column_config.NumberColumn(format="localized")},
)
