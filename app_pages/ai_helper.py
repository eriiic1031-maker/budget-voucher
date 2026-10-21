from datetime import date

import pandas as pd
import streamlit as st

from voucher import ai
from voucher.annual import due_items, mark_done
from voucher.anomaly import detect
from voucher.assist import accuracy
from voucher.db import connect
from voucher.docread import match_annual, read
from voucher.insight import ask, briefing, last_briefing
from voucher.ui import time_ago, won

if not st.session_state.user.is_admin:
    st.error("관리자만 볼 수 있는 화면입니다.")
    st.stop()

month = st.session_state.month
actor = st.session_state.actor

st.title("AI 도우미", icon=":material/smart_toy:", anchor=False)
stt = ai.status()
with st.container(horizontal=True, vertical_alignment="center"):
    st.badge(stt.label, icon=":material/memory:" if stt.available else ":material/rule:",
             color="green" if stt.available else "gray")
    if st.button("연결 다시 확인", icon=":material/refresh:", type="tertiary"):
        ai.status(refresh=True)
        st.rerun()
if not stt.available:
    st.caption("로컬 AI가 없어도 모든 기능은 **규칙·과거 기록 방식**으로 동작합니다. 내 PC에서 Ollama를 켜고 "
               f"`ollama pull {ai.MODEL}` 한 뒤 '연결 다시 확인'을 누르면 AI가 문장·추천·문서 읽기를 맡습니다. "
               "회사 데이터는 내 PC 밖으로 나가지 않습니다.")

st.session_state.setdefault("ai_view", "총무 브리핑")
with connect() as conn:
    anomalies = detect(conn, month)
view = st.segmented_control("보기", ["총무 브리핑", "질문하기", "문서 읽기", "이상 신호", "추천 정확도"],
                            required=True, key="ai_view", label_visibility="collapsed",
                            format_func=lambda v: f"이상 신호 {len(anomalies)}" if v == "이상 신호" else v)
SRC = {"ai": "🤖 로컬 AI가 작성", "rule": "📐 규칙 방식(문장 틀)으로 작성"}

# ============================================================ 총무 브리핑 ====
if view == "총무 브리핑":
    st.caption(f"{month} 총무 예산·정기 전표·연간 항목·작년 대비·이상 신호를 모아 팀장 보고용 브리핑을 만듭니다. "
               "숫자는 프로그램이 계산한 값만 씁니다.")
    if st.button("브리핑 만들기", type="primary", icon=":material/auto_awesome:"):
        with st.spinner("브리핑을 쓰는 중입니다… (로컬 AI는 30초~1분 걸릴 수 있어요)"):
            with connect() as conn:
                briefing(conn, month)
        st.rerun()   # AI 실패로 상태가 바뀌었으면 위 배지에도 반영
    with connect() as conn:
        last = last_briefing(conn, month)
    if last:
        text, src, at = last
        with st.container(border=True):
            st.markdown(text)
            st.caption(f"{SRC.get(src, src)} · {time_ago(at)}")
        st.download_button("텍스트로 받기", text.replace("**", ""), file_name=f"총무예산_브리핑_{month}.txt",
                           icon=":material/download:")
    else:
        st.info("아직 이 달의 브리핑이 없어요. '브리핑 만들기'를 누르세요.", icon=":material/description:")

# ============================================================== 질문하기 ====
elif view == "질문하기":
    st.caption("예산·전표에 대해 말로 물어보세요. 프로그램이 정해 둔 조회 기능 중 알맞은 것을 골라 계산한 숫자로만 답합니다.")
    examples = ["이번 달 차량유지비 왜 초과했어?", "작년보다 늘어난 비용 알려줘", "올해 누적으로 늘어난 거",
                "정기 전표 중에 아직 안 들어온 거", "이번 달 연간 항목 뭐 있어?", "7월 재산세 처리했어?"]
    picked = st.pills("예시 질문", examples, key="qa_pick")
    with st.form("qa", clear_on_submit=False):
        q = st.text_input("질문", value=picked or "", placeholder="예: 이번 달 복리후생비 괜찮아?")
        go = st.form_submit_button("물어보기", type="primary", icon=":material/send:")
    if go and q.strip():
        with st.spinner("찾아보는 중…"):
            with connect() as conn:
                ans = ask(conn, q.strip(), month)
        with st.container(border=True):
            st.markdown(ans.text)
            st.caption(f"{SRC.get(ans.source, ans.source)} · 사용한 조회: `{ans.tool}` "
                       + " ".join(f"{k}={v}" for k, v in ans.args.items() if v))
        if ans.table is not None and not ans.table.empty:
            with st.expander("근거 데이터"):
                st.dataframe(ans.table, hide_index=True, alt="질문에 쓴 근거 데이터")

# ============================================================== 문서 읽기 ====
elif view == "문서 읽기":
    st.caption("재산세 고지서·보험 갱신 안내문·점검 청구서 같은 PDF/사진을 올리면 금액·납부기한·항목을 뽑아 "
               "**연간 항목 처리**로 등록합니다. 예시 파일: `docs/sample_전기안전점검_청구서.pdf`")
    up = st.file_uploader("고지서·청구서 (PDF, PNG, JPG)", type=["pdf", "png", "jpg", "jpeg"], key="doc_up")
    if up and st.button("읽기", type="primary", icon=":material/document_scanner:"):
        with st.spinner("문서를 읽는 중…"):
            st.session_state.doc_result = read(up.getvalue(), up.name)
    r = st.session_state.get("doc_result")
    if r:
        with connect() as conn:
            items = conn.execute("SELECT id, name, months FROM annual_items WHERE active=1 ORDER BY id").fetchall()
            guess = match_annual(conn, r.item_name)
        st.caption({"ai": "🤖 로컬 AI가 읽었습니다.", "rule": "📐 PDF 글자에서 규칙으로 찾았습니다."}[r.source]
                   + (f" {r.note}" if r.note else "") + " 값을 확인·수정한 뒤 저장하세요.")
        ids = [i["id"] for i in items]
        with st.form("doc_save"):
            c1, c2 = st.columns(2)
            item_id = c1.selectbox("연간 항목", ids, index=ids.index(guess) if guess in ids else 0,
                                   format_func=lambda i: next(x["name"] for x in items if x["id"] == i))
            c2.text_input("문서에서 읽은 항목", value=r.item_name, disabled=True)
            amount = c1.number_input("금액(원)", min_value=0, step=10_000, value=int(r.amount or 0))
            due = c2.date_input("납부·지급 기한", value=date.fromisoformat(r.due_date) if r.due_date else date.today())
            note = st.text_input("메모", value=f"{r.issuer} {r.doc_type}".strip())
            if st.form_submit_button("연간 항목 처리로 저장", type="primary", icon=":material/task_alt:"):
                m = due.strftime("%Y-%m")
                with connect() as conn:
                    mark_done(conn, item_id, m, int(amount), due.isoformat(), note, actor)
                st.success(f"{m} 처리 완료로 저장했습니다. 전표 점검 › 연간 항목과 총무 예산에 반영됩니다.")
                st.session_state.pop("doc_result", None)
        if r.text:
            with st.expander("문서에서 읽은 글자"):
                st.text(r.text[:3000])

# ============================================================== 이상 신호 ====
elif view == "이상 신호":
    st.caption(f"{month} 비정기 전표 중 확인이 필요한 신호입니다. 찾는 것은 규칙(같은 데이터면 늘 같은 결과)으로, "
               "왜 이상한지는 문장으로 설명합니다. 문제없으면 전표 점검 › 비정기 전표에서 확인 완료하세요.")
    if not anomalies:
        st.success("이번 달은 이상 신호가 없어요.")
    ICON = {"중복 의심": "🔁", "분할 의심": "✂️", "금액 급증": "📈", "신규 고액": "🆕"}
    for a in anomalies:
        with st.container(border=True):
            with st.container(horizontal=True, vertical_alignment="center"):
                st.markdown(f"**{ICON.get(a.kind, '⚠️')} {a.kind}** · {a.supplier} '{a.item}'")
                st.space("stretch")
                st.caption(f"{a.date} · {won(a.amount)}")
            st.caption(a.explain)
    with st.expander("찾는 기준"):
        st.table(pd.DataFrame([
            ("🔁 중복 의심", "같은 공급자·같은 품목·같은 금액(50만원 이상)이 30일 안에 또 들어옴"),
            ("✂️ 분할 의심", "같은 품목을 3일 안에 3건 이상 비슷한 금액(또는 2건 같은 금액)으로, 각각 300만원 미만인데 합치면 300만원 이상"),
            ("📈 금액 급증", "그 공급자의 지난 12개월 평균보다 3배 이상 (100만원 이상)"),
            ("🆕 신규 고액", "최근 3개월 안에 처음 거래한 공급자의 300만원 이상 건"),
        ], columns=["신호", "기준"]), hide_index=True, alt="이상 신호 기준")

# ============================================================ 추천 정확도 ====
else:
    with connect() as conn:
        acc = accuracy(conn)
        log = pd.read_sql_query(
            """SELECT l.created_at AS 추천시각, i.supplier_name AS 공급자, l.source AS 방식, l.output_json, l.final_json,
                      l.accepted FROM ai_log l JOIN invoices i ON i.id = l.invoice_id
               WHERE l.feature='account' ORDER BY l.id DESC LIMIT 50""", conn)
        names = dict(conn.execute("SELECT code, name FROM gl_accounts"))
    st.caption("규칙이 없는 세금계산서에 계정·부서를 추천하고, 사람이 최종 전기한 결과와 비교한 기록입니다. "
               "검토·전기 화면에서 추천이 분개 표에 미리 채워집니다.")
    with st.container(horizontal=True):
        st.metric("추천 후 전기된 건", f"{acc['total']}건", border=True)
        st.metric("추천 그대로 전기", f"{acc['accepted']}건", border=True)
        st.metric("정확도", f"{acc['rate']:.0%}" if acc["rate"] is not None else "-", border=True,
                  help="추천한 계정·부서가 수정 없이 전기된 비율")
    if not log.empty:
        import json as _j
        log["추천"] = log.output_json.map(lambda s: names.get(_j.loads(s).get("gl_account"), "추천 없음"))
        log["근거"] = log.output_json.map(lambda s: _j.loads(s).get("reason", ""))
        log["최종"] = log.final_json.map(lambda s: ", ".join(dict.fromkeys(names.get(x["gl_account"], "") for x in
                                                                          _j.loads(s))) if isinstance(s, str) else "전기 전")
        log["결과"] = log.accepted.map({1: "✅ 그대로", 0: "✏️ 수정됨"}).fillna("⏳ 대기")
        log["방식"] = log["방식"].map({"ai": "🤖 AI", "history": "📚 과거 기록", "none": "-"})
        st.dataframe(log[["추천시각", "공급자", "방식", "추천", "최종", "결과", "근거"]], hide_index=True,
                     alt="계정 추천 기록", column_config={"근거": st.column_config.TextColumn(width="large")})
