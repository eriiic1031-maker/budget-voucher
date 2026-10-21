import pandas as pd
import streamlit as st

from voucher.budget import invoice_overview
from voucher.db import connect
from voucher.recurring import STATUS_LABEL, check_month, mark, mark_checked, unmark, unmark_checked
from voucher.ui import short_won, won

month = st.session_state.month
actor = st.session_state.actor
st.session_state.setdefault("check_nonce", 0)

st.title("전표 점검", icon=":material/fact_check:", anchor=False)
st.caption(f"{month} · **정기 전표**는 매달 빠짐없이 들어왔는지, **비정기 전표**는 금액·용도·부서가 맞는지 확인합니다.")

with connect() as conn:
    rows = check_month(conn, month)
    ov = invoice_overview(conn, month, owner=None)
nonrec = ov[~ov.recurring.astype(bool)]
unchecked = int(nonrec.checked_by.isna().sum())
rec_open = sum(not r.ok for r in rows)

view = st.segmented_control(
    "구분", ["정기 전표", "비정기 전표"], default="정기 전표", required=True, label_visibility="collapsed",
    format_func=lambda v: f"{v} · 확인 필요 {rec_open}" if v == "정기 전표" else f"{v} · 미확인 {unchecked}",
    key="check_view")

ICON = {"DONE": "✅", "CONFIRMED": "☑️", "SKIPPED": "➖", "WAITING": "⏳",
        "NOT_POSTED": "🟡", "AMOUNT_DIFF": "🟠", "OVERDUE": "🔴"}

# ================================================================== 정기 ====
if view == "정기 전표":
    if not rows:
        st.info("등록된 정기 전표가 없습니다. 기준정보 > 정기 전표에서 추가하세요.")
        st.stop()
    counts = pd.Series([r.status for r in rows]).value_counts()
    with st.container(horizontal=True):
        st.metric("완료", f"{sum(r.ok for r in rows)} / {len(rows)}", border=True)
        st.metric("🔴 기한 경과", f"{counts.get('OVERDUE', 0)}건", border=True)
        st.metric("🟠 금액 상이", f"{counts.get('AMOUNT_DIFF', 0)}건", border=True)
        st.metric("🟡 수신·미전기", f"{counts.get('NOT_POSTED', 0)}건", border=True)
        st.metric("⏳ 수신 대기", f"{counts.get('WAITING', 0)}건", border=True)

    df = pd.DataFrame([{
        "상태": f"{ICON[r.status]} {r.label}", "항목": r.name, "기한": r.due_date,
        "예상 금액": r.expected_amount, "실제 금액": r.actual_amount,
        "차이": (r.actual_amount - r.expected_amount) if r.actual_amount is not None and r.expected_amount else None,
        "SAP 전표번호": r.sap_doc_no, "담당": r.owner, "메모": r.note,
    } for r in rows])
    sel = st.dataframe(
        df, hide_index=True, key="rec_table", on_select="rerun", selection_mode="single-row", placeholder="-",
        alt="정기 전표 월별 점검 결과",
        column_config={c: st.column_config.NumberColumn(format="localized") for c in ["예상 금액", "실제 금액", "차이"]},
    )
    st.caption("👆 행을 선택하면 확인 처리할 수 있어요. 점검 대상은 기준정보 > 정기 전표에서 관리합니다.")

    if sel.selection.rows:
        r = rows[sel.selection.rows[0]]
        with st.container(border=True):
            st.subheader(r.name, anchor=False)
            st.markdown(f"상태 **{r.label}** · 기한 {r.due_date} · 공급자 사업자번호 `{r.supplier_bizno}`")
            if r.status == "NOT_POSTED":
                st.info("세금계산서는 들어왔지만 아직 SAP에 전기되지 않았습니다.")
                st.page_link("app_pages/review.py", label="검토·전기로 이동", icon=":material/arrow_forward:")
            elif r.status == "OVERDUE":
                st.error("기한이 지났는데 세금계산서가 없습니다. 공급자에게 발행을 요청하거나, "
                         "홈택스 엑셀로 메일 미수신 건인지 확인하세요.")
            elif r.status == "AMOUNT_DIFF":
                st.warning(f"예상 {won(r.expected_amount)} 대비 {r.actual_amount - r.expected_amount:+,}원 차이. "
                           "사유를 확인한 뒤 확인 처리하세요.")
            if r.status in ("CONFIRMED", "SKIPPED"):
                if st.button("확인 취소", icon=":material/undo:"):
                    with connect() as conn:
                        unmark(conn, r.recurring_id, month)
                    st.rerun()
            elif r.status != "DONE":
                with st.form(f"mark_{r.recurring_id}"):
                    action = st.segmented_control("처리", ["확인 완료", "이번 달 해당 없음"], default="확인 완료")
                    note = st.text_input("메모", placeholder="예: 출력량 증가로 초과요금 발생, 공급자 확인함")
                    if st.form_submit_button("저장", type="primary", icon=":material/check:"):
                        if not note.strip():
                            st.error("메모를 입력하세요.")
                        else:
                            with connect() as conn:
                                mark(conn, r.recurring_id, month,
                                     "CONFIRMED" if action == "확인 완료" else "SKIPPED", note.strip(), actor)
                            st.rerun()

    with st.expander("상태 설명"):
        st.table(pd.DataFrame({"상태": [f"{ICON[k]} {v}" for k, v in STATUS_LABEL.items()],
                               "의미": ["SAP 전기 완료, 금액 허용범위 내", "전기는 됐으나 예상 금액과 허용범위 이상 차이",
                                        "세금계산서 수신, 전기 전", "기한 전, 아직 미수신", "기한이 지났는데 미수신",
                                        "담당자가 사유 확인 후 완료 처리", "이번 달은 발생하지 않는 건으로 처리"]}),
                 hide_index=True, alt="정기 전표 상태 설명")
    st.stop()

# ================================================================= 비정기 ====
with st.container(horizontal=True):
    st.metric("비정기 전표", f"{len(nonrec)}건", border=True, help=f"합계 {won(nonrec.amount.sum())}")
    st.metric("미확인", f"{unchecked}건", border=True)
    st.metric("큰 금액 (300만원↑)", f"{int(nonrec.big.sum())}건", border=True,
              help=short_won(nonrec[nonrec.big].amount.sum()))
    st.metric("신규 거래처", f"{int(nonrec.new_vendor.sum())}건", border=True, help="최근 3개월 안에 처음 거래한 곳")

with st.container(horizontal=True, vertical_alignment="bottom"):
    only_open = st.toggle("미확인만 보기", value=unchecked > 0)
    only_gm = st.toggle("총무 예산만", value=True, help="운반비·광고비 등 다른 부서 예산 계정은 숨깁니다.")
    flags = st.pills("강조", ["큰 금액", "신규 거래처"], selection_mode="multi", label_visibility="collapsed")

with connect() as conn:
    gm_accounts = {r[0] for r in conn.execute("SELECT name FROM gl_accounts WHERE owner='총무'")}
v = nonrec.copy()
if only_open:
    v = v[v.checked_by.isna()]
if only_gm:
    # 아직 계정이 없는 '미분류' 건은 총무 예산일 수 있으니 항상 보여준다
    v = v[v.account.apply(lambda a: a == "미분류" or any(x.strip() in gm_accounts for x in a.split(",")))]
if "큰 금액" in (flags or []):
    v = v[v.big]
if "신규 거래처" in (flags or []):
    v = v[v.new_vendor]

if v.empty:
    st.success("확인할 비정기 전표가 없어요. 🎉")
    st.stop()

v["표시"] = v.apply(lambda r: ("💰" if r.big else "") + ("🆕" if r.new_vendor else ""), axis=1)
v["확인"] = v.checked_by.notna()
STATUS = {"POSTED": "전기 완료", "READY": "전기 대기", "REVIEW": "검토 대기", "SUBMITTED": "승인 요청", "ERROR": "오류"}
v["처리"] = v.status.map(STATUS)

sel = st.dataframe(
    v, hide_index=True, key=f"nonrec_{st.session_state.check_nonce}", on_select="rerun",
    selection_mode="multi-row", placeholder="-", alt="비정기 전표 목록",
    column_order=["확인", "표시", "date", "supplier", "item", "account", "dept", "amount", "처리", "checked_by",
                  "check_note"],
    column_config={
        "확인": st.column_config.CheckboxColumn("확인", width="small"),
        "표시": st.column_config.TextColumn("", width="small", help="💰 300만원 이상 · 🆕 신규 거래처"),
        "date": "발행일", "supplier": "공급자", "item": st.column_config.TextColumn("품목", width="medium"),
        "account": "계정", "dept": "부서",
        "amount": st.column_config.NumberColumn("비용(공급가)", format="localized"),
        "checked_by": "확인자", "check_note": "메모",
    },
)
picked = v.iloc[sel.selection.rows] if sel.selection.rows else v.iloc[0:0]
st.caption(f"👆 여러 건을 선택해 한 번에 확인할 수 있어요. 선택 {len(picked)}건 · {won(picked.amount.sum())}")

if len(picked):
    with st.container(horizontal=True, vertical_alignment="bottom"):
        note = st.text_input("확인 메모 (선택)", placeholder="예: 대회의실 리모델링 1차 기성, 품의 2026-112")
        if st.button(f"{len(picked)}건 확인 완료", type="primary", icon=":material/done_all:"):
            with connect() as conn:
                mark_checked(conn, [int(i) for i in picked.invoice_id], actor, note.strip())
            st.session_state.check_nonce += 1
            st.toast(f"{len(picked)}건 확인했습니다")
            st.rerun()
        if picked.checked_by.notna().any() and st.button("확인 취소", icon=":material/undo:"):
            with connect() as conn:
                unmark_checked(conn, [int(i) for i in picked[picked.checked_by.notna()].invoice_id])
            st.session_state.check_nonce += 1
            st.rerun()
