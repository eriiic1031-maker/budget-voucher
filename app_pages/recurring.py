from datetime import date

import pandas as pd
import streamlit as st

from voucher.annual import due_items, mark_done, months_of
from voucher.annual import unmark as unmark_annual
from voucher.attach_ui import attachment_panel
from voucher.attachments import counts as attach_counts
from voucher.budget import invoice_overview
from voucher.db import connect
from voucher.recurring import STATUS_LABEL, check_month, mark, mark_checked, unmark, unmark_checked
from voucher.ui import short_won, won

month = st.session_state.month
actor = st.session_state.actor
st.session_state.setdefault("check_nonce", 0)

st.title("전표 점검", icon=":material/fact_check:", anchor=False)
st.caption(f"{month} · **정기 전표**는 매달 빠짐없이 들어왔는지, **비정기 전표**는 금액·용도·부서가 맞는지, "
           "**연간 항목**은 매년 이 시기에 내야 하는 세금·보험·점검을 처리했는지 확인합니다.")

with connect() as conn:
    rows = check_month(conn, month)
    ov = invoice_overview(conn, month, owner=None)
    annual = due_items(conn, month)
    ann_att = attach_counts(conn, "annual")
nonrec = ov[~ov.recurring.astype(bool)]
annual_open = sum(a.status != "DONE" for a in annual)
unchecked = int(nonrec.checked_by.isna().sum())
rec_open = sum(not r.ok for r in rows)

VIEW_LABEL = {"정기 전표": f"정기 전표 · 확인 필요 {rec_open}", "비정기 전표": f"비정기 전표 · 미확인 {unchecked}",
              "연간 항목": f"연간 항목 · 미처리 {annual_open}"}
st.session_state.setdefault("check_view", "정기 전표")
view = st.segmented_control("구분", list(VIEW_LABEL), required=True, label_visibility="collapsed",
                            format_func=VIEW_LABEL.get, key="check_view")

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

# ================================================================ 연간 항목 ====
if view == "연간 항목":
    ANN_ICON = {"DONE": "✅", "PENDING": "⏳", "OVERDUE": "🔴"}
    st.caption("세금계산서가 오지 않는 재산세·자동차세·보험 갱신·법정 점검 같은 항목입니다. 처리했으면 금액을 적고 "
               "**처리 완료**를 누르세요 → 총무 예산 실적에 들어갑니다. 처리 전에는 예상 금액이 월말 예상에 들어갑니다.")
    if not annual:
        st.success(f"{month}에는 처리할 연간 항목이 없어요.")
    for a in annual:
        with st.container(border=True):
            with st.container(horizontal=True, vertical_alignment="center"):
                st.markdown(f"**{ANN_ICON[a.status]} {a.name}**")
                st.badge(a.label, color={"DONE": "green", "PENDING": "blue", "OVERDUE": "red"}[a.status])
                st.badge(a.category or "기타", color="gray")
                st.space("stretch")
                st.caption(f"기한 {a.due_date} · 예상 {won(a.expected_amount)}")
            if a.item_note:
                st.caption(a.item_note)
            if a.status == "DONE":
                with st.container(horizontal=True, vertical_alignment="center"):
                    st.markdown(f"처리 금액 **{won(a.amount)}** · 처리일 {a.paid_date} · {a.checked_by}"
                                + (f" · {a.note}" if a.note else ""))
                    st.space("stretch")
                    if st.button("처리 취소", key=f"ann_undo_{a.item_id}", icon=":material/undo:"):
                        with connect() as conn:
                            unmark_annual(conn, a.item_id, month)
                        st.rerun()
            else:
                with st.form(f"ann_{a.item_id}_{month}", border=False):
                    c1, c2, c3 = st.columns([2, 2, 4], vertical_alignment="bottom")
                    amt = c1.number_input("처리 금액(원)", min_value=0, step=10_000, value=int(a.expected_amount or 0))
                    paid = c2.date_input("처리일", value=min(date.today(), date.fromisoformat(a.due_date)))
                    note = c3.text_input("메모", placeholder="예: 위택스 납부, 전년 대비 6% 인상")
                    if st.form_submit_button("처리 완료", type="primary", icon=":material/task_alt:"):
                        with connect() as conn:
                            mark_done(conn, a.item_id, month, int(amt), paid.isoformat(), note.strip(), actor)
                        st.toast(f"{a.name} 처리 완료")
                        st.rerun()
            n_att = ann_att.get(f"{a.item_id}:{month}", 0)
            with st.expander(f"📎 증빙자료 {n_att}개 (고지서·영수증·점검 결과서)"):
                attachment_panel("annual", f"{a.item_id}:{month}", actor, key=f"ann_att_{a.item_id}_{month}",
                                 title="📎 증빙자료")

    with st.expander("연간 항목 전체 일정 (1~12월)"):
        with connect() as conn:
            items = conn.execute("SELECT * FROM annual_items WHERE active=1 ORDER BY id").fetchall()
        grid = pd.DataFrame([{"항목": it["name"], "분류": it["category"], "예상 금액": it["expected_amount"],
                              **{f"{m}월": ("●" if m in months_of(it) else "") for m in range(1, 13)}}
                             for it in items])
        st.dataframe(grid, hide_index=True, alt="연간 항목 월별 일정",
                     column_config={"예상 금액": st.column_config.NumberColumn(format="localized")})
        st.caption("항목 추가·수정은 기준정보 › 연간 항목에서 합니다.")
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
