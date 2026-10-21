"""예산 및 전표관리 (샘플).

    .venv\\Scripts\\python -m streamlit run streamlit_app.py

샘플이라 로그인은 없다. 사이드바 '화면 보기'에서 총무 담당자(관리자) / 일반 사용자 화면을 바꿔 볼 수 있다.
운영 전환 시에는 사내 SSO(st.login) 등으로 로그인을 붙이고 '화면 보기' 전환은 없애야 한다.
"""
import streamlit as st

from voucher.auth import ROLE_LABEL, get_user
from voucher.db import connect, init_db
from voucher.queries import month_options

APP_NAME = "예산 및 전표관리"

st.set_page_config(page_title=APP_NAME, page_icon=":material/account_balance_wallet:", layout="wide")
init_db()


@st.cache_resource(show_spinner=False)
def ensure_demo_data() -> bool:
    """DB가 비어 있거나(클라우드 첫 실행) 예시 데이터 버전이 바뀌었으면 예시 데이터를 새로 만든다."""
    from voucher.db import get_meta
    from voucher.seed import SEED_VERSION, run
    with connect() as conn:
        has_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        version = get_meta(conn, "seed_version")
    if has_users and version == SEED_VERSION:
        return False
    run(reset=True)
    return True


with st.spinner("처음 실행이라 예시 데이터를 만드는 중입니다 (약 10~20초)…"):
    ensure_demo_data()

st.session_state.setdefault("month", month_options()[0])

# ---------- 화면 보기 (로그인 대신) ----------
with connect() as conn:
    people = conn.execute(
        "SELECT u.user_id, u.name, u.role, c.name AS dept FROM users u "
        "LEFT JOIN cost_centers c ON c.code = u.cost_center WHERE u.active = 1 "
        "ORDER BY CASE u.role WHEN 'admin' THEN 0 ELSE 1 END, u.user_id").fetchall()
if not people:
    st.error("사용자가 없습니다. 예시 데이터를 먼저 만드세요: `.venv\\Scripts\\python -m voucher.seed`")
    st.stop()
labels = {p["user_id"]: f"{p['name']} · {ROLE_LABEL.get(p['role'], p['role'])} ({p['dept'] or '-'})" for p in people}
st.session_state.setdefault("viewer", people[0]["user_id"])
if st.session_state.viewer not in labels:
    st.session_state.viewer = people[0]["user_id"]

with st.sidebar:
    st.markdown(f"### :material/account_balance_wallet: {APP_NAME}")
    st.selectbox("화면 보기", list(labels), format_func=labels.get, key="viewer",
                 help="샘플이라 로그인이 없습니다. 총무 담당자(관리자)와 일반 사용자 화면을 바꿔 볼 수 있어요.")

with connect() as conn:
    user = get_user(conn, st.session_state.viewer)
st.session_state.user = user
st.session_state.actor = user.user_id

# ---------- 권한별 메뉴 ----------
common = [
    st.Page("app_pages/dashboard.py", title="대시보드", icon=":material/space_dashboard:", default=True),
    *([st.Page("app_pages/budget.py", title="총무 예산", icon=":material/savings:"),
       st.Page("app_pages/ai_helper.py", title="AI 도우미", icon=":material/smart_toy:")] if user.is_admin else []),
    st.Page("app_pages/recurring.py", title="전표 점검", icon=":material/event_repeat:"),
    st.Page("app_pages/review.py", title="검토·전기" if user.is_admin else "검토·승인 요청",
            icon=":material/rule:"),
    st.Page("app_pages/my_costs.py", title="부서별 비용", icon=":material/pie_chart:"),
    st.Page("app_pages/invoices.py", title="세금계산서 조회", icon=":material/search:"),
]
admin = [
    st.Page("app_pages/hometax.py", title="홈택스 대사", icon=":material/compare_arrows:"),
    st.Page("app_pages/masters.py", title="기준정보", icon=":material/tune:"),
    st.Page("app_pages/users.py", title="사용자 관리", icon=":material/group:"),
]
page = st.navigation({"업무": common, "관리자": admin} if user.is_admin else {"업무": common})

with st.sidebar:
    st.selectbox("기준 월", month_options(), key="month")
    from voucher import ai
    st.caption(f"샘플 데이터 기준일 2026-10-21 · SAP는 모의 모드 · {ai.status().label}")

page.run()
