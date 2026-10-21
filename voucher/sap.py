"""SAP 전표 생성 (BAPI_ACC_DOCUMENT_POST).

분개
  (차) 비용계정 x N (코스트센터/사용자별)      ACCOUNTGL
  (차) 부가세대급금 (세금코드별 합산)           ACCOUNTTAX
      (대) 공급업체 (미지급금/외상매입금)        ACCOUNTPAYABLE

수정세금계산서(마이너스 금액)는 부호가 반대로 들어가고 전표유형이 대변메모(KG)가 된다.

SAP_MODE=mock  : SAP 없이 로컬에서 전표번호를 발번해 흐름을 검증
SAP_MODE=rfc   : pyrfc 로 실제 BAPI 호출 (SAP NW RFC SDK + pyrfc 설치 필요, .env 에 접속정보)
"""
from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from .classifier import Proposal
from .config import settings
from .parser import TaxInvoice


class SapPostingError(RuntimeError):
    pass


@dataclass
class PostResult:
    doc_no: str
    fiscal_year: str
    company_code: str
    messages: list[str]


def build_bapi_payload(inv: TaxInvoice, proposal: Proposal, posting_date: date | None = None) -> dict:
    is_credit = inv.total_amount < 0
    pstng = (posting_date or date.fromisoformat(inv.issue_date)).strftime("%Y%m%d")
    doc_date = inv.issue_date.replace("-", "")

    header = {
        "BUS_ACT": "RFBU",
        "USERNAME": settings.sap_user,
        "COMP_CODE": settings.company_code,
        "DOC_DATE": doc_date,
        "PSTNG_DATE": pstng,
        "DOC_TYPE": settings.doc_type_credit if is_credit else settings.doc_type_invoice,
        "REF_DOC_NO": inv.approval_no[-16:],          # 참조필드 16자 제한 → 승인번호 뒤 16자리
        "HEADER_TXT": f"전자세금계산서 {inv.approval_no}"[:25],
    }

    gl_items, tax_items, amounts = [], [], []
    item_no = 1

    # 대변: 공급업체
    payable = [{
        "ITEMNO_ACC": f"{item_no:010d}",
        "VENDOR_NO": proposal.sap_vendor,
        "COMP_CODE": settings.company_code,
        "ITEM_TEXT": f"{inv.supplier_name}"[:50],
        "ALLOC_NMBR": inv.approval_no[-18:],
    }]
    amounts.append({"ITEMNO_ACC": f"{item_no:010d}", "CURRENCY": settings.currency,
                    "AMT_DOCCUR": -inv.total_amount})

    # 차변: 비용
    for line in proposal.lines:
        item_no += 1
        gl_items.append({
            "ITEMNO_ACC": f"{item_no:010d}",
            "GL_ACCOUNT": line.gl_account,
            "COSTCENTER": line.cost_center,
            "TAX_CODE": line.tax_code,
            "ITEM_TEXT": line.text[:50],
            "ALLOC_NMBR": (line.user_id or "")[:18],     # 사용자(사번)는 지정필드에 기록
        })
        amounts.append({"ITEMNO_ACC": f"{item_no:010d}", "CURRENCY": settings.currency,
                        "AMT_DOCCUR": line.amount})

    # 차변: 부가세대급금 (세금코드별 합산)
    tax_by_code: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for line in proposal.lines:
        if line.tax_amount:
            tax_by_code[line.tax_code][0] += line.tax_amount
            tax_by_code[line.tax_code][1] += line.amount
    for code, (tax, base) in tax_by_code.items():
        item_no += 1
        tax_items.append({
            "ITEMNO_ACC": f"{item_no:010d}",
            "GL_ACCOUNT": settings.vat_input_gl,
            "TAX_CODE": code,
            "ACCT_KEY": "VST",
        })
        amounts.append({"ITEMNO_ACC": f"{item_no:010d}", "CURRENCY": settings.currency,
                        "AMT_DOCCUR": tax, "AMT_BASE": base})

    if sum(a["AMT_DOCCUR"] for a in amounts) != 0:
        raise SapPostingError("차대 불일치: 전표를 만들 수 없습니다")

    return {
        "DOCUMENTHEADER": header,
        "ACCOUNTPAYABLE": payable,
        "ACCOUNTGL": gl_items,
        "ACCOUNTTAX": tax_items,
        "CURRENCYAMOUNT": amounts,
    }


class MockSapClient:
    """SAP 없이 동작 확인용. 전표번호를 51xxxxxxxx 로 발번한다."""

    def __init__(self, conn):
        self.conn = conn

    def post(self, payload: dict) -> PostResult:
        year = payload["DOCUMENTHEADER"]["PSTNG_DATE"][:4]
        n = self.conn.execute("SELECT COUNT(*) FROM postings WHERE fiscal_year = ?", (year,)).fetchone()[0]
        return PostResult(doc_no=f"51{n + 1:08d}", fiscal_year=year,
                          company_code=payload["DOCUMENTHEADER"]["COMP_CODE"],
                          messages=["MOCK: 전표가 생성되었습니다"])


class RfcSapClient:
    """pyrfc 를 통해 BAPI_ACC_DOCUMENT_CHECK → POST → BAPI_TRANSACTION_COMMIT."""

    def __init__(self):
        try:
            from pyrfc import Connection  # type: ignore
        except ImportError as e:
            raise SapPostingError("pyrfc 가 설치되어 있지 않습니다 (SAP NW RFC SDK 필요)") from e
        self._conn = Connection(
            ashost=os.environ["SAP_ASHOST"], sysnr=os.environ["SAP_SYSNR"],
            client=os.environ["SAP_CLIENT"], user=os.environ["SAP_RFC_USER"],
            passwd=os.environ["SAP_RFC_PASSWORD"], lang=os.environ.get("SAP_LANG", "KO"),
        )

    @staticmethod
    def _errors(ret: list[dict]) -> list[str]:
        return [f"{r['ID']}{r['NUMBER']} {r['MESSAGE']}" for r in ret if r.get("TYPE") in ("E", "A")]

    def post(self, payload: dict) -> PostResult:
        check = self._conn.call("BAPI_ACC_DOCUMENT_CHECK", **payload)
        if errs := self._errors(check["RETURN"]):
            raise SapPostingError("; ".join(errs))
        res = self._conn.call("BAPI_ACC_DOCUMENT_POST", **payload)
        if errs := self._errors(res["RETURN"]):
            self._conn.call("BAPI_TRANSACTION_ROLLBACK")
            raise SapPostingError("; ".join(errs))
        self._conn.call("BAPI_TRANSACTION_COMMIT", WAIT="X")
        obj_key = res["OBJ_KEY"]  # BELNR(10) + BUKRS(4) + GJAHR(4)
        return PostResult(doc_no=obj_key[:10], company_code=obj_key[10:14], fiscal_year=obj_key[14:18],
                          messages=[r["MESSAGE"] for r in res["RETURN"]])


def get_client(conn):
    if settings.sap_mode == "rfc":
        return RfcSapClient()
    return MockSapClient(conn)
