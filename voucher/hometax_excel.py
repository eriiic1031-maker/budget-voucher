"""[무료] 홈택스에서 직접 내려받은 매입 전자세금계산서 목록(엑셀) 가져오기.

홈택스 > 전자(세금)계산서 목록조회 > 매입 선택 > 조회 > '엑셀내려받기' 한 파일을 그대로 올리면 된다.
홈택스 화면 개편으로 컬럼 순서·제목줄 수가 바뀔 수 있어서, 컬럼 '이름'으로 찾는다.
  - 위쪽 제목/조건 줄은 건너뛰고 '승인번호'가 있는 줄을 헤더로 인식
  - 2단 헤더(예: [공급자] 아래 [등록번호][상호])도 합쳐서 인식
  - .xlsx / .xls / .csv, 그리고 확장자만 .xls 인 HTML 표도 처리

엑셀에는 품목이 대표 1건만 있는 경우가 많아 품목명 + 합계 금액으로 1줄짜리 세금계산서를 만든다.
(여러 품목을 계정별로 나눠야 하는 건은 검토 화면에서 행을 추가해 나누면 된다)
"""
from __future__ import annotations

import io
import re
from datetime import date

import pandas as pd

from .hometax import HometaxError, HometaxRecord
from .parser import InvoiceItem, TaxInvoice

# 필드: 후보 컬럼명 (공백 제거 후 비교). '~' 로 시작하면 '포함' 매칭, (이름, n) 은 n번째 출현
FIELDS: dict[str, list] = {
    "approval_no": ["승인번호", "국세청승인번호"],
    "write_date": ["작성일자", "작성일"],
    "issue_date": ["발급일자", "발행일자"],
    "supplier_bizno": ["공급자사업자등록번호", "공급자등록번호", ("~등록번호", 0)],
    "supplier_name": ["공급자상호", "공급자상호명", ("상호", 0)],
    "buyer_bizno": ["공급받는자사업자등록번호", "공급받는자등록번호", ("~등록번호", 1)],
    "total": ["합계금액", "합계"],
    "supply": ["공급가액", "공급가액합계"],
    "tax": ["세액", "세액합계"],
    "kind": ["전자세금계산서종류", "종류"],
    "category": ["전자세금계산서분류", "분류"],
    "remark": ["비고"],
    "item_name": ["품목명"],
}
REQUIRED = ["approval_no", "write_date", "supplier_bizno", "total"]


class HometaxExcelError(HometaxError):
    pass


def _norm(v) -> str:
    return re.sub(r"\s+", "", str(v)) if v is not None and not (isinstance(v, float) and pd.isna(v)) else ""


def _read_raw(data: bytes, filename: str) -> pd.DataFrame:
    name = filename.lower()
    head = data[:512].lstrip().lower()
    if head.startswith(b"<") and (b"<table" in data[:200_000].lower() or b"<html" in head):
        for enc in ("utf-8", "cp949"):   # .xls 로 저장된 HTML 표 (관공서 엑셀에 흔함)
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise HometaxExcelError("HTML 형식 파일의 인코딩을 읽을 수 없습니다 (UTF-8/CP949)")
        tables = pd.read_html(io.StringIO(text), header=None, thousands=None)
        return max(tables, key=len).astype(object)
    if name.endswith(".csv"):
        for enc in ("utf-8-sig", "cp949"):
            try:
                return pd.read_csv(io.BytesIO(data), header=None, dtype=str, encoding=enc)
            except UnicodeDecodeError:
                continue
        raise HometaxExcelError("CSV 인코딩을 읽을 수 없습니다 (UTF-8/CP949)")
    engine = "xlrd" if name.endswith(".xls") else "openpyxl"
    return pd.read_excel(io.BytesIO(data), header=None, dtype=str, engine=engine)


def _header(raw: pd.DataFrame) -> tuple[int, list[str], list[str]]:
    """(헤더 행 번호, 컬럼명 목록, 상위헤더+컬럼명 목록)"""
    for i in range(min(len(raw), 40)):
        names = [_norm(v) for v in raw.iloc[i].tolist()]
        if any(n in ("승인번호", "국세청승인번호") for n in names):
            above = [_norm(v) for v in raw.iloc[i - 1].tolist()] if i > 0 else [""] * len(names)
            filled, last = [], ""
            for a in above:            # 병합셀 → 앞 값 채우기
                last = a or last
                filled.append(last)
            combined = [f"{a}{n}" if a and a != n else n for a, n in zip(filled, names)]
            return i, names, combined
    raise HometaxExcelError("'승인번호' 컬럼을 찾지 못했습니다. 홈택스 '목록조회'에서 내려받은 파일인지 확인하세요.")


def _locate(names: list[str], combined: list[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    used: set[int] = set()
    for field, cands in FIELDS.items():
        for c in cands:
            if isinstance(c, tuple):
                key, nth = c
                if key.startswith("~"):
                    hits = [j for j, n in enumerate(names) if key[1:] in n]
                else:
                    hits = [j for j, n in enumerate(names) if n == key]
                if len(hits) > nth:
                    found[field] = hits[nth]
                    break
            else:
                hits = [j for j, (n, cb) in enumerate(zip(names, combined)) if c in (n, cb) and j not in used]
                if hits:
                    found[field] = hits[0]
                    break
        if field in found:
            used.add(found[field])
    missing = [f for f in REQUIRED if f not in found]
    if missing:
        raise HometaxExcelError(f"필수 컬럼을 찾지 못했습니다: {missing}. 인식된 컬럼: {[n for n in names if n]}")
    return found


def _text(v) -> str:
    return "" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v).strip()


def _digits(v) -> str:
    return "".join(ch for ch in _norm(v) if ch.isdigit())


def _date(v) -> str | None:
    d = _digits(v)[:8]
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else None


def _amount(v) -> int:
    t = _norm(v).replace(",", "")
    try:
        return int(round(float(t))) if t else 0
    except ValueError:
        return 0


class ExcelHometaxClient:
    """업로드된 홈택스 엑셀을 홈택스 수집 클라이언트처럼 쓴다 (sync/대사 로직 재사용)."""
    name = "hometax_excel"

    def __init__(self, data: bytes, filename: str):
        raw = _read_raw(data, filename)
        hdr, names, combined = _header(raw)
        col = _locate(names, combined)
        self.columns = {f: names[i] for f, i in col.items()}
        self.records: dict[str, HometaxRecord] = {}
        self.invoices: dict[str, TaxInvoice] = {}
        self.skipped = 0

        def get(row, field, default=""):
            return row[col[field]] if field in col else default

        for _, row in raw.iloc[hdr + 1:].iterrows():
            approval = _digits(get(row, "approval_no"))
            wdate = _date(get(row, "write_date"))
            if len(approval) < 20 or not wdate:      # 합계줄·빈줄
                self.skipped += 1
                continue
            total = _amount(get(row, "total"))
            supply = _amount(get(row, "supply")) if "supply" in col else total
            tax = _amount(get(row, "tax")) if "tax" in col else total - supply
            kind, category = _norm(get(row, "kind")), _norm(get(row, "category"))
            modified = "수정" in kind
            exempt = "계산서" in category and "세금" not in category
            type_code = ("04" if modified else "03") if exempt else ("02" if modified else "01")
            item = _text(get(row, "item_name")) or _text(get(row, "remark")) or "홈택스 엑셀"
            supplier_name = _text(get(row, "supplier_name"))
            bizno = _digits(get(row, "supplier_bizno"))

            self.records[approval] = HometaxRecord(
                approval_no=approval, write_date=wdate, issue_date=_date(get(row, "issue_date")),
                supplier_bizno=bizno, supplier_name=supplier_name, supply_amount=supply, tax_amount=tax,
                total_amount=total, modify_yn=modified, item_name=item,
            )
            self.invoices[approval] = TaxInvoice(
                approval_no=approval, issue_date=wdate, type_code=type_code + "01",
                supplier_bizno=bizno, supplier_name=supplier_name,
                buyer_bizno=_digits(get(row, "buyer_bizno")), buyer_name="",
                supply_amount=supply, tax_amount=tax, total_amount=total,
                remark=_text(get(row, "remark")),
                items=[InvoiceItem(1, item, "", supply, tax, wdate)],
            )
        if not self.records:
            raise HometaxExcelError("가져올 세금계산서 행이 없습니다.")

    @property
    def period(self) -> tuple[date, date]:
        ds = sorted(r.write_date for r in self.records.values())
        return date.fromisoformat(ds[0]), date.fromisoformat(ds[-1])

    def list_purchases(self, start: date, end: date) -> list[HometaxRecord]:
        return [r for r in self.records.values() if start.isoformat() <= r.write_date <= end.isoformat()]

    def get_invoice(self, approval_no: str) -> TaxInvoice:
        return self.invoices[approval_no]
