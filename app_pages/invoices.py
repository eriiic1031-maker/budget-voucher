import json
from pathlib import Path

import streamlit as st

from voucher.attach_ui import attachment_panel
from voucher.attachments import counts
from voucher.db import connect
from voucher.queries import STATUS_LABEL, audit_log, invoices

month = st.session_state.month
actor = st.session_state.actor

st.title("세금계산서 조회", icon=":material/search:", anchor=False)

with st.container(horizontal=True, vertical_alignment="bottom"):
    status = st.pills("상태", list(STATUS_LABEL.values()), selection_mode="multi")
    keyword = st.text_input("공급자 / 승인번호 검색")
    only_att = st.toggle("증빙 있는 건만")

df = invoices(month)
with connect() as conn:
    att = counts(conn, "invoice")
df["증빙"] = df.id.astype(str).map(att).fillna(0).astype(int)
if status:
    df = df[df["상태"].isin(status)]
if keyword:
    df = df[df.supplier_name.str.contains(keyword, case=False, na=False)
            | df.approval_no.str.contains(keyword, na=False)]
if only_att:
    df = df[df["증빙"] > 0]

st.caption(f"{month} · {len(df)}건 · 합계 {df.total_amount.sum():,}원 · 증빙 붙은 건 {(df['증빙'] > 0).sum()}건")
sel = st.dataframe(
    df.assign(증빙=df["증빙"].map(lambda n: f"📎 {n}" if n else "")), hide_index=True, key="inv_table",
    on_select="rerun", selection_mode="single-row", placeholder="-",
    column_order=["issue_date", "상태", "supplier_name", "supplier_bizno", "supply_amount", "tax_amount",
                  "total_amount", "증빙", "sap_doc_no", "posted_by", "approval_no", "source"],
    column_config={
        "issue_date": "발행일", "supplier_name": "공급자", "supplier_bizno": "사업자번호",
        "supply_amount": st.column_config.NumberColumn("공급가액", format="localized"),
        "tax_amount": st.column_config.NumberColumn("세액", format="localized"),
        "total_amount": st.column_config.NumberColumn("합계", format="localized"),
        "증빙": st.column_config.TextColumn("증빙", width="small"),
        "sap_doc_no": "SAP 전표번호", "posted_by": "전기자", "approval_no": "승인번호", "source": "수집경로",
    },
    alt="세금계산서 전체 목록",
)

if sel.selection.rows:
    inv_id = int(df.iloc[sel.selection.rows[0]].id)
    with connect() as conn:
        p = conn.execute("SELECT payload_json FROM postings WHERE invoice_id = ?", (inv_id,)).fetchone()
        raw = conn.execute("SELECT raw_path, approval_no FROM invoices WHERE id = ?", (inv_id,)).fetchone()
    left, right = st.columns(2)
    with left, st.container(border=True):
        st.subheader("SAP 전표 (BAPI_ACC_DOCUMENT_POST)")
        if p:
            st.json(json.loads(p["payload_json"]), expanded=2)
        else:
            st.caption("아직 전기되지 않았습니다.")
    with right:
        with st.container(border=True):
            attachment_panel("invoice", inv_id, actor, key=f"inv_att_{inv_id}",
                             hint="전기 후에도 견적서·계약서를 붙이거나 받을 수 있습니다.")
            if raw["raw_path"] and Path(raw["raw_path"]).exists():
                st.download_button("원본 전자세금계산서 XML 받기", Path(raw["raw_path"]).read_bytes(),
                                   file_name=f"{raw['approval_no']}.xml", mime="application/xml",
                                   icon=":material/code:", type="tertiary")
        with st.container(border=True):
            st.subheader("처리 이력")
            st.dataframe(audit_log(inv_id), hide_index=True, alt="세금계산서 처리 이력")
