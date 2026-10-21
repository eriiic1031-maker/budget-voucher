"""예산 및 전표관리 - 예시 데이터 (총무 관점).

- 2025-01 ~ 2026-10(21일까지) 세금계산서 월 70~80건 · 2025년 비용 합계 약 120억원
- 정기 전표 17종(임차료·관리비·전기·가스·통신·렌탈·리스·식당·용역·주차·SaaS) + 비정기 약 60건/월
- 2026년 총무 예산 = 2025년 같은 달 실적 × 1.05, 2025년 예산 = 2026년 예산 ÷ 1.05
- 연간 항목 10종 (재산세 7·9월, 자동차세 6·12월, 주민세 8월, 보험 갱신, 법정 점검 등) — 2025년~2026-09 처리 완료
- 작년보다 늘어난 비용 예시: 사내 피트니스 제휴(2026-03 신규), 보안관제 서비스(2026-05 신규),
  본사 주차 5면 추가(2026-07), 재산세·차량보험 인상, 전기요금 인상, 대회의실 리모델링
- 2026년 10월에는 예산 경고가 보이도록 일부러 넣은 상황:
    수선비: 대회의실 리모델링 3,800만원 → 예산 초과
    차량유지비: 유가 상승으로 주유 속도 +35%, 엔진 수리 680만원 → 초과 예상
    복리후생비: 창립기념 행사 1,500만원 (작년 900만원) → 초과 예상
    수도광열비: 전기요금 인상(+9%) → 초과 예상
    렌탈료: 복합기 초과 출력 요금 → 정기 전표 금액 상이
    본사 임차료: 메일 미수신(홈택스에만 있음) → 기한 경과

모든 업체명·사업자번호·인명은 가상의 값이다.
    python -m voucher.seed

화면 보기용 사용자 (샘플은 로그인 없음 · 사이드바 '화면 보기'에서 전환)
    관리자  admin  (총무 담당자, 총무팀)
    사용자  E1023  (홍길동, 영업1팀)
    사용자  E2045  (김철수, 물류팀)
"""
from __future__ import annotations

import calendar
import json
import random
from datetime import date, datetime

import pandas as pd

from .auth import upsert_user
from .classifier import Proposal
from .config import BASE_DIR, settings
from .db import connect, init_db, reset_db, set_meta
from .pipeline import ingest_xml, post_invoice, submit_invoice
from .recurring import mark_checked, recurring_map

NS = "urn:kr:or:kec:standard:Tax:ReusableAggregateBusinessInformationEntitySchemaModule:1:0"
SEED_VERSION = "4"   # 예시 데이터 구성이 바뀌면 올린다 → 앱이 처음 열릴 때 예시 데이터를 다시 만든다
TODAY = date(2026, 10, 21)

DEMO_USERS = [
    # user_id, name, role, cost_center, password
    ("admin", "총무 담당자", "admin", "K1200", "demo-admin-2026"),
    ("E1023", "홍길동", "user", "K2100", "demo-user-2026"),
    ("E2045", "김철수", "user", "K3100", "demo-user-2026"),
]

COST_CENTERS = [
    ("K1100", "경영지원팀"), ("K1200", "총무팀"), ("K2100", "영업1팀"), ("K2200", "영업2팀"), ("K3100", "물류팀"),
    ("K4100", "부산지점"), ("K4200", "대전지점"), ("K5100", "연구소"),
]

GL_ACCOUNTS = [
    # code, name, 예산 주관
    ("81900000", "지급임차료", "총무"), ("81910000", "건물관리비", "총무"), ("81300000", "수도광열비", "총무"),
    ("81400000", "통신비", "총무"), ("81920000", "렌탈료", "총무"), ("82210000", "차량리스료", "총무"),
    ("82200000", "차량유지비", "총무"), ("81100000", "복리후생비", "총무"), ("83200000", "용역비", "총무"),
    ("83000000", "소모품비", "총무"), ("82000000", "수선비", "총무"),
    ("81700000", "세금과공과", "총무"), ("82100000", "보험료", "총무"),
    ("82400000", "운반비", "물류팀"), ("83100000", "지급수수료", "IT팀"), ("83300000", "광고선전비", "마케팅팀"),
    ("83400000", "교육훈련비", "인사팀"), ("11350000", "부가세대급금", "회계"),
]

V = {  # key: (bizno, sap_vendor, name)
    "hq_rent": ("2208100003", "200003", "다온빌딩(주)"),
    "hq_mgmt": ("2208100013", "200013", "다온빌딩관리(주)"),
    "bs_rent": ("6218100021", "200021", "해운대타워(주)"),
    "dj_rent": ("3058100022", "200022", "둔산프라자(주)"),
    "power": ("1208100031", "200031", "한빛전력(주)"),
    "gas": ("1208100032", "200032", "푸른가스(주)"),
    "tel": ("1048100002", "200002", "누리텔레콤(주)"),
    "copier": ("1358100004", "200004", "마루오피스렌탈"),
    "water": ("1418100005", "200005", "맑은물렌탈(주)"),
    "lease": ("1168100001", "200001", "한빛캐피탈(주)"),
    "food": ("2148100011", "200011", "푸른식탁(주)"),
    "guard": ("1158100041", "200041", "든든시큐리티(주)"),
    "clean": ("1158100042", "200042", "반짝클린(주)"),
    "facility": ("1158100043", "200043", "한결시설관리(주)"),
    "parking": ("1208100088", "200088", "바른주차(주)"),
    "saas": ("1078100012", "200012", "구름소프트(주)"),
    "fuel1": ("3128100006", "200006", "새솔주유소"),
    "fuel2": ("6058100077", "200077", "한길주유소"),
    "carfix": ("4108100051", "200051", "바른카센터(주)"),
    "stationery": ("2118100099", "200099", "온누리문구"),
    "hygiene": ("2118100061", "200061", "깨끗한위생(주)"),
    "print": ("1018100092", "200092", "바른인쇄(주)"),
    "repair": ("1268100071", "200071", "튼튼설비(주)"),
    "snack": ("1138100081", "200081", "달콤다과(주)"),
    "courier": ("1248100010", "200010", "한결로지스(주)"),
    "ad": ("1018100091", "200091", "빛나애드(주)"),
    "edu": ("2208100095", "200095", "배움에듀(주)"),
    "fitness": ("1198100055", "200055", "튼튼헬스(주)"),     # 2026-03 신규 계약
    "secmon": ("1198100056", "200056", "세이프넷(주)"),      # 2026-05 신규 계약
}
VENDORS = [v for v in V.values()]

RULES = [
    # priority, vendor key | None, keyword, gl, cost_center, alloc_key, non_deductible, auto_post, memo
    (10, "hq_rent", None, "81900000", None, "OFFICE_RENT", 0, 1, "본사 임차료 - 면적 배부"),
    (10, "hq_mgmt", None, "81910000", None, "OFFICE_RENT", 0, 1, "본사 관리비 - 면적 배부"),
    (10, "bs_rent", None, "81900000", "K4100", None, 0, 1, "부산지점 임차료"),
    (10, "dj_rent", None, "81900000", "K4200", None, 0, 1, "대전지점 임차료"),
    (10, "power", None, "81300000", None, "OFFICE_RENT", 0, 1, "전기요금 - 면적 배부"),
    (10, "gas", None, "81300000", None, "OFFICE_RENT", 0, 1, "가스요금 - 면적 배부"),
    (10, "tel", None, "81400000", None, "OFFICE_COMMON", 0, 1, "통신비 - 인원 배부"),
    (10, "copier", None, "81920000", None, "OFFICE_COMMON", 0, 0, "복합기 렌탈 - 금액 변동 잦아 검토"),
    (10, "water", None, "81920000", None, "OFFICE_COMMON", 0, 1, "정수기·공기청정기 렌탈"),
    (10, "lease", None, "82210000", None, None, 0, 1, "차량리스료 - 차량번호로 귀속"),
    (10, "food", None, "81100000", None, "HEADCOUNT", 0, 1, "구내식당 위탁 - 본사 인원 배부"),
    (10, "guard", None, "83200000", None, "OFFICE_RENT", 0, 1, "경비 용역"),
    (10, "clean", None, "83200000", None, "OFFICE_RENT", 0, 1, "청소 용역"),
    (10, "facility", None, "83200000", None, "OFFICE_RENT", 0, 1, "시설관리 용역"),
    (10, "parking", None, "82200000", "K1200", None, 0, 1, "본사 월 정기주차"),
    (10, "saas", None, "83100000", None, "OFFICE_COMMON", 0, 1, "업무 SaaS (IT팀 예산)"),
    (10, "fuel1", None, "82200000", None, None, 0, 1, "유류비 - 차량번호로 귀속"),
    (10, "fuel2", None, "82200000", None, None, 0, 1, "유류비 - 차량번호로 귀속"),
    (10, "carfix", None, "82200000", None, None, 0, 1, "차량 정비 - 차량번호로 귀속"),
    (10, "stationery", None, "83000000", None, "OFFICE_COMMON", 0, 1, "사무용품"),
    (10, "hygiene", None, "83000000", None, "OFFICE_RENT", 0, 1, "위생·소모품"),
    (10, "print", None, "83000000", "K1200", None, 0, 1, "명함·인쇄"),
    (10, "repair", None, "82000000", "K1200", None, 0, 0, "시설 수선 - 금액 커서 검토"),
    (10, "snack", None, "81100000", None, "HEADCOUNT", 0, 1, "사내 간식·행사"),
    (10, "courier", None, "82400000", "K3100", None, 0, 1, "택배·운송 (물류팀 예산)"),
    (10, "ad", None, "83300000", "K2100", None, 0, 1, "광고 (마케팅 예산)"),
    (10, "edu", None, "83400000", "K1100", None, 0, 1, "교육 (인사 예산)"),
    (10, "fitness", None, "81100000", None, "HEADCOUNT", 0, 1, "사내 피트니스 제휴 (2026-03~)"),
    (10, "secmon", None, "83200000", None, "OFFICE_RENT", 0, 1, "보안관제 서비스 (2026-05~)"),
    (50, None, "주유", "82200000", None, None, 0, 0, "미등록 주유소 - 검토"),
]

ALLOC = [
    *[("OFFICE_RENT", cc, None, r) for cc, r in
      [("K1100", 15), ("K1200", 10), ("K2100", 25), ("K2200", 25), ("K3100", 10), ("K5100", 15)]],
    *[("OFFICE_COMMON", cc, None, r) for cc, r in
      [("K1100", 12), ("K1200", 8), ("K2100", 30), ("K2200", 28), ("K3100", 25), ("K4100", 15), ("K4200", 10),
       ("K5100", 22)]],
    *[("HEADCOUNT", cc, None, r) for cc, r in
      [("K1100", 12), ("K1200", 8), ("K2100", 30), ("K2200", 28), ("K3100", 25), ("K5100", 22)]],
]

VEHICLES = [
    # plate, cost_center, user_id, user_name, non_deductible, 월 리스료(공급가)
    ("12가3456", "K2100", "E1023", "홍길동", 1, 1_350_000),
    ("80바1234", "K3100", "E2045", "김철수", 0, 980_000),     # 화물 → 공제
    ("23나4567", "K2100", "E1031", "이하늘", 1, 1_250_000),
    ("34다5678", "K2200", "E1102", "박민수", 1, 1_420_000),
    ("45라6789", "K2200", "E1108", "최지현", 1, 1_180_000),
    ("56마7890", "K4100", "E3011", "정우진", 1, 1_100_000),
    ("67거1357", "K4200", "E3105", "강도윤", 1, 1_050_000),
    ("78너2468", "K1100", "E1201", "윤서진", 1, 1_550_000),
    ("81사3579", "K3100", "E2051", "임준호", 0, 920_000),     # 화물
    ("90아4680", "K5100", "E5012", "송다은", 1, 1_300_000),
    ("11자5791", "K1200", "E1255", "배현우", 1, 1_200_000),
    ("22차6802", "K2100", "E1044", "조은비", 1, 1_280_000),
]

# 정기 전표: name, vendor, keyword, 공급가 기준액(2025), 허용오차%, 기한일, 담당, gl, 2026 인상률, 시작월
RECURRING = [
    ("본사 임차료", "hq_rent", None, 320_000_000, 0, 5, "총무팀", "81900000", 1.03),
    ("본사 관리비", "hq_mgmt", None, 85_000_000, 5, 5, "총무팀", "81910000", 1.03),
    ("부산지점 임차료", "bs_rent", None, 45_000_000, 0, 5, "총무팀", "81900000", 1.03),
    ("대전지점 임차료", "dj_rent", None, 30_000_000, 0, 5, "총무팀", "81900000", 1.03),
    ("차량 리스료", "lease", None, None, 0, 10, "총무팀", "82210000", 1.0),
    ("본사 정기주차", "parking", None, 6_000_000, 30, 12, "총무팀", "82200000", 1.0),   # 2026-07 5면 추가
    ("복합기 렌탈료", "copier", None, 9_000_000, 10, 12, "총무팀", "81920000", 1.03),
    ("전기요금", "power", None, 38_000_000, 40, 15, "총무팀", "81300000", 1.09),
    ("인터넷·전화 요금", "tel", "인터넷", 18_000_000, 10, 15, "총무팀", "81400000", 1.03),
    ("가스요금", "gas", None, 6_000_000, 60, 18, "총무팀", "81300000", 1.05),
    ("휴대폰 요금", "tel", "휴대폰", 12_000_000, 10, 18, "총무팀", "81400000", 1.03),
    ("정수기·공기청정기 렌탈료", "water", None, 4_300_000, 5, 22, "총무팀", "81920000", 1.03),
    ("경비 용역", "guard", None, 42_000_000, 0, 28, "총무팀", "83200000", 1.03),
    ("청소 용역", "clean", None, 35_000_000, 0, 28, "총무팀", "83200000", 1.03),
    ("시설관리 용역", "facility", None, 28_000_000, 0, 28, "총무팀", "83200000", 1.03),
    ("구내식당 위탁", "food", None, 160_000_000, 10, 30, "총무팀", "81100000", 1.03),
    ("업무 SaaS 사용료", "saas", None, 9_900_000, 5, 30, "IT팀", "83100000", 1.0),
    ("사내 피트니스 제휴", "fitness", None, 3_000_000, 0, 15, "총무팀", "81100000", 1.0, "2026-03"),
    ("보안관제 서비스", "secmon", None, 1_800_000, 0, 28, "총무팀", "83200000", 1.0, "2026-05"),
]
RECURRING = [r if len(r) == 10 else (*r, None) for r in RECURRING]

# 연간 항목: 이름, 처리월, 기한일, gl, 2025 금액, 2026 금액, 분류, 메모
ANNUAL = [
    ("법인차량 보험 갱신", "1", 20, "82100000", 14_400_000, 15_840_000, "보험", "12대 일괄 갱신"),
    ("화재보험 갱신", "3", 31, "82100000", 9_800_000, 10_290_000, "보험", "본사·지점 건물"),
    ("소방시설 종합정밀점검", "4", 30, "83200000", 3_300_000, 3_520_000, "점검", "연 1회 법정 점검"),
    ("자동차세", "6,12", 30, "81700000", 1_450_000, 1_450_000, "세금", "법인차량 12대 · 1월 연납 시 할인"),
    ("재산세(건물분)", "7", 31, "81700000", 18_500_000, 19_610_000, "세금", "7월 16~31일 납부"),
    ("주민세(사업소분)", "8", 31, "81700000", 2_800_000, 2_940_000, "세금", "8월 1~31일 신고·납부"),
    ("재산세(토지분)", "9", 30, "81700000", 12_300_000, 13_160_000, "세금", "9월 16~30일 납부"),
    ("정기 소독", "3,6,9,12", 25, "83200000", 880_000, 920_000, "점검", "분기 1회 방역"),
    ("전기 정기 안전점검", "10", 31, "83200000", 1_500_000, 1_650_000, "점검", "연 1회 · 정전 일정 공지 필요"),
    ("승강기 정기검사", "11", 30, "83200000", 650_000, 680_000, "점검", "연 1회"),
]
# 정기 전표 발행일(공급자가 보내는 날)
ISSUE_DAY = {"fitness": 14, "secmon": 24, "hq_rent": 1, "hq_mgmt": 3, "bs_rent": 1, "dj_rent": 1, "lease": 5, "parking": 9, "copier": 10,
             "power": 12, "tel_인터넷": 12, "gas": 14, "tel_휴대폰": 15, "water": 20, "guard": 25, "clean": 25,
             "facility": 25, "food": 27, "saas": 28}
POWER_SEASON = {1: 1.15, 2: 1.1, 3: 0.95, 4: 0.85, 5: 0.85, 6: 1.0, 7: 1.35, 8: 1.45, 9: 1.05, 10: 0.9,
                11: 0.95, 12: 1.1}
GAS_SEASON = {1: 2.2, 2: 1.9, 3: 1.4, 4: 0.9, 5: 0.5, 6: 0.3, 7: 0.25, 8: 0.25, 9: 0.35, 10: 0.7, 11: 1.3, 12: 2.0}
LEASE_TOTAL = sum(v[5] for v in VEHICLES)


def build_xml(approval_no: str, issue: date, supplier: tuple[str, str], items: list[tuple[str, str, int]],
              type_code: str = "0101", buyer_bizno: str | None = None, remark: str = "") -> bytes:
    """items: (품목명, 비고, 공급가액). 세액은 10%로 계산."""
    supply = sum(i[2] for i in items)
    tax = sum(int(i[2] * 0.1) for i in items)
    d = issue.strftime("%Y%m%d")
    lines = "".join(
        f"""
  <TaxInvoiceTradeLineItem>
    <SequenceNumeric>{n}</SequenceNumeric>
    <PurchaseExpiryDateTime>{d}</PurchaseExpiryDateTime>
    <NameText>{name}</NameText>
    <DescriptionText>{desc}</DescriptionText>
    <InvoiceAmount>{amt}</InvoiceAmount>
    <TotalTax><CalculatedAmount>{int(amt * 0.1)}</CalculatedAmount></TotalTax>
  </TaxInvoiceTradeLineItem>"""
        for n, (name, desc, amt) in enumerate(items, start=1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<TaxInvoice xmlns="{NS}">
  <ExchangedDocument><IssueDateTime>{d}120000</IssueDateTime></ExchangedDocument>
  <TaxInvoiceDocument>
    <IssueID>{approval_no}</IssueID>
    <TypeCode>{type_code}</TypeCode>
    <DescriptionText>{remark}</DescriptionText>
    <IssueDateTime>{d}</IssueDateTime>
    <PurposeCode>02</PurposeCode>
  </TaxInvoiceDocument>
  <TaxInvoiceTradeSettlement>
    <InvoicerParty><ID>{supplier[0]}</ID><NameText>{supplier[1]}</NameText></InvoicerParty>
    <InvoiceeParty><ID>{buyer_bizno or settings.company_bizno}</ID><NameText>(주)데모회사</NameText></InvoiceeParty>
    <SpecifiedMonetarySummation>
      <ChargeTotalAmount>{supply}</ChargeTotalAmount>
      <TaxTotalAmount>{tax}</TaxTotalAmount>
      <GrandTotalAmount>{supply + tax}</GrandTotalAmount>
    </SpecifiedMonetarySummation>
  </TaxInvoiceTradeSettlement>{lines}
</TaxInvoice>""".encode("utf-8")


_seq = iter(range(1, 10**8))


def _approval(d: date, rnd: random.Random | None = None) -> str:
    return f"{d:%Y%m%d}41000012{next(_seq):08d}"


def _r(rnd: random.Random, lo: int, hi: int, unit: int = 10_000) -> int:
    return rnd.randrange(lo // unit, hi // unit + 1) * unit


# ---------------------------------------------------------------- 생성기 ----
def month_invoices(y: int, m: int, rnd: random.Random, cutoff: int | None = None) -> list[dict]:
    """한 달치 세금계산서 목록. dict(vendor, date, items, kind, ...). cutoff 일 이후 발행분은 만들지 않음."""
    last = calendar.monthrange(y, m)[1]
    out: list[dict] = []
    k26 = y >= 2026

    def add(vendor, day, items, **kw):
        day = min(day, last)
        if cutoff is None or day <= cutoff:
            out.append({"vendor": vendor, "date": date(y, m, day), "items": items, **kw})

    mm = f"{m}월분"
    # ---- 정기 ----
    for name, vk, kw, base, _tol, _due, _own, _gl, rise, start in RECURRING:
        if start and f"{y}-{m:02d}" < start:
            continue
        f = rise if k26 else 1.0
        key = f"{vk}_{kw}" if kw else vk
        day = ISSUE_DAY[key]
        if vk == "lease":
            items = [(f"차량 리스료 {p[:-4]} {p[-4:]}", mm, p_amt) for p, *_rest, p_amt in VEHICLES]
            add(vk, day, items)
            continue
        amt = round(base * f, -4)
        if vk == "parking":   # 2025~2026-06 은 15면(450만원), 2026-07부터 20면(600만원)
            amt = 6_000_000 if (y, m) >= (2026, 7) else 4_500_000
        if vk == "power":
            amt *= POWER_SEASON[m] * rnd.uniform(0.96, 1.04)
        elif vk == "gas":
            amt *= GAS_SEASON[m] * rnd.uniform(0.93, 1.07)
        elif vk == "food":
            amt *= (0.9 if m in (2, 9, 10) else 1.0) * rnd.uniform(0.96, 1.04)
        elif vk in ("tel", "copier"):
            amt *= rnd.uniform(0.95, 1.05)
        label = {"tel": f"{kw} 요금"}.get(vk, name)
        add(vk, day, [(label, mm, round(amt, -4))])

    # ---- 비정기 ----
    fuel_k = 1.35 if (y, m) == (2026, 10) else (1.04 if k26 else 1.0)
    for _ in range(22):
        p = rnd.choice(VEHICLES)
        oil = "경유" if p[4] == 0 else "휘발유"
        add(rnd.choice(["fuel1", "fuel1", "fuel2"]), rnd.randint(1, 28),
            [(f"{oil} {p[0]}", "", round(_r(rnd, 300_000, 1_300_000) * fuel_k, -3))])
    for _ in range(3):
        p = rnd.choice(VEHICLES)
        add("carfix", rnd.randint(2, 27), [(f"정기 점검·소모품 교환 {p[0]}", "", _r(rnd, 300_000, 3_500_000))])
    for _ in range(8):
        add("stationery", rnd.randint(1, 28), [(rnd.choice(["A4 용지 외", "사무용품", "토너·잉크", "파일·바인더"]), "",
                                                _r(rnd, 300_000, 2_500_000))])
    for _ in range(3):
        add("hygiene", rnd.randint(1, 28), [("화장지·위생용품", "", _r(rnd, 1_000_000, 3_500_000))])
    for _ in range(4):
        add("print", rnd.randint(1, 28), [(rnd.choice(["명함 인쇄", "사내 양식 인쇄", "현수막"]), "",
                                           _r(rnd, 200_000, 1_800_000))])
    for _ in range(2):
        add("repair", rnd.randint(3, 26), [(rnd.choice(["공조기 필터 교체", "누수 보수", "출입문 수리", "조명 교체"]),
                                            "", _r(rnd, 2_000_000, 9_000_000))])
    for _ in range(6):
        add("snack", rnd.randint(1, 28), [(rnd.choice(["사내 간식", "회의 다과", "탕비실 음료"]), "",
                                           _r(rnd, 500_000, 3_000_000))])
    for _ in range(8):
        add("courier", rnd.randint(1, 28), [("택배 운송료", "", _r(rnd, 2_000_000, 6_000_000))])
    for _ in range(2):
        add("ad", rnd.randint(1, 28), [(rnd.choice(["온라인 광고", "전시회 부스"]), "", _r(rnd, 5_000_000, 30_000_000))])
    for _ in range(2):
        add("edu", rnd.randint(1, 28), [(rnd.choice(["직무 교육", "리더십 교육"]), "", _r(rnd, 2_000_000, 8_000_000))])

    # ---- 계절·연례 행사 ----
    if m == 10:   # 창립기념일
        add("snack", 20, [("창립기념 행사 케이터링", "", 15_000_000 if k26 else 9_000_000)])
    if m == 12:
        add("snack", 19, [("송년회 케이터링", "", 25_000_000)])
    if m == 6:
        add("repair", 14, [("하계 공조기 점검", "", 6_500_000)])
    if (y, m) == (2026, 10):  # 이번 달 일부러 넣은 상황
        add("repair", 16, [("대회의실 리모델링", "1차 기성", 38_000_000)])
        add("stationery", 6, [("토너·잉크", "복합기용", 1_800_000)])      # 중복 의심 (같은 금액 8일 뒤)
        add("stationery", 14, [("토너·잉크", "복합기용", 1_800_000)])
        for d in (13, 14, 15):                                            # 분할 의심 (3일 동안 3건, 합계 330만원)
            add("print", d, [("홍보 브로슈어 인쇄", f"{d - 12}차", 1_000_000)])
        add("carfix", 18, [("엔진 수리 34다5678", "", 6_800_000)])
        out = [o for o in out if not (o["vendor"] == "copier")]
        add("copier", 10, [("복합기 렌탈료", mm, 9_270_000), ("초과 출력 요금", mm, 2_150_000)])
    return out


def all_invoices() -> list[dict]:
    rnd = random.Random(2026)
    out = []
    for y, m in [(2025, mm) for mm in range(1, 13)] + [(2026, mm) for mm in range(1, 11)]:
        out += month_invoices(y, m, rnd, cutoff=TODAY.day if (y, m) == (TODAY.year, TODAY.month) else None)
    return out


# 규칙이 없는 거래처 — 들어오면 '과거 비슷한 건'으로 계정·부서를 추천하고, 담당자가 확정(가끔 수정)한다
# (발행일, 사업자번호, 공급자, 품목, 공급가, 담당자가 확정한 계정, 부서)
NO_RULE_HISTORY = [
    ("2025-11-20", "2118100098", "새봄문구", "탁상 달력 제작", 1_700_000, "83000000", "K1200"),
    ("2026-01-14", "1308100301", "모아철물", "공구·철물 소모품", 420_000, "83000000", "K1200"),
    ("2026-02-11", "1308100302", "하늘꽃집", "경조 화환", 300_000, "81100000", "K1100"),
    ("2026-02-25", "1308100301", "모아철물", "철물 소모품(나사·앵커)", 260_000, "83000000", "K1200"),
    ("2026-03-18", "1308100303", "커피로스터리 온", "회의용 원두", 540_000, "81100000", "K1200"),
    ("2026-04-08", "1308100302", "하늘꽃집", "개업 축하 화환", 350_000, "81100000", "K1100"),
    ("2026-04-22", "1308100303", "커피로스터리 온", "탕비실 원두", 610_000, "81100000", "K1200"),
    ("2026-05-12", "1308100301", "모아철물", "공구 소모품", 380_000, "83000000", "K1200"),
    ("2026-05-28", "1308100304", "바른열쇠", "출입문 도어락 교체", 1_250_000, "82000000", "K1200"),   # 첫 거래 → 추천 없음
    ("2026-06-16", "1308100302", "하늘꽃집", "조문 화환", 300_000, "81100000", "K1100"),
    ("2026-07-07", "1308100303", "커피로스터리 온", "회의용 원두", 580_000, "81100000", "K1200"),
    ("2026-07-21", "1308100304", "바른열쇠", "사무실 열쇠 복사", 90_000, "83000000", "K1200"),       # 과거와 달라 수정됨
    ("2026-08-19", "1308100301", "모아철물", "공구·철물 소모품", 450_000, "83000000", "K1200"),
    ("2026-09-08", "1308100302", "하늘꽃집", "경조 화환", 300_000, "81100000", "K1100"),
    ("2026-09-23", "1308100303", "커피로스터리 온", "행사용 원두·컵", 720_000, "81100000", "K2100"),   # 부서가 달라 수정됨
]


def load_no_rule_history(conn) -> None:
    """규칙 없는 거래처 세금계산서 → (추천 기록) → 담당자 확정 전기. 추천 정확도 화면의 예시."""
    for d, bizno, name, item, amt, gl, cc in NO_RULE_HISTORY:
        day = date.fromisoformat(d)
        r = ingest_xml(conn, build_xml(_approval(day), day, (bizno, name), [(item, "", amt)]), "mail")
        row = conn.execute("SELECT proposal_json FROM invoices WHERE id=?", (r.invoice_id,)).fetchone()
        p = Proposal.from_dict(json.loads(row["proposal_json"]))
        p.sap_vendor = "3" + bizno[-5:]
        for line in p.lines:
            line.gl_account, line.cost_center = gl, cc
        post_invoice(conn, r.invoice_id, actor="admin", proposal=p)


def hometax_only_invoices() -> list[tuple[str, bytes]]:
    """홈택스에는 있지만 메일로는 오지 않은 세금계산서 (조회 링크만 발송한 공급자 등)."""
    b, _, n = V["hq_rent"]
    pb, _, pn = V["print"]
    return [
        ("ht_1001_rent.xml", build_xml("202610014100001299000001", date(2026, 10, 1), (b, n),
                                       [("본사 임차료", "10월분", round(320_000_000 * 1.03, -4))])),
        ("ht_1013_print.xml", build_xml("202610134100001299000002", date(2026, 10, 13), (pb, pn),
                                        [("명함 인쇄", "신규 입사자", 480_000)])),
    ]


def write_hometax_excel_sample(xmls: list[bytes], path) -> None:
    """홈택스 '목록조회 > 엑셀내려받기' 와 비슷한 모양의 예시 파일 (제목줄 + 헤더 + 데이터)."""
    from .parser import parse_xml_bytes

    header = ["작성일자", "승인번호", "발급일자", "전송일자", "공급자사업자등록번호", "종사업장번호", "상호", "대표자명",
              "공급받는자사업자등록번호", "종사업장번호", "상호", "대표자명", "합계금액", "공급가액", "세액",
              "전자세금계산서분류", "전자세금계산서종류", "발급유형", "비고", "영수/청구 구분",
              "품목일자", "품목명", "품목규격", "품목수량", "품목단가", "품목공급가액", "품목세액", "품목비고"]
    rows = []
    for x in xmls:
        i = parse_xml_bytes(x)
        it = i.items[0]
        rows.append([i.issue_date, f"{i.approval_no[:8]}-{i.approval_no[8:16]}-{i.approval_no[16:]}", i.issue_date,
                     i.issue_date, f"{i.supplier_bizno[:3]}-{i.supplier_bizno[3:5]}-{i.supplier_bizno[5:]}", "",
                     i.supplier_name, "", "101-81-99999", "", "(주)데모회사", "", i.total_amount, i.supply_amount,
                     i.tax_amount, "세금계산서", "수정" if i.is_modified else "일반", "전자발급", i.remark, "청구",
                     it.date, it.name, "", 1, it.supply_amount, it.supply_amount, it.tax_amount, it.description])
    rows.sort(key=lambda r: r[0])
    title = [["전자(세금)계산서 목록조회 - 매입"], ["조회기간 : 2026-10-01 ~ 2026-10-31 (작성일자 기준)"], []]
    pd.DataFrame(title + [header] + rows).to_excel(path, header=False, index=False)


# ---------------------------------------------------------------- 적재 ----
def load_master(conn) -> None:
    conn.executemany("INSERT OR REPLACE INTO cost_centers VALUES (?,?)", COST_CENTERS)
    conn.executemany("INSERT OR REPLACE INTO gl_accounts(code, name, owner) VALUES (?,?,?)", GL_ACCOUNTS)
    conn.executemany("INSERT OR REPLACE INTO vendors(bizno, sap_vendor, name) VALUES (?,?,?)", VENDORS)
    conn.executemany(
        "INSERT INTO account_rules(priority, supplier_bizno, keyword, gl_account, cost_center, alloc_key,"
        " non_deductible, auto_post, memo) VALUES (?,?,?,?,?,?,?,?,?)",
        [(p, V[vk][0] if vk else None, *rest) for p, vk, *rest in RULES])
    conn.executemany("INSERT INTO allocation_keys(key, cost_center, user_id, ratio) VALUES (?,?,?,?)", ALLOC)
    conn.executemany("INSERT OR REPLACE INTO vehicles VALUES (?,?,?,?,?)", [v[:5] for v in VEHICLES])
    rows = []
    for name, vk, kw, base, tol, due, owner, gl, rise, start in RECURRING:
        supply = LEASE_TOTAL if vk == "lease" else round(base * rise, -4)   # 발행 금액과 같은 방식으로 계산
        total = supply + int(supply * 0.1)
        rows.append((name, V[vk][0], kw, total, tol, due, owner, gl, start))
    conn.executemany(
        "INSERT INTO recurring_vouchers(name, supplier_bizno, keyword, expected_amount, tolerance_pct, due_day,"
        " owner, gl_account, start_month) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.executemany(
        "INSERT INTO annual_items(name, months, due_day, gl_account, expected_amount, category, note)"
        " VALUES (?,?,?,?,?,?,?)",
        [(n, ms, d, gl, a26, cat, note) for n, ms, d, gl, _a25, a26, cat, note in ANNUAL])


def load_annual_history(conn) -> None:
    """연간 항목 처리 이력: 2025년 전부 + 2026년은 기준일 이전 달까지 처리 완료로 체크."""
    from .annual import mark_done
    ids = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM annual_items")}
    for n, ms, d, _gl, a25, a26, _cat, _note in ANNUAL:
        for mm in [int(x) for x in ms.split(",")]:
            for y, amt in ((2025, a25), (2026, a26)):
                if (y, mm) >= (TODAY.year, TODAY.month):
                    continue
                day = min(d, calendar.monthrange(y, mm)[1]) - 3
                mark_done(conn, ids[n], f"{y}-{mm:02d}", amt, f"{y}-{mm:02d}-{day:02d}", "", "admin")


def load_users(conn) -> None:
    for uid, name, role, cc, pw in DEMO_USERS:
        upsert_user(conn, uid, name, role, cc, pw)


def load_budgets(conn, year: int = 2026, uplift: float = 1.05) -> None:
    """올해 예산 = 작년 같은 달 실적 × 1.05 (100만원 단위 반올림). 총무 예산 계정만."""
    from .budget import load_lines, owner_accounts
    ly = load_lines(conn, f"{year - 1}-01-01", f"{year - 1}-12-31", include_pending=False)
    ly["m"] = ly.month.str[5:7].astype(int)
    agg = ly.groupby(["m", "gl"])["amount"].sum()
    rows = [(year, m, gl, int(round(agg.get((m, gl), 0) * uplift, -6)))
            for gl in owner_accounts(conn) for m in range(1, 13)]
    conn.executemany("INSERT OR REPLACE INTO budgets VALUES (?,?,?,?)", rows)
    # 작년 예산: 올해 예산이 작년 예산의 1.05배가 되도록 (100만원 단위)
    conn.executemany("INSERT OR REPLACE INTO budgets VALUES (?,?,?,?)",
                     [(year - 1, m, gl, int(round(amt / uplift, -6))) for _, m, gl, amt in rows])


def _realistic_timestamps(conn) -> None:
    """데모 이력이 모두 '방금'으로 보이지 않게, 처리 시각을 발행일 기준으로 맞춘다."""
    now = datetime(TODAY.year, TODAY.month, TODAY.day, 9, 30).isoformat(timespec="seconds")
    expr = ("min(?, strftime('%Y-%m-%dT%H:%M:%S', i.issue_date, '+1 day', '+9 hours', "
            "'+' || (i.id % 300) || ' minutes'))")
    conn.execute(f"UPDATE postings SET posted_at = (SELECT {expr} FROM invoices i WHERE i.id = postings.invoice_id)",
                 (now,))
    conn.execute(f"""UPDATE audit_log SET at = (SELECT {expr} FROM invoices i WHERE i.id = audit_log.invoice_id)
                     WHERE invoice_id IS NOT NULL AND action IN ('INGEST','CLASSIFY','POST','CHECK')""", (now,))
    conn.execute("UPDATE invoices SET checked_at = (SELECT p.posted_at FROM postings p WHERE p.invoice_id = invoices.id)"
                 " WHERE checked_at IS NOT NULL")


def run(reset: bool = True) -> None:
    if reset:
        reset_db()
    init_db()
    sample_dir = BASE_DIR / "samples"
    sample_dir.mkdir(exist_ok=True)
    for old in sample_dir.glob("*.xml"):
        old.unlink()

    from . import ai
    ai.disable(True)            # 예시 데이터는 빠르고 늘 같게 — 추천은 '과거 비슷한 건' 방식으로
    invs = all_invoices()
    names = {k: (b, n) for k, (b, _, n) in V.items()}
    oct_xmls: list[bytes] = []
    with connect() as conn:
        load_master(conn)
        load_users(conn)
        for inv in invs:
            d = inv["date"]
            if inv["vendor"] == "hq_rent" and (d.year, d.month) == (2026, 10):
                continue   # 메일 미수신 → 홈택스 엑셀로만 확인 가능
            xml = build_xml(_approval(d), d, names[inv["vendor"]], inv["items"])
            r = ingest_xml(conn, xml, "mail")
            recent = (d.year, d.month) == (TODAY.year, TODAY.month)
            if recent:
                oct_xmls.append(xml)
            # 이번 달 19일 이후 건은 아직 '전기 대기'로 남겨 둔다
            if r.invoice_id and r.status in ("NEW_READY", "NEW_REVIEW") and not (recent and d.day >= 19):
                post_invoice(conn, r.invoice_id, actor="auto" if r.status == "NEW_READY" else "admin")

        load_no_rule_history(conn)

        # ---- 이번 달 검토가 필요한 상황 ----
        extra = [
            ("2118100098", "새봄문구", date(2026, 10, 18), [("탁상 달력 제작", "2027년", 1_850_000)]),
            ("6058100066", "동네주유소", date(2026, 10, 19), [("휘발유 12가3456", "지방 출장", 95_000)]),
        ]
        for bizno, nm, d, items in extra:
            xml = build_xml(_approval(d), d, (bizno, nm), items)
            oct_xmls.append(xml)
            ingest_xml(conn, xml, "mail")
        row = conn.execute("SELECT id, proposal_json FROM invoices WHERE supplier_name='동네주유소'").fetchone()
        p = Proposal.from_dict(json.loads(row["proposal_json"]))
        for line in p.lines:
            line.gl_account = "82200000"   # 사용자가 계정을 '차량유지비'로 채움
        submit_invoice(conn, row["id"], p, "E1023", "지방 출장 중 주유했습니다. 처음 가는 주유소라 등록 부탁드려요")

        # ---- 비정기 전표 확인: 과거는 모두 확인, 이번 달은 12일까지만 확인 ----
        rec = recurring_map(conn, "2025-01-01", "2026-12-31")
        ids = [r["id"] for r in conn.execute(
            "SELECT id, issue_date FROM invoices WHERE status='POSTED' AND issue_date <= '2026-10-12'")
            if r["id"] not in rec]
        mark_checked(conn, ids, "admin")

        load_annual_history(conn)
        conn.execute("DELETE FROM audit_log WHERE action = 'ANNUAL_DONE'")   # 과거 이력이 '최근 활동'을 덮지 않게
        load_budgets(conn)
        _realistic_timestamps(conn)

        set_meta(conn, "seed_version", SEED_VERSION)
        n = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        y25 = conn.execute("SELECT SUM(supply_amount) FROM invoices WHERE issue_date LIKE '2025%'").fetchone()[0]

    ai.disable(False)
    write_hometax_excel_sample(oct_xmls + [x for _, x in hometax_only_invoices()],
                               sample_dir / "홈택스_매입목록_예시_202610.xlsx")
    print(f"예시 데이터 생성 완료 · 세금계산서 {n:,}건 · 2025년 공급가액 합계 {y25 / 1e8:,.1f}억원")


if __name__ == "__main__":
    run()
