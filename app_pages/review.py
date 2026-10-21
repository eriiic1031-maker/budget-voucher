import json

import pandas as pd
import streamlit as st

from voucher.annual import due_items
from voucher.assist import last_suggestion, log_suggestion, suggest
from voucher.attach_ui import attachment_panel
from voucher.classifier import Proposal, ProposedLine, validate
from voucher.db import audit, connect, now
from voucher.pipeline import ingest_xml, post_invoice, process, row_to_invoice, submit_invoice
from voucher.queries import invoices, name_maps
from voucher.ui import won

user = st.session_state.user
actor = user.user_id
is_admin = user.is_admin
cc_names, gl_names = name_maps()
st.session_state.setdefault("review_nonce", 0)


def finish():
    """처리 후 목록이 바뀌므로 선택을 초기화한다 (다른 건이 같은 행 위치로 올라와 잘못 전기되는 것 방지)."""
    st.session_state.review_nonce += 1
    st.rerun()


st.title("검토·전기" if is_admin else "검토·승인 요청", icon=":material/fact_check:", anchor=False)
st.caption("자동 분류가 확신하지 못한 세금계산서를 확인합니다. "
           + ("승인 요청 건을 확인하고 SAP에 전기하세요." if is_admin else
              "계정·부서를 채워 **승인 요청**하면 관리자가 확인 후 전기합니다."))

# ---------- 이번 달 연간 항목 알림 ----------
if is_admin:
    with connect() as conn:
        ann = [a for a in due_items(conn, st.session_state.month) if a.status != "DONE"]
    if ann:
        st.info("이번 달 처리할 연간 항목: " + " · ".join(
            f"**{a.name}** (기한 {a.due_date[5:].replace('-', '/')}, 예상 {won(a.expected_amount)})" for a in ann)
            + " — 처리했으면 전표 점검 › 연간 항목에서 체크하세요.", icon=":material/event:")

# ---------- 수동 업로드 (관리자) ----------
if is_admin:
    with st.expander("XML 직접 업로드", icon=":material/upload_file:"):
        files = st.file_uploader("전자세금계산서 XML", type=["xml"], accept_multiple_files=True)
        if files and st.button("업로드 처리", icon=":material/play_arrow:"):
            with connect() as conn:
                for f in files:
                    r = ingest_xml(conn, f.getvalue(), "upload", f.name)
                    st.write(f"{f.name}: {r.status} {r.message}")

# ---------- 목록 ----------
pending = invoices(statuses=["SUBMITTED", "REVIEW", "ERROR", "READY"])
GROUPS = {
    "승인 요청": ["SUBMITTED"],
    "검토 대기": ["REVIEW", "ERROR"],
    "전기 대기": ["READY"],
}
if not is_admin:
    GROUPS = {"검토 필요": ["REVIEW", "ERROR"], "내 승인 요청": ["SUBMITTED"]}

counts = {g: int(pending.status.isin(s).sum()) for g, s in GROUPS.items()}
pref = st.session_state.pop("review_group_pref", None)   # 대시보드 카드에서 고른 구분
if pref in GROUPS:
    st.session_state.review_nonce += 1                   # 새 기본값이 적용되도록 위젯을 새로 만든다
default = pref if pref in GROUPS else next((g for g, c in counts.items() if c), list(GROUPS)[0])
group = st.segmented_control("구분", list(GROUPS), default=default, required=True,
                             format_func=lambda g: f"{g} {counts[g]}", label_visibility="collapsed",
                             key=f"group_{st.session_state.review_nonce}")
view = pending[pending.status.isin(GROUPS[group])]
if group == "내 승인 요청":
    view = view[view.submitted_by == actor]

if group == "전기 대기" and len(view):
    with st.container(horizontal=True, vertical_alignment="center"):
        st.caption(f"자동 분류가 끝난 {len(view)}건 · 합계 {won(view.total_amount.sum())}")
        if st.button(f"{len(view)}건 일괄 전기", type="primary", icon=":material/publish:"):
            with connect() as conn:
                msgs = [post_invoice(conn, int(iid), actor) for iid in view.id]
            fails = [m for ok, m in msgs if not ok]
            st.toast(f"{len(msgs) - len(fails)}건 전기 완료" + (f", {len(fails)}건 실패" if fails else ""))
            finish()

if view.empty:
    st.success("이 구분에는 처리할 세금계산서가 없어요. 🎉")
    st.stop()

sel = st.dataframe(
    view, hide_index=True, key=f"pending_table_{st.session_state.review_nonce}", on_select="rerun",
    selection_mode="single-row", placeholder="-",
    column_order=["issue_date", "상태", "supplier_name", "total_amount", "submitted_by", "status_reason"],
    column_config={
        "issue_date": "발행일", "supplier_name": "공급자", "submitted_by": "요청자",
        "total_amount": st.column_config.NumberColumn("합계", format="localized"),
        "status_reason": st.column_config.TextColumn("사유", width="large"),
    },
    alt="처리 대기 세금계산서 목록",
)

if not sel.selection.rows:
    st.caption("👆 행을 선택하면 분개를 확인하고 처리할 수 있어요.")
    st.stop()

invoice_id = int(view.iloc[sel.selection.rows[0]].id)
with connect() as conn:
    row = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
inv = row_to_invoice(row)
proposal = Proposal.from_dict(json.loads(row["proposal_json"] or "{}"))
readonly = not is_admin and row["status"] == "SUBMITTED"

# ---------- 상세 ----------
with st.container(border=True):
    st.subheader(f"{inv.supplier_name} · {won(inv.total_amount)}", anchor=False)
    st.caption(f"승인번호 {inv.approval_no} · 발행일 {inv.issue_date} · 사업자번호 {inv.supplier_bizno}"
               + (" · 수정세금계산서" if inv.is_modified else "") + (f" · {inv.remark}" if inv.remark else ""))
    if row["status_reason"]:
        (st.info if row["status"] == "SUBMITTED" else st.warning)(
            row["status_reason"], title=f"{row['submitted_by']} 님의 요청" if row["status"] == "SUBMITTED"
            else "확인이 필요한 이유")

    st.dataframe(pd.DataFrame([i.__dict__ for i in inv.items]), hide_index=True, alt="세금계산서 품목",
                 column_order=["seq", "name", "description", "supply_amount", "tax_amount"],
                 column_config={"seq": "No", "name": "품목", "description": "비고",
                                "supply_amount": st.column_config.NumberColumn("공급가액", format="localized"),
                                "tax_amount": st.column_config.NumberColumn("세액", format="localized")})

    with connect() as conn:
        sug = last_suggestion(conn, invoice_id)
    if sug and row["status"] != "POSTED":
        with st.container(border=True):
            with st.container(horizontal=True, vertical_alignment="center"):
                src = {"ai": "🤖 로컬 AI 추천", "history": "📚 과거 비슷한 건으로 추천"}.get(sug["source"], "추천 없음")
                st.markdown(f"**{src}**")
                if sug.get("gl_account"):
                    st.badge(f"신뢰도 {sug['confidence']:.0%}",
                             color="green" if sug["confidence"] >= 0.7 else "orange")
                st.space("stretch")
                if is_admin and st.button("다시 추천받기", icon=":material/auto_awesome:", key=f"resug_{invoice_id}",
                                          help="기준정보를 바꿨거나 로컬 AI를 켠 뒤 다시 추천받습니다."):
                    with st.spinner("추천하는 중…"):
                        with connect() as conn:
                            s = suggest(conn, inv.supplier_bizno, inv.supplier_name,
                                        " ".join(i.name for i in inv.items), inv.supply_amount)
                            log_suggestion(conn, invoice_id, s)
                            if s.gl_account:
                                for l in proposal.lines:
                                    l.gl_account = s.gl_account
                                    l.cost_center = l.cost_center or s.cost_center
                                conn.execute("UPDATE invoices SET proposal_json=? WHERE id=?",
                                             (json.dumps(proposal.to_dict(), ensure_ascii=False), invoice_id))
                    st.session_state[f"ed_ver_{invoice_id}"] = st.session_state.get(f"ed_ver_{invoice_id}", 0) + 1
                    st.rerun()
            if sug.get("gl_account"):
                st.caption(f"{gl_names.get(sug['gl_account'], sug['gl_account'])} · "
                           f"{cc_names.get(sug.get('cost_center'), '공통 배부(부서 선택 필요)')} — {sug['reason']} "
                           "· 분개 표에 미리 채웠어요. 맞으면 그대로 전기하세요.")
            else:
                st.caption(sug.get("reason", ""))

    vendor = st.text_input("SAP 공급업체 코드", value=proposal.sap_vendor or "", key=f"vendor_{invoice_id}",
                           disabled=readonly, help="모르면 비워 두고 요청 메모에 적어 주세요." if not is_admin else None)

    st.markdown("**차변 분개** · 행을 추가하면 부서·사용자별로 나눌 수 있어요. 부가세대급금은 공제 세액 합계로 자동 생성됩니다.")
    lines_df = pd.DataFrame([l.__dict__ for l in proposal.lines]) if proposal.lines else pd.DataFrame(
        columns=list(ProposedLine.__dataclass_fields__))
    edited = st.data_editor(
        lines_df, key=f"lines_{invoice_id}_{st.session_state.get(f'ed_ver_{invoice_id}', 0)}",
        num_rows="fixed" if readonly else "dynamic", hide_index=True,
        disabled=readonly,
        column_order=["item_name", "gl_account", "cost_center", "user_id", "amount", "tax_amount", "tax_code",
                      "text"],
        column_config={
            "item_name": "품목명",
            "gl_account": st.column_config.SelectboxColumn(
                "계정", options=list(gl_names), required=True,
                format_func=lambda c: f"{gl_names.get(c, c)} ({c})"),
            "cost_center": st.column_config.SelectboxColumn(
                "부서", options=list(cc_names), required=True, format_func=lambda c: cc_names.get(c, c)),
            "user_id": "사용자(사번)",
            "amount": st.column_config.NumberColumn("비용 금액", format="localized", required=True),
            "tax_amount": st.column_config.NumberColumn("공제 세액", format="localized"),
            "tax_code": st.column_config.SelectboxColumn(
                "세금코드", options=["V1", "V3", "V0"], required=True,
                format_func=lambda c: {"V1": "V1 공제", "V3": "V3 불공제", "V0": "V0 면세"}.get(c, c)),
            "text": "적요",
        },
        alt="차변 분개 라인 편집",
    )

    def _s(v):
        return None if pd.isna(v) or str(v).strip() == "" else str(v).strip()

    new_lines = [ProposedLine(
        item_seq=int(r.item_seq) if pd.notna(r.item_seq) else 0, item_name=_s(r.item_name) or "",
        gl_account=_s(r.gl_account), cost_center=_s(r.cost_center), user_id=_s(r.user_id),
        amount=int(r.amount) if pd.notna(r.amount) else 0,
        tax_amount=int(r.tax_amount) if pd.notna(r.tax_amount) else 0,
        tax_code=_s(r.tax_code) or "V1", rule_id=None, text=_s(r.text) or inv.supplier_name,
    ) for r in edited.itertuples()]
    final = Proposal(lines=new_lines, sap_vendor=vendor.strip() or None)
    errs = validate(inv.total_amount, final, require_vendor=is_admin)

    debit = sum(l.amount + l.tax_amount for l in new_lines)
    with st.container(horizontal=True):
        st.metric("차변 합계", won(debit))
        st.metric("대변 (공급업체)", won(inv.total_amount))
        st.metric("차이", won(debit - inv.total_amount))
    if errs and not readonly:
        st.error("\n".join(f"- {e}" for e in errs), title="아직 처리할 수 없어요")
    elif not readonly:
        st.success("차변·대변이 맞습니다.", icon=":material/check_circle:")

    # ---------- 증빙자료 ----------
    st.divider()
    n_att = attachment_panel("invoice", invoice_id, actor, key=f"att_{invoice_id}",
                             hint="견적서·계약서·거래명세서·검수 사진 등. 전기 후에도 세금계산서 조회에서 볼 수 있습니다.")
    if inv.total_amount >= 3_000_000 and n_att == 0:
        st.warning("300만원 이상 건인데 증빙자료가 없어요. 전기는 할 수 있지만 견적서·품의서를 붙여 두는 것을 권장합니다.",
                   icon=":material/attach_file:")
    st.divider()

    # ---------- 사용자: 승인 요청 ----------
    if not is_admin:
        if readonly:
            st.caption("관리자 승인을 기다리는 중입니다. 수정이 필요하면 관리자에게 말씀해 주세요.")
        else:
            note = st.text_input("요청 메모", placeholder="예: 거래처 출장 중 주유, 신규 거래처 등록 부탁드립니다",
                                 key=f"note_{invoice_id}")
            if st.button("관리자에게 승인 요청", type="primary", icon=":material/send:", disabled=bool(errs)):
                with connect() as conn:
                    ok, msg = submit_invoice(conn, invoice_id, final, actor, note.strip())
                (st.toast if ok else st.error)(msg)
                if ok:
                    finish()
        st.stop()

    # ---------- 관리자: 전기 ----------
    with connect() as conn:
        vendor_known = conn.execute("SELECT 1 FROM vendors WHERE bizno=?", (inv.supplier_bizno,)).fetchone()
    save_vendor = st.checkbox("이 사업자번호를 공급업체 마스터에 저장", value=True,
                              key=f"sv_{invoice_id}") if not vendor_known else False
    save_rule = st.checkbox("이 분개를 공급자 규칙으로 저장 (다음부터 자동 분류)", key=f"rule_{invoice_id}",
                            help="첫 번째 라인의 계정과 부서로 공급자 단위 규칙을 만듭니다.")

    with st.container(horizontal=True):
        if st.button("SAP 전기", type="primary", icon=":material/publish:", disabled=bool(errs)):
            with connect() as conn:
                if save_vendor and final.sap_vendor:
                    conn.execute("INSERT OR REPLACE INTO vendors(bizno, sap_vendor, name) VALUES (?,?,?)",
                                 (inv.supplier_bizno, final.sap_vendor, inv.supplier_name))
                if save_rule and new_lines:
                    l0 = new_lines[0]
                    conn.execute(
                        "INSERT INTO account_rules(priority, supplier_bizno, gl_account, cost_center,"
                        " non_deductible, auto_post, memo) VALUES (20,?,?,?,?,0,?)",
                        (inv.supplier_bizno, l0.gl_account, l0.cost_center, int(l0.tax_code == "V3"),
                         f"검토 화면에서 생성 ({actor}, {now()[:10]})"))
                ok, msg = post_invoice(conn, invoice_id, actor, final)
            (st.toast if ok else st.error)(msg)
            if ok:
                finish()
        if row["status"] == "SUBMITTED" and st.button("반려", icon=":material/undo:",
                                                      help="요청자에게 다시 검토 대기로 돌려보냅니다."):
            with connect() as conn:
                conn.execute("UPDATE invoices SET status='REVIEW', status_reason=?, updated_at=? WHERE id=?",
                             (f"반려됨 ({user.name}) · 다시 확인해 주세요", now(), invoice_id))
                audit(conn, invoice_id, "REJECT", None, actor)
            finish()
        if st.button("재분류", icon=":material/refresh:", help="기준정보를 고친 뒤 규칙을 다시 적용합니다."):
            with connect() as conn:
                process(conn, invoice_id)
            finish()
        if st.button("전기 제외", icon=":material/block:", help="중복 수신, 타사 발행분 등 전표가 필요 없는 건"):
            with connect() as conn:
                conn.execute("UPDATE invoices SET status='IGNORED', updated_at=? WHERE id=?", (now(), invoice_id))
                audit(conn, invoice_id, "IGNORE", None, actor)
            finish()
