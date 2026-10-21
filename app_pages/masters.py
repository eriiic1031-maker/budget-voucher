import pandas as pd
import streamlit as st

from voucher.db import connect
from voucher.queries import table

if not st.session_state.user.is_admin:
    st.error("관리자만 볼 수 있는 화면입니다.")
    st.stop()

st.title("기준정보", icon=":material/tune:", anchor=False)

# (테이블, 탭 이름, 설명, 컬럼 라벨)
MASTERS = {
    "recurring_vouchers": ("정기 전표", "매월 발행 여부를 점검할 항목입니다. 같은 공급자에 여러 건이면 키워드로 구분하세요.", {
        "id": None, "name": "항목명", "supplier_bizno": "공급자 사업자번호", "keyword": "품목 키워드",
        "expected_amount": "예상 합계금액", "tolerance_pct": "허용오차(%)", "due_day": "기한(일)",
        "owner": "담당", "gl_account": "예산 계정", "start_month": "시작월(YYYY-MM)", "active": "사용"}),
    "annual_items": ("연간 항목", "매년 같은 시기에 처리할 세금·보험·점검입니다. 처리 월은 쉼표로 구분 (예: 6,12).", {
        "id": None, "name": "항목명", "months": "처리 월", "due_day": "기한(일)", "gl_account": "예산 계정",
        "expected_amount": "예상 금액", "category": "분류", "note": "메모", "active": "사용"}),
    "account_rules": ("계정 규칙", "priority 가 작은 규칙부터 적용합니다. 공급자·키워드 중 하나는 반드시 지정하세요.", {
        "id": None, "priority": "우선순위", "supplier_bizno": "공급자 사업자번호", "keyword": "품목 키워드",
        "gl_account": "G/L 계정", "cost_center": "코스트센터", "alloc_key": "배부기준",
        "non_deductible": "불공제", "auto_post": "자동전기", "memo": "메모"}),
    "allocation_keys": ("배부 기준", "같은 key 의 비율 합계로 나눠 배부합니다 (끝수는 마지막 행).", {
        "key": "배부기준", "cost_center": "코스트센터", "user_id": "사용자(사번)", "ratio": "비율"}),
    "vehicles": ("차량", "품목·비고에 차량번호가 있으면 이 차량의 부서·사용자로 귀속합니다. 번호는 공백 없이 입력하세요.", {
        "plate": "차량번호", "cost_center": "코스트센터", "user_id": "사용자(사번)", "user_name": "사용자명",
        "non_deductible": "불공제"}),
    "vendors": ("공급업체", "사업자번호 → SAP 공급업체 코드(LIFNR) 매핑입니다.", {
        "bizno": "사업자번호", "sap_vendor": "SAP 공급업체", "name": "업체명", "payment_terms": "지급조건"}),
    "cost_centers": ("부서", "SAP 코스트센터 코드와 화면에 보일 부서명입니다.", {"code": "코스트센터", "name": "부서명"}),
    "gl_accounts": ("계정", "SAP G/L 계정과 예산 주관 부서입니다. 주관이 '총무'인 계정만 총무 예산·대시보드에 나옵니다.",
                    {"code": "G/L 계정", "name": "계정명", "owner": "예산 주관"}),
}
BOOL_COLS = {"active", "non_deductible", "auto_post"}

tabs = st.tabs([v[0] for v in MASTERS.values()])
for tab, (name, (label, desc, cols)) in zip(tabs, MASTERS.items()):
    with tab:
        st.caption(desc)
        df = table(name)
        for c in BOOL_COLS & set(df.columns):
            df[c] = df[c].astype(bool)
        edited = st.data_editor(
            df, key=f"edit_{name}", num_rows="dynamic", hide_index=True,
            column_order=[c for c, l in cols.items() if l],
            column_config={c: (st.column_config.CheckboxColumn(l) if c in BOOL_COLS else l)
                           for c, l in cols.items() if l},
            alt=f"{label} 기준정보 편집",
        )
        if st.button("저장", key=f"save_{name}", type="primary", icon=":material/save:"):
            data = edited.copy()
            for c in BOOL_COLS & set(data.columns):
                data[c] = data[c].fillna(False).astype(int)
            data = data.astype(object).where(pd.notna(data), None)
            try:
                rows_ = [tuple(v.item() if hasattr(v, "item") else v for v in row)  # numpy → python
                         for row in data.itertuples(index=False, name=None)]
                cols_ = list(data.columns)
                insert = f"INSERT INTO {name}({','.join(cols_)}) VALUES ({','.join('?' * len(cols_))})"
                with connect() as conn:
                    if "id" in cols_:
                        # id 가 있는 표는 다른 표가 참조하므로 기존 행은 갱신, 새 행(id 없음)은 추가, 지운 행은 삭제
                        idx = cols_.index("id")
                        keep_ids = [r[idx] for r in rows_ if r[idx] is not None]
                        gone = f"id NOT IN ({','.join('?' * len(keep_ids))})" if keep_ids else "1=1"
                        if name == "annual_items":
                            conn.execute(f"DELETE FROM annual_checks WHERE item_id IN "
                                         f"(SELECT id FROM annual_items WHERE {gone})", keep_ids)
                        if name == "recurring_vouchers":
                            conn.execute(f"DELETE FROM recurring_checks WHERE recurring_id IN "
                                         f"(SELECT id FROM recurring_vouchers WHERE {gone})", keep_ids)
                        conn.execute(f"DELETE FROM {name} WHERE {gone}", keep_ids)
                        upd = ",".join(f"{c}=excluded.{c}" for c in cols_ if c != "id")
                        conn.executemany(f"{insert} ON CONFLICT(id) DO UPDATE SET {upd}", rows_)
                    else:
                        conn.execute(f"DELETE FROM {name}")
                        conn.executemany(insert, rows_)
                st.toast(f"{label} 저장 완료")
                st.rerun()
            except Exception as e:  # 필수값 누락, 키 중복 등
                st.error(f"저장 실패: {e}")
