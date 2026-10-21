import streamlit as st

from voucher import ai
from voucher.components import pdf_print_buttons
from voucher.db import connect
from voucher.report import collect, page_images, render_pdf
from voucher.ui import short_won

if not st.session_state.user.is_admin:
    st.error("관리자만 볼 수 있는 화면입니다.")
    st.stop()

month = st.session_state.month
user = st.session_state.user
y, m = month.split("-")

st.title("보고서", icon=":material/summarize:", anchor=False)
st.caption(f"{int(y)}년 {int(m)}월 총무 예산을 **상급자 보고용 PDF**로 만듭니다. 결재란 · 요약 지표 · 핵심 보고 사항 · "
           "계정별 현황 · 초과 사유 · 작년 대비 · 처리 현황 · 조치 계획 순서이며, 숫자는 모두 프로그램이 계산한 값입니다.")

with st.form("report_opt", border=True):
    c1, c2, c3 = st.columns(3)
    author = c1.text_input("작성자", value=user.name if "총무" in user.name else f"총무팀 {user.name}")
    audience = c2.text_input("보고 대상", value="총무팀장")
    approvers = c3.text_input("결재란 (쉼표로 구분)", value="담당, 팀장, 본부장")
    memo = st.text_area("담당자 의견 (선택)", placeholder="예: 차량 수리비 증가는 노후 차량 2대 때문으로, 내년 교체를 검토하겠습니다.",
                        height=80)
    stt = ai.status()
    use_ai = st.toggle("로컬 AI 종합 의견 넣기", value=stt.available, disabled=not stt.available,
                       help="로컬 AI(Ollama)가 연결돼 있을 때만 쓸 수 있습니다. 보안 정책상 외부 AI는 쓰지 않습니다."
                       if stt.available else f"지금은 쓸 수 없습니다: {stt.reason}. 보안 정책상 로컬 AI만 연동합니다.")
    go = st.form_submit_button("보고서 만들기", type="primary", icon=":material/description:")

if go:
    with st.spinner("보고서를 만드는 중…" + (" (로컬 AI 의견은 30초~1분 걸릴 수 있어요)" if use_ai else "")):
        with connect() as conn:
            data = collect(conn, month, use_ai=use_ai)
        signs = tuple(a.strip() for a in approvers.split(",") if a.strip())[:5] or ("담당", "팀장")
        pdf = render_pdf(data, author.strip() or "총무팀", audience.strip() or "팀장", memo, signs)
        st.session_state.report = {"month": month, "pdf": pdf, "pages": page_images(pdf),
                                   "messages": data.messages, "ai": bool(data.ai_summary),
                                   "kpi": (data.budget, data.actual, data.forecast)}

rep = st.session_state.get("report")
if not rep or rep["month"] != month:
    st.info("옵션을 확인하고 **보고서 만들기**를 누르세요. 미리보기와 PDF 다운로드·인쇄 버튼이 나타납니다.",
            icon=":material/description:")
    st.stop()

name = f"총무예산보고서_{month}.pdf"
with st.container(horizontal=True, vertical_alignment="center"):
    st.download_button("PDF 다운로드", rep["pdf"], file_name=name, mime="application/pdf", type="primary",
                       icon=":material/download:")
    pdf_print_buttons(rep["pdf"], key="report_print")
b, a, f = rep["kpi"]
st.caption(f"{len(rep['pages'])}쪽 · 예산 {short_won(b)} · 집행 {short_won(a)} · 월말 예상 {short_won(f)}"
           + (" · 🤖 종합 의견: 로컬 AI 작성" if rep["ai"] else ""))

with st.container(border=True):
    st.markdown("**핵심 보고 사항**")
    st.markdown("\n".join(f"{i}. {t}" for i, t in enumerate(rep["messages"], 1)))

st.subheader("미리보기", anchor=False)
tabs = st.tabs([f"{i}쪽" for i in range(1, len(rep["pages"]) + 1)])
for t, png in zip(tabs, rep["pages"]):
    with t:
        st.image(png, width=820)
