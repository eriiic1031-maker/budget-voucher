"""국세청 표준 전자세금계산서 XML 파서.

표준 스키마(urn:kr:or:kec:standard:Tax:...)는 발행 ASP마다 namespace prefix가 달라서
태그의 local-name만 보고 읽는다.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path


class InvoiceParseError(ValueError):
    pass


@dataclass
class InvoiceItem:
    seq: int
    name: str
    description: str
    supply_amount: int
    tax_amount: int
    date: str | None = None


@dataclass
class TaxInvoice:
    approval_no: str
    issue_date: str          # YYYY-MM-DD
    type_code: str
    supplier_bizno: str
    supplier_name: str
    buyer_bizno: str
    buyer_name: str
    supply_amount: int
    tax_amount: int
    total_amount: int
    remark: str = ""
    items: list[InvoiceItem] = field(default_factory=list)

    @property
    def is_modified(self) -> bool:
        """수정(세금)계산서 여부."""
        return self.type_code[:2] in ("02", "04")

    @property
    def is_tax_exempt(self) -> bool:
        """면세 계산서(부가세 없음) 여부."""
        return self.type_code[:2] in ("03", "04")

    def to_dict(self) -> dict:
        return asdict(self)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el: ET.Element | None, name: str) -> ET.Element | None:
    if el is None:
        return None
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _children(el: ET.Element | None, name: str) -> list[ET.Element]:
    if el is None:
        return []
    return [c for c in el if _local(c.tag) == name]


def _find(root: ET.Element, *path: str) -> ET.Element | None:
    cur: ET.Element | None = root
    for p in path:
        cur = _child(cur, p)
    return cur


def _text(el: ET.Element | None, default: str = "") -> str:
    return (el.text or "").strip() if el is not None and el.text else default


def _amount(el: ET.Element | None) -> int:
    t = _text(el, "0").replace(",", "")
    try:
        return int(round(float(t)))
    except ValueError as e:
        raise InvoiceParseError(f"금액 형식 오류: {t!r}") from e


def _date(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())[:8]
    if len(digits) != 8:
        raise InvoiceParseError(f"일자 형식 오류: {raw!r}")
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"


def _bizno(raw: str) -> str:
    return "".join(ch for ch in raw if ch.isdigit())


def parse_xml_bytes(data: bytes) -> TaxInvoice:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise InvoiceParseError(f"XML 파싱 실패: {e}") from e

    if _local(root.tag) != "TaxInvoice":
        # 일부 ASP는 래퍼로 감싸서 보낸다
        found = next((el for el in root.iter() if _local(el.tag) == "TaxInvoice"), None)
        if found is None:
            raise InvoiceParseError("TaxInvoice 요소가 없습니다 (전자세금계산서 XML이 아님)")
        root = found

    doc = _child(root, "TaxInvoiceDocument")
    settle = _child(root, "TaxInvoiceTradeSettlement")
    if doc is None or settle is None:
        raise InvoiceParseError("TaxInvoiceDocument / TaxInvoiceTradeSettlement 누락")

    approval_no = _text(_child(doc, "IssueID")).replace("-", "")
    if not approval_no:
        raise InvoiceParseError("승인번호(IssueID) 누락")

    invoicer = _child(settle, "InvoicerParty")
    invoicee = _child(settle, "InvoiceeParty")
    summ = _child(settle, "SpecifiedMonetarySummation")

    items = []
    for i, li in enumerate(_children(root, "TaxInvoiceTradeLineItem"), start=1):
        seq_txt = _text(_child(li, "SequenceNumeric"))
        purchase = _text(_child(li, "PurchaseExpiryDateTime"))
        items.append(InvoiceItem(
            seq=int(seq_txt) if seq_txt.isdigit() else i,
            name=_text(_child(li, "NameText")),
            description=_text(_child(li, "DescriptionText")),
            supply_amount=_amount(_child(li, "InvoiceAmount")),
            tax_amount=_amount(_find(li, "TotalTax", "CalculatedAmount")),
            date=_date(purchase) if purchase else None,
        ))

    remarks = [_text(r) for r in _children(doc, "DescriptionText") if _text(r)]

    return TaxInvoice(
        approval_no=approval_no,
        issue_date=_date(_text(_child(doc, "IssueDateTime"))),
        type_code=_text(_child(doc, "TypeCode"), "0101"),
        supplier_bizno=_bizno(_text(_child(invoicer, "ID"))),
        supplier_name=_text(_child(invoicer, "NameText")),
        buyer_bizno=_bizno(_text(_child(invoicee, "ID"))),
        buyer_name=_text(_child(invoicee, "NameText")),
        supply_amount=_amount(_child(summ, "ChargeTotalAmount")),
        tax_amount=_amount(_child(summ, "TaxTotalAmount")),
        total_amount=_amount(_child(summ, "GrandTotalAmount")),
        remark=" / ".join(remarks),
        items=items,
    )


def parse_xml_file(path: Path) -> TaxInvoice:
    return parse_xml_bytes(Path(path).read_bytes())
