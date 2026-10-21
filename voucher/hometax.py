"""홈택스(국세청) 매입 전자세금계산서 수집 및 대사.

메일 수집은 공급자가 메일을 안 보내거나 XML 없이 링크만 보내면 누락된다.
홈택스에는 발행된 모든 세금계산서가 있으므로
  1) 기간별 매입 목록을 받아 hometax_invoices 에 저장하고
  2) 시스템(invoices)에 없는 건은 XML을 내려받아 자동으로 같은 파이프라인에 태우고
  3) 국세청 기준 vs 시스템 기준을 대사해 누락·불일치를 보여준다.

PopbillHometaxClient: 팝빌 홈택스 수집 API (https://developers.popbill.com)
  사전 준비(사용자가 팝빌에서 직접): 연동회원 가입 → LinkID/SecretKey 발급 →
  홈택스 인증서 또는 부서사용자 계정 등록 → 홈택스 수집 정액제 신청
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

import pandas as pd

from .config import BASE_DIR, settings
from .db import audit, now
from .pipeline import ingest_invoice, ingest_xml


class HometaxError(RuntimeError):
    pass


@dataclass
class HometaxRecord:
    approval_no: str
    write_date: str
    issue_date: str | None
    supplier_bizno: str
    supplier_name: str
    supply_amount: int
    tax_amount: int
    total_amount: int
    modify_yn: bool
    item_name: str = ""


class HometaxClient(Protocol):
    name: str

    def list_purchases(self, start: date, end: date) -> list[HometaxRecord]: ...

    def get_xml(self, approval_no: str) -> bytes: ...
    # XML 을 줄 수 없는 수집원(홈택스 엑셀 등)은 get_xml 대신 get_invoice(approval_no) -> TaxInvoice 를 구현


def _ymd(s: str | None) -> str | None:
    if not s:
        return None
    d = "".join(c for c in str(s) if c.isdigit())[:8]
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else None


def _int(v) -> int:
    try:
        return int(round(float(str(v).replace(",", ""))))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- 팝빌 ----
class PopbillHometaxClient:
    name = "popbill"
    JOB_DONE = 3

    def __init__(self, poll_seconds: float = 2.0, timeout_seconds: float = 180.0):
        try:
            from popbill import HTTaxinvoiceService, PopbillException  # type: ignore
        except ImportError as e:
            raise HometaxError("popbill 패키지가 없습니다: pip install popbill") from e
        if not (settings.popbill_link_id and settings.popbill_secret_key):
            raise HometaxError("POPBILL_LINK_ID / POPBILL_SECRET_KEY 를 .env 에 설정하세요")

        self._exc = PopbillException
        self.svc = HTTaxinvoiceService(settings.popbill_link_id, settings.popbill_secret_key)
        self.svc.IsTest = settings.popbill_is_test
        self.svc.IPRestrictOnOff = True
        self.svc.UseLocalTimeYN = True
        self.corp = settings.company_bizno
        self.user = settings.popbill_user_id or None
        self.poll, self.timeout = poll_seconds, timeout_seconds

    def _call(self, fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except self._exc as e:
            raise HometaxError(f"팝빌 오류 [{e.code}] {e.message}") from e

    # 상태 점검용 (관리자 페이지에서 사용)
    def check_status(self) -> dict[str, str]:
        out = {}
        for label, fn in [("인증서", self.svc.checkCertValidation), ("부서사용자", self.svc.checkDeptUser)]:
            try:
                r = fn(self.corp, self.user)
                out[label] = f"정상 ({r.message})"
            except self._exc as e:
                out[label] = f"[{e.code}] {e.message}"
        try:
            st = self.svc.getFlatRateState(self.corp, self.user)
            out["정액제"] = f"이용 중 (만료 {st.useEndDate})" if st.state == 1 else "미신청/만료"
        except self._exc as e:
            out["정액제"] = f"[{e.code}] {e.message}"
        return out

    def certificate_url(self) -> str:
        """홈택스 인증서를 팝빌에 등록하는 팝업 URL (담당자가 브라우저에서 직접 등록)."""
        return self._call(self.svc.getCertificatePopUpURL, self.corp, self.user)

    def list_purchases(self, start: date, end: date) -> list[HometaxRecord]:
        # 1) 수집 요청: BUY(매입), DType W(작성일자) 기준
        job_id = self._call(self.svc.requestJob, self.corp, "BUY", "W",
                            start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), self.user)
        # 2) 완료 대기 (홈택스 조회라 수 초~수십 초 걸림)
        deadline = time.monotonic() + self.timeout
        while True:
            st = self._call(self.svc.getJobState, self.corp, job_id, self.user)
            if st.jobState == self.JOB_DONE:
                if st.errorCode != 1:
                    raise HometaxError(f"홈택스 수집 실패 [{st.errorCode}] {st.errorReason}")
                break
            if time.monotonic() > deadline:
                raise HometaxError(f"홈택스 수집 시간 초과 (JobID {job_id})")
            time.sleep(self.poll)
        # 3) 결과 조회 (페이지 단위)
        out, page = [], 1
        while True:
            res = self._call(self.svc.search, self.corp, job_id, ["N", "M"], ["T", "N", "Z"], ["R", "C", "N"],
                             "", "", "", page, 1000, "D", self.user)
            for r in res.list or []:
                out.append(HometaxRecord(
                    approval_no=str(r.ntsconfirmNum).replace("-", ""),
                    write_date=_ymd(r.writeDate),
                    issue_date=_ymd(r.issueDate),
                    supplier_bizno="".join(c for c in str(r.invoicerCorpNum or "") if c.isdigit()),
                    supplier_name=r.invoicerCorpName or "",
                    supply_amount=_int(r.supplyCostTotal),
                    tax_amount=_int(r.taxTotal),
                    total_amount=_int(r.totalAmount),
                    modify_yn=bool(r.modifyYN),
                    item_name=r.itemName or "",
                ))
            if page >= (res.pageCount or 1):
                break
            page += 1
        return out

    def get_xml(self, approval_no: str) -> bytes:
        res = self._call(self.svc.getXML, self.corp, approval_no, self.user)
        xml = res.retObject
        if not xml:
            raise HometaxError(f"XML 없음: {approval_no}")
        return xml.encode("utf-8") if isinstance(xml, str) else xml


# ---------------------------------------------------------------- 데모 ----
class MockHometaxClient:
    """SAP·팝빌 계약 전 데모용. samples/ 의 XML + '메일로 안 온' 세금계산서를 홈택스 목록인 것처럼 돌려준다."""
    name = "mock"

    def __init__(self):
        from .parser import parse_xml_bytes
        from .seed import hometax_only_invoices

        self._xml: dict[str, bytes] = {}
        files = [p.read_bytes() for p in sorted((BASE_DIR / "samples").glob("*.xml"))]
        for xml in files + [x for _, x in hometax_only_invoices()]:
            self._xml[parse_xml_bytes(xml).approval_no] = xml
        self._parse = parse_xml_bytes

    def list_purchases(self, start: date, end: date) -> list[HometaxRecord]:
        out = []
        for no, xml in self._xml.items():
            inv = self._parse(xml)
            if start.isoformat() <= inv.issue_date <= end.isoformat():
                out.append(HometaxRecord(no, inv.issue_date, inv.issue_date, inv.supplier_bizno, inv.supplier_name,
                                         inv.supply_amount, inv.tax_amount, inv.total_amount, inv.is_modified,
                                         inv.items[0].name if inv.items else ""))
        return out

    def get_xml(self, approval_no: str) -> bytes:
        return self._xml[approval_no]


def get_client() -> HometaxClient:
    if settings.hometax_provider == "popbill":
        return PopbillHometaxClient()
    return MockHometaxClient()


# ---------------------------------------------------------------- 동기화 ----
@dataclass
class SyncResult:
    fetched: int
    new_in_hometax: int
    ingested: int
    errors: list[str]


def sync(conn: sqlite3.Connection, start: date, end: date, client: HometaxClient | None = None,
         auto_ingest: bool = True, actor: str = "system", save_dir: Path | None = None) -> SyncResult:
    client = client or get_client()
    records = client.list_purchases(start, end)
    ts = now()
    conn.executemany(
        """INSERT INTO hometax_invoices(approval_no, write_date, issue_date, supplier_bizno, supplier_name,
               supply_amount, tax_amount, total_amount, modify_yn, item_name, fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(approval_no) DO UPDATE SET
               supply_amount=excluded.supply_amount, tax_amount=excluded.tax_amount,
               total_amount=excluded.total_amount, fetched_at=excluded.fetched_at""",
        [(r.approval_no, r.write_date, r.issue_date, r.supplier_bizno, r.supplier_name, r.supply_amount,
          r.tax_amount, r.total_amount, int(r.modify_yn), r.item_name, ts) for r in records],
    )

    known = {row[0] for row in conn.execute("SELECT approval_no FROM invoices")}
    missing = [r for r in records if r.approval_no not in known]
    ingested, errors = 0, []
    if auto_ingest:
        save_dir = save_dir or settings.inbox_dir / "hometax" / date.today().strftime("%Y%m")
        save_dir.mkdir(parents=True, exist_ok=True)
        for r in missing:
            try:
                if hasattr(client, "get_invoice"):
                    res = ingest_invoice(conn, client.get_invoice(r.approval_no), client.name)
                else:
                    xml = client.get_xml(r.approval_no)
                    path = save_dir / f"{r.approval_no}.xml"
                    path.write_bytes(xml)
                    res = ingest_xml(conn, xml, "hometax", str(path))
                if res.status == "PARSE_ERROR":
                    errors.append(f"{r.approval_no}: {res.message}")
                else:
                    ingested += 1
            except Exception as e:  # 한 건 실패가 전체 동기화를 막지 않게
                errors.append(f"{r.approval_no}: {e}")

    conn.execute(
        "INSERT INTO hometax_sync_log(provider, start_date, end_date, fetched, ingested, errors, actor, at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (client.name, start.isoformat(), end.isoformat(), len(records), ingested, "\n".join(errors) or None,
         actor, ts),
    )
    audit(conn, None, "HOMETAX_SYNC", {"start": start.isoformat(), "end": end.isoformat(),
                                       "fetched": len(records), "ingested": ingested}, actor)
    return SyncResult(len(records), len(missing), ingested, errors)


# ---------------------------------------------------------------- 대사 ----
RECON_LABEL = {
    "MATCH": "일치",
    "AMOUNT_DIFF": "금액 불일치",
    "HOMETAX_ONLY": "시스템 미수집",
    "SYSTEM_ONLY": "홈택스 미확인",
}


def reconcile(conn: sqlite3.Connection, month: str) -> pd.DataFrame:
    """국세청(홈택스) 기준과 시스템 수집분을 승인번호로 대사한다 (작성일자 기준 월)."""
    h = pd.read_sql_query(
        "SELECT * FROM hometax_invoices WHERE substr(write_date,1,7) = ?", conn, params=[month])
    s = pd.read_sql_query(
        """SELECT i.approval_no, i.issue_date, i.supplier_bizno, i.supplier_name, i.total_amount, i.status,
                  i.source, p.sap_doc_no
           FROM invoices i LEFT JOIN postings p ON p.invoice_id = i.id
           WHERE substr(i.issue_date,1,7) = ? AND i.status != 'IGNORED'""", conn, params=[month])
    m = h.merge(s, on="approval_no", how="outer", suffixes=("_ht", "_sys"), indicator=True)

    def _status(r):
        if r["_merge"] == "left_only":
            return "HOMETAX_ONLY"
        if r["_merge"] == "right_only":
            return "SYSTEM_ONLY"
        return "MATCH" if r["total_amount_ht"] == r["total_amount_sys"] else "AMOUNT_DIFF"

    if m.empty:
        return pd.DataFrame(columns=["recon", "대사", "approval_no", "작성일", "공급자", "홈택스 금액", "시스템 금액",
                                     "상태", "수집경로", "sap_doc_no"])
    m["recon"] = m.apply(_status, axis=1)
    m["대사"] = m["recon"].map(RECON_LABEL)
    m["작성일"] = m["write_date"].fillna(m["issue_date_sys"])
    m["공급자"] = m["supplier_name_ht"].fillna(m["supplier_name_sys"])
    out = m.rename(columns={"total_amount_ht": "홈택스 금액", "total_amount_sys": "시스템 금액",
                            "status": "상태", "source": "수집경로"})
    order = {"HOMETAX_ONLY": 0, "AMOUNT_DIFF": 1, "SYSTEM_ONLY": 2, "MATCH": 3}
    out = out.sort_values(by=["recon", "작성일"], key=lambda c: c.map(order) if c.name == "recon" else c)
    return out[["recon", "대사", "approval_no", "작성일", "공급자", "홈택스 금액", "시스템 금액", "상태", "수집경로",
                "sap_doc_no"]].reset_index(drop=True)


def month_range(month: str) -> tuple[date, date]:
    import calendar
    y, m = map(int, month.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def last_sync(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM hometax_sync_log ORDER BY id DESC LIMIT 1").fetchone()
