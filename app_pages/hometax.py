import streamlit as st

from voucher.config import settings
from voucher.db import connect
from voucher.hometax import HometaxError, RECON_LABEL, get_client, last_sync, month_range, reconcile, sync
from voucher.hometax_excel import ExcelHometaxClient
from voucher.queries import STATUS_LABEL
from voucher.ui import time_ago

if not st.session_state.user.is_admin:
    st.error("관리자만 볼 수 있는 화면입니다.")
    st.stop()

month = st.session_state.month
actor = st.session_state.user.user_id
start, end = month_range(month)

st.title("홈택스 대사", icon=":material/compare_arrows:", anchor=False)
st.caption("국세청(홈택스)에 신고된 매입 세금계산서와 시스템에 들어온 건을 승인번호로 맞춰 봅니다. "
           "메일로 안 온 세금계산서도 여기서 잡아 자동으로 등록합니다.")

# ---------- 수집 방법 ----------
free_tab, api_tab = st.tabs([":material/upload_file: 홈택스 엑셀 올리기 (무료)", ":material/cloud_sync: API 자동 수집 (유료)"])

with free_tab:
    st.markdown(
        "1. [홈택스](https://hometax.go.kr) 로그인 → **전자(세금)계산서 목록조회**\n"
        "2. **매입** 선택, 기간 지정 후 조회 → **엑셀내려받기**\n"
        "3. 받은 파일을 아래에 그대로 올리기 (편집 불필요)"
    )
    up = st.file_uploader("홈택스 엑셀 파일", type=["xlsx", "xls", "csv"], key="ht_excel",
                          help="예시 파일: samples/홈택스_매입목록_예시_202610.xlsx")
    auto = st.toggle("시스템에 없는 건 자동 등록", value=True, key="ht_auto_excel",
                     help="엑셀에만 있는 세금계산서를 검토·전기 대기열에 넣습니다 (품목은 대표 1건으로 등록).")
    if up and st.button("가져오기", type="primary", icon=":material/download:"):
        try:
            client = ExcelHometaxClient(up.getvalue(), up.name)
            s, e = client.period
            with connect() as conn:
                r = sync(conn, s, e, client=client, auto_ingest=auto, actor=actor)
            st.success(f"{s} ~ {e} · {r.fetched}건 확인 · 시스템에 없던 {r.new_in_hometax}건 중 {r.ingested}건 등록",
                       title="가져오기 완료")
            if r.errors:
                st.error("\n".join(r.errors), title="일부 건 등록 실패")
            with st.expander("인식한 컬럼"):
                st.json(client.columns)
        except HometaxError as e:
            st.error(str(e), title="파일을 읽지 못했어요")

with api_tab:
    provider = {"popbill": "팝빌 홈택스 연동", "mock": "데모 (가상 홈택스 데이터)"}.get(
        settings.hometax_provider, settings.hometax_provider)
    st.caption(f"매일 자동으로 홈택스를 조회합니다. 연동: **{provider}**. "
               "팝빌 등 ASP의 홈택스 수집은 유료(정액제)입니다. 설정 방법은 README '홈택스 연동' 참고.")
    with st.container(horizontal=True, vertical_alignment="center"):
        auto_api = st.toggle("시스템에 없는 건 자동 등록", value=True, key="ht_auto_api")
        if st.button(f"{start:%m/%d}~{end:%m/%d} 수집", icon=":material/cloud_download:"):
            with st.spinner("홈택스에서 매입 세금계산서를 조회하는 중입니다 (수십 초 걸릴 수 있음)"):
                try:
                    with connect() as conn:
                        r = sync(conn, start, end, auto_ingest=auto_api, actor=actor)
                    st.toast(f"홈택스 {r.fetched}건 조회 · 미수집 {r.new_in_hometax}건 · 신규 등록 {r.ingested}건")
                    if r.errors:
                        st.error("\n".join(r.errors), title="일부 건 등록 실패")
                except HometaxError as e:
                    st.error(str(e), title="홈택스 수집 실패")
    if settings.hometax_provider == "popbill" and st.button("팝빌 연동 상태 확인", icon=":material/link:"):
        try:
            client = get_client()
            st.table(client.check_status(), alt="팝빌 홈택스 연동 상태")
            st.link_button("홈택스 인증서 등록 (팝빌)", client.certificate_url(), icon=":material/key:")
        except HometaxError as e:
            st.error(str(e))

# ---------- 대사 결과 ----------
with connect() as conn:
    df = reconcile(conn, month)
    ls = last_sync(conn)

st.subheader(f"{month} 대사 결과", divider="gray", anchor=False)
if ls:
    src = {"hometax_excel": "엑셀", "popbill": "팝빌", "mock": "데모"}.get(ls["provider"], ls["provider"])
    st.caption(f"마지막 확인 {time_ago(ls['at'])} ({src}) · {ls['start_date']}~{ls['end_date']} · "
               f"{ls['fetched']}건 확인, {ls['ingested']}건 등록")

counts = df["recon"].value_counts()
with st.container(horizontal=True):
    st.metric("홈택스 매입", f"{(df['홈택스 금액'].notna()).sum()}건", border=True)
    st.metric("✅ 일치", f"{counts.get('MATCH', 0)}건", border=True)
    st.metric("🔴 시스템 미수집", f"{counts.get('HOMETAX_ONLY', 0)}건", border=True,
              help="국세청에는 있는데 시스템에 없음 → 자동 등록을 켜고 다시 가져오기")
    st.metric("🟠 금액 불일치", f"{counts.get('AMOUNT_DIFF', 0)}건", border=True)
    st.metric("🟡 홈택스 미확인", f"{counts.get('SYSTEM_ONLY', 0)}건", border=True,
              help="시스템에는 있는데 국세청 목록에 없음 → 홈택스 자료를 아직 안 올렸거나, 취소·잘못된 세금계산서")

if df.empty:
    st.info("이 달의 데이터가 없습니다.")
    st.stop()
if not counts.get("MATCH") and not df["홈택스 금액"].notna().any():
    st.info("아직 이 달의 홈택스 자료가 없어요. 위에서 홈택스 엑셀을 올려 주세요.", icon=":material/upload_file:")

ICON = {"MATCH": "✅", "AMOUNT_DIFF": "🟠", "HOMETAX_ONLY": "🔴", "SYSTEM_ONLY": "🟡"}
df["대사"] = df["recon"].map(lambda k: f"{ICON[k]} {RECON_LABEL[k]}")
df["상태"] = df["상태"].map(STATUS_LABEL)
issues_only = st.toggle("문제 건만 보기", value=bool((df.recon != "MATCH").any()))
view = df[df.recon != "MATCH"] if issues_only else df
st.dataframe(
    view, hide_index=True, placeholder="-", alt="홈택스 대사 결과",
    column_order=["대사", "작성일", "공급자", "홈택스 금액", "시스템 금액", "상태", "수집경로", "sap_doc_no", "approval_no"],
    column_config={
        "홈택스 금액": st.column_config.NumberColumn(format="localized"),
        "시스템 금액": st.column_config.NumberColumn(format="localized"),
        "상태": st.column_config.TextColumn("처리 상태"),
        "sap_doc_no": "SAP 전표번호", "approval_no": "승인번호",
    },
)
