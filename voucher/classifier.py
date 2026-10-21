"""세금계산서 품목 → 계정/코스트센터/사용자 분류 및 배부.

결정 순서 (품목별)
  1) 계정: account_rules 를 priority 순으로 보고 (공급자, 키워드) 조건을 모두 만족하는 첫 규칙
  2) 귀속: 품목에 차량번호가 있으면 vehicles 마스터 → 규칙의 alloc_key 배부 → 규칙의 cost_center
  3) 하나라도 결정 못 하면 issues 에 사유를 남기고 검토 대기로 보낸다
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass, field

from .config import settings
from .parser import InvoiceItem, TaxInvoice

PLATE_RE = re.compile(r"\d{2,3}\s?[가-힣]\s?\d{4}")


@dataclass
class ProposedLine:
    item_seq: int
    item_name: str
    gl_account: str | None
    cost_center: str | None
    user_id: str | None
    amount: int              # 비용 계정 금액 (불공제면 세액 포함)
    tax_amount: int          # 공제 대상 세액 (불공제/면세면 0)
    tax_code: str
    rule_id: int | None = None
    text: str = ""


@dataclass
class Proposal:
    lines: list[ProposedLine] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    sap_vendor: str | None = None
    auto_post: bool = False

    def to_dict(self) -> dict:
        return {
            "lines": [asdict(l) for l in self.lines],
            "issues": self.issues,
            "sap_vendor": self.sap_vendor,
            "auto_post": self.auto_post,
        }

    @staticmethod
    def from_dict(d: dict) -> "Proposal":
        return Proposal(
            lines=[ProposedLine(**l) for l in d.get("lines", [])],
            issues=list(d.get("issues", [])),
            sap_vendor=d.get("sap_vendor"),
            auto_post=bool(d.get("auto_post")),
        )


def split_amount(total: int, ratios: list[float]) -> list[int]:
    """비율대로 나누고 원 단위 끝수는 마지막 라인에서 맞춘다."""
    s = sum(ratios)
    if s <= 0:
        raise ValueError("배부 비율 합계가 0입니다")
    parts = [int(total * r / s) for r in ratios]
    parts[-1] += total - sum(parts)
    return parts


def find_plate(text: str) -> str | None:
    m = PLATE_RE.search(text)
    return m.group(0).replace(" ", "") if m else None


def _match_rule(conn: sqlite3.Connection, inv: TaxInvoice, item: InvoiceItem) -> sqlite3.Row | None:
    haystack = f"{item.name} {item.description} {inv.remark}"
    rows = conn.execute(
        "SELECT * FROM account_rules WHERE supplier_bizno IS NULL OR supplier_bizno = ? "
        "ORDER BY priority, id",
        (inv.supplier_bizno,),
    ).fetchall()
    for r in rows:
        if r["supplier_bizno"] is None and not r["keyword"]:
            continue  # 조건 없는 규칙은 무시 (모든 건에 걸리는 사고 방지)
        if r["keyword"] and r["keyword"] not in haystack:
            continue
        return r
    return None


def classify(conn: sqlite3.Connection, inv: TaxInvoice) -> Proposal:
    p = Proposal()

    vendor = conn.execute("SELECT sap_vendor FROM vendors WHERE bizno = ?", (inv.supplier_bizno,)).fetchone()
    if vendor:
        p.sap_vendor = vendor["sap_vendor"]
    else:
        p.issues.append(f"SAP 공급업체 미등록: {inv.supplier_name}({inv.supplier_bizno})")

    if inv.buyer_bizno and inv.buyer_bizno != settings.company_bizno:
        p.issues.append(f"공급받는자 사업자번호가 자사와 다름: {inv.buyer_bizno}")

    items = inv.items or [InvoiceItem(1, inv.remark or inv.supplier_name, "", inv.supply_amount, inv.tax_amount)]
    if sum(i.supply_amount for i in items) != inv.supply_amount:
        p.issues.append("품목 공급가액 합계가 세금계산서 공급가액과 다름")
    if sum(i.tax_amount for i in items) != inv.tax_amount:
        p.issues.append("품목 세액 합계가 세금계산서 세액과 다름")

    all_auto = True
    for item in items:
        rule = _match_rule(conn, inv, item)
        if rule is None:
            p.issues.append(f"[{item.seq}] '{item.name}' 계정 규칙 없음")
            all_auto = False
        elif not rule["auto_post"]:
            all_auto = False

        gl = rule["gl_account"] if rule else None
        non_deductible = bool(rule["non_deductible"]) if rule else False

        # 귀속 대상: (코스트센터, 사용자, 비율) 목록
        targets: list[tuple[str | None, str | None, float]] = []
        plate = find_plate(f"{item.name} {item.description} {inv.remark}")
        if plate:
            v = conn.execute("SELECT * FROM vehicles WHERE plate = ?", (plate,)).fetchone()
            if v:
                targets = [(v["cost_center"], v["user_id"], 1.0)]
                non_deductible = non_deductible or bool(v["non_deductible"])
            else:
                p.issues.append(f"[{item.seq}] 차량번호 {plate} 가 차량 마스터에 없음")
                all_auto = False
        if not targets and rule and rule["alloc_key"]:
            keys = conn.execute("SELECT * FROM allocation_keys WHERE key = ? ORDER BY rowid",
                                (rule["alloc_key"],)).fetchall()
            if keys:
                targets = [(k["cost_center"], k["user_id"], k["ratio"]) for k in keys]
            else:
                p.issues.append(f"[{item.seq}] 배부기준 '{rule['alloc_key']}' 이 비어 있음")
                all_auto = False
        if not targets and rule and rule["cost_center"]:
            targets = [(rule["cost_center"], None, 1.0)]
        if not targets:
            if rule:
                p.issues.append(f"[{item.seq}] '{item.name}' 코스트센터 미지정")
            targets = [(None, None, 1.0)]
            all_auto = False

        if inv.is_tax_exempt:
            tax_code, expense, vat = settings.tax_code_exempt, item.supply_amount, 0
        elif non_deductible:
            tax_code, expense, vat = settings.tax_code_nondeductible, item.supply_amount + item.tax_amount, 0
        else:
            tax_code, expense, vat = settings.tax_code_deductible, item.supply_amount, item.tax_amount

        ratios = [t[2] for t in targets]
        for (cc, user, _), amt, tax in zip(targets, split_amount(expense, ratios), split_amount(vat, ratios)):
            p.lines.append(ProposedLine(
                item_seq=item.seq, item_name=item.name, gl_account=gl, cost_center=cc, user_id=user,
                amount=amt, tax_amount=tax, tax_code=tax_code,
                rule_id=rule["id"] if rule else None,
                text=f"{inv.supplier_name} {item.name}"[:50],
            ))

    p.auto_post = all_auto and not p.issues
    return p


def validate(inv_total: int, proposal: Proposal, require_vendor: bool = True) -> list[str]:
    """전기 직전 최종 검증 (검토 화면에서 수정한 뒤에도 다시 호출).
    사용자의 승인 요청은 SAP 공급업체 코드를 몰라도 되므로 require_vendor=False."""
    errs = []
    if require_vendor and not proposal.sap_vendor:
        errs.append("SAP 공급업체 코드가 없습니다")
    for l in proposal.lines:
        if not l.gl_account:
            errs.append(f"[{l.item_seq}] 계정 미지정")
        if not l.cost_center:
            errs.append(f"[{l.item_seq}] 코스트센터 미지정")
    debit = sum(l.amount + l.tax_amount for l in proposal.lines)
    if debit != inv_total:
        errs.append(f"차변 합계 {debit:,} ≠ 세금계산서 합계 {inv_total:,}")
    return errs
