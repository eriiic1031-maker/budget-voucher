import json

import streamlit as st

from voucher.db import connect
from voucher.queries import STATUS_LABEL, audit_log, invoices

month = st.session_state.month

st.title("세금계산서 조회", icon=":material/search:", anchor=False)

with st.container(horizontal=True, vertical_alignment="bottom"):
    status = st.pills("상태", list(STATUS_LABEL.values()), selection_mode="multi")
    keyword = st.text_input("공급자 / 승인번호 검색")

df = invoices(month)
if status:
    df = df[df["상태"].isin(status)]
if keyword:
    df = df[df.supplier_name.str.contains(keyword, case=False, na=False)
            | df.approval_no.str.contains(keyword, na=False)]

st.caption(f"{month} · {len(df)}건 · 합계 {df.total_amount.sum():,}원")
sel = st.dataframe(
    df, hide_index=True, key="inv_table", on_select="rerun", selection_mode="single-row", placeholder="-",
    column_order=["issue_date", "상태", "supplier_name", "supplier_bizno", "supply_amount", "tax_amount",
                  "total_amount", "sap_doc_no", "posted_by", "approval_no", "source"],
    column_config={
        "issue_date": "발행일", "supplier_name": "공급자", "supplier_bizno": "사업자번호",
        "supply_amount": st.column_config.NumberColumn("공급가액", format="localized"),
        "tax_amount": st.column_config.NumberColumn("세액", format="localized"),
        "total_amount": st.column_config.NumberColumn("합계", format="localized"),
        "sap_doc_no": "SAP 전표번호", "posted_by": "전기자", "approval_no": "승인번호", "source": "수집경로",
    },
    alt="세금계산서 전체 목록",
)

if sel.selection.rows:
    inv_id = int(df.iloc[sel.selection.rows[0]].id)
    with connect() as conn:
        p = conn.execute("SELECT payload_json FROM postings WHERE invoice_id = ?", (inv_id,)).fetchone()
    left, right = st.columns(2)
    with left, st.container(border=True):
        st.subheader("SAP 전표 (BAPI_ACC_DOCUMENT_POST)")
        if p:
            st.json(json.loads(p["payload_json"]), expanded=2)
        else:
            st.caption("아직 전기되지 않았습니다.")
    with right, st.container(border=True):
        st.subheader("처리 이력")
        st.dataframe(audit_log(inv_id), hide_index=True, alt="세금계산서 처리 이력")
