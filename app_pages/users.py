import secrets

import streamlit as st

from voucher.auth import ROLE_LABEL, upsert_user
from voucher.db import audit, connect
from voucher.queries import name_maps, table

if not st.session_state.user.is_admin:
    st.error("관리자만 볼 수 있는 화면입니다.")
    st.stop()

me = st.session_state.user
cc_names, _ = name_maps()

st.title("사용자 관리", icon=":material/group:", anchor=False)
st.caption("관리자는 예산·전기·기준정보·홈택스·사용자 관리까지, 사용자는 부서 비용 조회와 승인 요청까지 할 수 있습니다. "
           "샘플이라 로그인은 없고, 사이드바 '화면 보기'에서 여기 등록된 사람의 화면으로 바꿔 볼 수 있습니다.")

users = table("users").drop(columns=["password_hash"])
users["권한"] = users.role.map(ROLE_LABEL)
users["부서"] = users.cost_center.map(cc_names)
users["상태"] = users.active.map({1: "사용", 0: "중지"})

with st.container(horizontal=True):
    st.metric("전체", f"{len(users)}명", border=True)
    st.metric("관리자", f"{(users.role == 'admin').sum()}명", border=True)
    st.metric("사용자", f"{(users.role == 'user').sum()}명", border=True)

sel = st.dataframe(users, hide_index=True, on_select="rerun", selection_mode="single-row", key="users_table",
                   column_order=["user_id", "name", "권한", "부서", "상태"],
                   column_config={"user_id": "아이디", "name": "이름"}, alt="사용자 목록")

editing = users.iloc[sel.selection.rows[0]] if sel.selection.rows else None
with st.container(border=True):
    st.subheader(f"{editing['name']} 수정" if editing is not None else "새 사용자 추가", anchor=False)
    with st.form("user_form", clear_on_submit=editing is None):
        c1, c2 = st.columns(2)
        uid = c1.text_input("아이디 (사번)", value=editing["user_id"] if editing is not None else "",
                            disabled=editing is not None)
        name = c2.text_input("이름", value=editing["name"] if editing is not None else "")
        role = c1.segmented_control("권한", list(ROLE_LABEL), format_func=ROLE_LABEL.get, required=True,
                                    default=editing["role"] if editing is not None else "user")
        cc = c2.selectbox("부서", list(cc_names), format_func=cc_names.get,
                          index=list(cc_names).index(editing["cost_center"])
                          if editing is not None and editing["cost_center"] in cc_names else 0)
        active = st.toggle("사용", value=bool(editing["active"]) if editing is not None else True)
        if st.form_submit_button("저장", type="primary", icon=":material/save:"):
            uid = (uid or "").strip()
            if not uid or not name.strip():
                st.error("아이디와 이름을 입력하세요.")
            elif editing is not None and uid == me.user_id and (role != "admin" or not active):
                st.error("지금 보고 있는 계정의 관리자 권한이나 사용 상태는 해제할 수 없습니다.")
            else:
                # 샘플은 로그인이 없으므로 비밀번호는 쓰지 않는 임의 값으로만 채운다
                temp = secrets.token_urlsafe(16) if editing is None else None
                with connect() as conn:
                    upsert_user(conn, uid, name.strip(), role, cc, temp, active)
                    audit(conn, None, "USER_SAVE", {"user_id": uid, "role": role, "active": active}, me.user_id)
                st.toast(f"{name} 저장 완료")
                st.rerun()
