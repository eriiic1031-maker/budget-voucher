"""월간 총무 예산 보고서 (상급자 보고용 PDF).

collect() 가 숫자를 모두 계산하고(budget·recurring·annual·anomaly), render_pdf() 가 A4 보고서로 그린다.
구성: 결재란 · 요약 지표 · 핵심 보고 사항 · 계정별 예산 현황 · 초과 사유 · 작년 대비 증가 · 처리 현황 · 조치 계획.
"""
from __future__ import annotations

import io
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

import pandas as pd

from . import ai
from .annual import due_items
from .anomaly import detect
from .attachments import counts as attach_counts
from .budget import SEVERITY, annual_summary, forecast_month, invoice_overview, yoy_increase
from .recurring import STATUS_LABEL as REC_LABEL
from .recurring import check_month
from .ui import short_won

REC_OK = ("DONE", "CONFIRMED", "SKIPPED")


@dataclass
class ReportData:
    month: str
    today: date
    budget: int
    actual: int
    forecast: int
    accounts: list                    # BudgetStatus (심각한 순)
    ytd_budget: int
    ytd_actual: int
    year_budget: int
    yoy: pd.DataFrame                 # 이번 달 1일~오늘, 작년 같은 기간 대비 증가
    yoy_now: int
    yoy_ly: int
    recurring: list
    annual: list
    anomalies: list
    invoices: int
    posted: int
    big_no_evidence: list             # 300만원 이상인데 증빙 없는 건
    messages: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    ai_summary: str | None = None

    @property
    def ym(self) -> tuple[int, int]:
        y, m = self.month.split("-")
        return int(y), int(m)


def collect(conn: sqlite3.Connection, month: str, today: date | None = None, use_ai: bool = False) -> ReportData:
    today = today or date.today()
    y, m = map(int, month.split("-"))
    res = sorted(forecast_month(conn, month, today), key=lambda s: (SEVERITY[s.status], -(s.forecast - s.budget)))
    ys = annual_summary(conn, y, m)
    yoy, now_amt, ly_amt = yoy_increase(conn, month, today, "month")
    ov = invoice_overview(conn, month)
    att = attach_counts(conn, "invoice")
    big = ov[ov.big & ~ov.invoice_id.astype(str).isin(att)]
    d = ReportData(
        month=month, today=today,
        budget=sum(s.budget for s in res), actual=sum(s.actual for s in res), forecast=sum(s.forecast for s in res),
        accounts=res,
        ytd_budget=int(ys["누적 예산"].sum()), ytd_actual=int(ys["누적 실적"].sum()), year_budget=int(ys["연간 예산"].sum()),
        yoy=yoy, yoy_now=now_amt, yoy_ly=ly_amt,
        recurring=check_month(conn, month, today), annual=due_items(conn, month, today),
        anomalies=detect(conn, month),
        invoices=len(ov), posted=int((ov.status == "POSTED").sum()),
        big_no_evidence=[(r.date, r.supplier, r.item, int(r.amount)) for r in big.itertuples()],
    )
    d.messages, d.actions = _messages(d), _actions(d)
    if use_ai and ai.status().available:
        d.ai_summary = _ai_summary(d)
    return d


def _pct(a: int, b: int) -> str:
    return f"{a / b:.0%}" if b else "-"


def _messages(d: ReportData) -> list[str]:
    """핵심 보고 사항 (개조식, 3~5줄)."""
    out = []
    gap = d.forecast - d.budget
    out.append(f"{d.ym[1]}월 예산 {short_won(d.budget)} 중 {short_won(d.actual)} 집행({_pct(d.actual, d.budget)}), "
               f"월말 {short_won(d.forecast)} 예상({_pct(d.forecast, d.budget)}) → "
               + (f"{short_won(gap)} 초과 예상" if gap > 0 else f"{short_won(-gap)} 여유 예상"))
    over = [s for s in d.accounts if s.status == "OVER"]
    risk = [s for s in d.accounts if s.status == "RISK"]
    if over or risk:
        parts = [f"{s.name}(+{short_won(s.forecast - s.budget)})" for s in (over + risk)[:3]]
        out.append(f"초과 {len(over)}개·초과 예상 {len(risk)}개 계정 — 월말 예상 초과액: " + ", ".join(parts))
    if not d.yoy.empty:
        top = d.yoy.iloc[0]
        cause = str(top["원인"]).split(" · ")[0]
        out.append(f"작년 같은 기간 대비 가장 많이 늘어난 비용은 {top['계정']} +{short_won(top['증가'])} ({cause})")
    rec_done = sum(r.status in REC_OK for r in d.recurring)
    late = [r.name for r in d.recurring if r.status == "OVERDUE"]
    out.append(f"정기 전표 {len(d.recurring)}건 중 {rec_done}건 처리"
               + (f", 기한 경과 {len(late)}건({', '.join(late)})" if late else ", 기한 경과 없음"))
    issues = []
    if d.anomalies:
        issues.append(f"이상 신호 {len(d.anomalies)}건")
    if d.big_no_evidence:
        issues.append(f"300만원 이상 증빙 미첨부 {len(d.big_no_evidence)}건")
    if issues:
        out.append("점검 필요: " + ", ".join(issues))
    return out


def _actions(d: ReportData) -> list[str]:
    out = []
    for s in d.accounts:
        if s.status == "OVER":
            out.append(f"{s.name}: 남은 기간 추가 집행은 필수 건만 진행, 초과분 {short_won(s.actual - s.budget)} 사유 소명")
        elif s.status == "RISK":
            out.append(f"{s.name}: 월말 {short_won(s.forecast)} 예상 — 이번 주 지출 계획 재점검")
    for r in d.recurring:
        if r.status == "OVERDUE":
            out.append(f"{r.name}: 기한({r.due_date[5:].replace('-', '/')}) 경과, 공급자에 세금계산서 발행 요청")
        elif r.status == "AMOUNT_DIFF":
            out.append(f"{r.name}: 예상 금액과 달라 계약 변경 여부 확인")
        elif r.status == "NOT_POSTED":
            out.append(f"{r.name}: 계산서 수신, 전기 처리")
    for a in d.annual:
        if a.status != "DONE":
            out.append(f"{a.name}: 기한 {a.due_date[5:].replace('-', '/')}까지 납부 후 연간 항목 처리")
    if d.anomalies:
        out.append(f"이상 신호 {len(d.anomalies)}건: 중복·분할 결재 여부 확인")
    if d.big_no_evidence:
        out.append(f"300만원 이상 {len(d.big_no_evidence)}건: 견적서·계약서 등 증빙 첨부")
    return out


def _ai_summary(d: ReportData) -> str | None:
    facts = {"month": d.month, "budget": d.budget, "actual": d.actual, "forecast": d.forecast,
             "key_points": d.messages, "actions": d.actions[:6]}
    prompt = ("당신은 총무팀 담당자입니다. 아래 사실만 사용해 본부장에게 올릴 보고서의 '종합 의견'을 "
              "한국어 보고체(~임, ~함)로 3문장 이내로 쓰세요. 숫자는 사실에 있는 값만, 없는 내용은 쓰지 마세요.\n\n"
              + json.dumps(facts, ensure_ascii=False))
    try:
        return ai.chat([{"role": "user", "content": prompt}], temperature=0.2).strip()
    except ai.AIUnavailable:
        return None


# =============================================================== PDF ====
FONT_CANDIDATES = [
    ("C:/Windows/Fonts/malgun.ttf", "C:/Windows/Fonts/malgunbd.ttf"),
    ("/usr/share/fonts/truetype/nanum/NanumGothic.ttf", "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf"),
    ("/usr/share/fonts/truetype/nanum/NanumGothic.ttf", "/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
    ("/System/Library/Fonts/AppleSDGothicNeo.ttc", "/System/Library/Fonts/AppleSDGothicNeo.ttc"),
]
_FONTS: tuple[str, str] | None = None


def _fonts() -> tuple[str, str]:
    """한글 글꼴 등록: 맑은 고딕 → 나눔고딕(클라우드, packages.txt) → 내장 CID 글꼴."""
    global _FONTS
    if _FONTS:
        return _FONTS
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for reg, bold in FONT_CANDIDATES:
        if Path(reg).exists() and Path(bold).exists():
            try:
                pdfmetrics.registerFont(TTFont("KR", reg))
                pdfmetrics.registerFont(TTFont("KR-B", bold))
                _FONTS = ("KR", "KR-B")
                return _FONTS
            except Exception:
                continue
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    pdfmetrics.registerFont(UnicodeCIDFont("HYGothic-Medium"))
    _FONTS = ("HYGothic-Medium", "HYGothic-Medium")
    return _FONTS


NAVY, ACCENT, GRAY, LINE, HEAD_BG = "#1F3864", "#2E74B5", "#6B7280", "#D0D5DD", "#EEF2F7"
TONE = {"OVER": ("#B42318", "#FDECEA"), "RISK": ("#C2410C", "#FFF1E6"), "WATCH": ("#8A6100", "#FFF8E1"),
        "OK": ("#1E7B34", None), "NO_BUDGET": ("#6B7280", None)}


def render_pdf(d: ReportData, author: str = "총무팀", audience: str = "팀장", memo: str = "",
               approvers: tuple[str, ...] = ("담당", "팀장", "본부장")) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)

    reg, bold = _fonts()
    C = colors.HexColor
    base = ParagraphStyle("b", fontName=reg, fontSize=9.5, leading=14, textColor=C("#1F2937"), wordWrap="CJK")
    small = ParagraphStyle("s", parent=base, fontSize=8, leading=11, textColor=C(GRAY))
    cell = ParagraphStyle("c", parent=base, fontSize=8.6, leading=12)
    cell_r = ParagraphStyle("cr", parent=cell, alignment=TA_RIGHT)
    cell_c = ParagraphStyle("cc", parent=cell, alignment=TA_CENTER)
    head = ParagraphStyle("h", parent=cell, fontName=bold, alignment=TA_CENTER, textColor=C(NAVY))
    h2 = ParagraphStyle("h2", parent=base, fontName=bold, fontSize=11.5, leading=16, textColor=C(NAVY),
                        spaceBefore=10, spaceAfter=5, keepWithNext=1)
    title = ParagraphStyle("t", parent=base, fontName=bold, fontSize=19, leading=25, textColor=C(NAVY))

    def P(t, st=base):
        return Paragraph(t if t.startswith("<") else escape(str(t)), st)

    def B(t, st=cell):
        return Paragraph(f'<font name="{bold}">{escape(str(t))}</font>', st)

    W = A4[0] - 36 * mm
    y, m = d.ym
    story = []

    # ---- 머리: 제목 + 결재란
    sign = Table([["결\n재", *approvers], ["", *[""] * len(approvers)]],
                 colWidths=[8 * mm] + [17 * mm] * len(approvers), rowHeights=[6 * mm, 15 * mm])
    sign.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, -1), reg, 8), ("TEXTCOLOR", (0, 0), (-1, -1), C("#374151")),
        ("GRID", (0, 0), (-1, -1), 0.6, C("#9CA3AF")), ("SPAN", (0, 0), (0, 1)),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (-1, 0), C(HEAD_BG)), ("BACKGROUND", (0, 0), (0, 1), C(HEAD_BG)),
    ]))
    head_tbl = Table([[[P(f"{y}년 {m}월 총무 예산 보고서", title), Spacer(1, 3),
                        P(f"기준일 {d.today:%Y-%m-%d} · 작성 {author} · 보고 {audience}", small)], sign]],
                     colWidths=[W - (8 + 17 * len(approvers)) * mm,
                                (8 + 17 * len(approvers)) * mm])
    head_tbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                  ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    story += [head_tbl, Spacer(1, 4)]
    rule = Table([[""]], colWidths=[W], rowHeights=[1.6])
    rule.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), C(NAVY))]))
    story += [rule, Spacer(1, 8)]

    # ---- 요약 지표
    gap = d.forecast - d.budget
    n_over = sum(s.status == "OVER" for s in d.accounts)
    n_risk = sum(s.status == "RISK" for s in d.accounts)
    yoy_gap = d.yoy_now - d.yoy_ly
    kpis = [
        ("이번 달 예산", short_won(d.budget), f"연간 {short_won(d.year_budget)}", NAVY),
        ("집행 (오늘까지)", short_won(d.actual), f"집행률 {_pct(d.actual, d.budget)}", NAVY),
        ("월말 예상", short_won(d.forecast),
         f"예산의 {_pct(d.forecast, d.budget)} · " + (f"{short_won(gap)} 초과" if gap > 0 else f"{short_won(-gap)} 여유"),
         TONE["OVER"][0] if gap > 0 else TONE["OK"][0]),
        ("초과 / 초과 예상", f"{n_over} / {n_risk}개", f"전체 {len(d.accounts)}개 계정",
         TONE["OVER"][0] if n_over else NAVY),
        ("연간 누적 집행", short_won(d.ytd_actual), f"누적 예산의 {_pct(d.ytd_actual, d.ytd_budget)}", NAVY),
    ]
    kv = ParagraphStyle("kv", parent=base, fontName=bold, fontSize=11, leading=15)
    cells = [[P(k, small), Paragraph(f'<font color="{c}">{escape(v)}</font>', kv), P(s, small)] for k, v, s, c in kpis]
    kt = Table([cells], colWidths=[W / len(kpis)] * len(kpis))
    kt.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, C(LINE)), ("LINEAFTER", (0, 0), (-2, -1), 0.6, C(LINE)),
        ("BACKGROUND", (0, 0), (-1, -1), C("#F8FAFC")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story += [kt]
    story.append(P(f"작년 같은 기간({m}월 1일~{d.today.day}일) {short_won(d.yoy_ly)} → 올해 {short_won(d.yoy_now)} "
                   f"({'+' if yoy_gap >= 0 else '-'}{short_won(abs(yoy_gap))})", small))

    # ---- 핵심 보고 사항
    box = [[Paragraph(f'<font name="{bold}" color="{ACCENT}">{i}.</font> {escape(t)}', base)]
           for i, t in enumerate(d.messages, 1)]
    if d.ai_summary:
        box.append([Paragraph(f'<font name="{bold}" color="{ACCENT}">종합 의견</font> {escape(d.ai_summary)} '
                              f'<font color="{GRAY}" size="7">(로컬 AI 작성)</font>', base)])
    bt = Table(box, colWidths=[W])
    bt.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.8, C(ACCENT)), ("LINEBEFORE", (0, 0), (0, -1), 3, C(ACCENT)),
                            ("LEFTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 3),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]))
    story += [KeepTogether([P("1. 핵심 보고 사항", h2), bt])]

    # ---- 계정별 예산 현황
    rows = [[P("계정", head), P("예산", head), P("집행", head), P("월말 예상", head), P("예상률", head),
             P("작년 같은 달", head), P("상태", head)]]
    style = [("GRID", (0, 0), (-1, -1), 0.4, C(LINE)), ("BACKGROUND", (0, 0), (-1, 0), C(HEAD_BG)),
             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 2.5),
             ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5)]
    for i, s in enumerate(d.accounts, 1):
        fg, bg = TONE[s.status]
        rows.append([P(s.name, cell), P(short_won(s.budget), cell_r), P(short_won(s.actual), cell_r),
                     P(short_won(s.forecast), cell_r), P(_pct(s.forecast, s.budget), cell_r),
                     P(short_won(s.ly_actual), cell_r),
                     Paragraph(f'<font name="{bold}" color="{fg}">{s.label}</font>', cell_c)])
        if bg:
            style.append(("BACKGROUND", (0, i), (-1, i), C(bg)))
    rows.append([B("합계"), B(short_won(d.budget), cell_r), B(short_won(d.actual), cell_r),
                 B(short_won(d.forecast), cell_r), B(_pct(d.forecast, d.budget), cell_r),
                 B(short_won(sum(s.ly_actual for s in d.accounts)), cell_r), P("", cell)])
    style += [("LINEABOVE", (0, -1), (-1, -1), 1, C(NAVY)), ("BACKGROUND", (0, -1), (-1, -1), C("#F3F4F6"))]
    at = Table(rows, colWidths=[W * x for x in (.18, .14, .14, .16, .09, .15, .14)], repeatRows=1)
    at.setStyle(TableStyle(style))
    story += [P("2. 계정별 예산 현황", h2), at,
              P("월말 예상 = 지금까지 집행 + 남은 정기 전표 + 비정기 지출 속도(작년 같은 달 계절성 반영). "
                "빨강 = 이미 초과, 주황 = 월말 초과 예상, 노랑 = 주의(90% 이상).", small)]

    # ---- 초과 사유
    alert = [s for s in d.accounts if s.status in ("OVER", "RISK")]
    if alert:
        rs = [[P("계정", head), P("사유", head)]]
        for s in alert:
            fg, _ = TONE[s.status]
            why = "<br/>".join("· " + escape(r.replace("**", "")) for r in s.reasons[:4]) or "-"
            rs.append([Paragraph(f'<font name="{bold}">{escape(s.name)}</font><br/>'
                                 f'<font color="{fg}" size="7.5">{s.label}</font>', cell), Paragraph(why, cell)])
        rt = Table(rs, colWidths=[W * .2, W * .8], repeatRows=1)
        rt.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, C(LINE)), ("BACKGROUND", (0, 0), (-1, 0), C(HEAD_BG)),
                                ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story += [P("3. 초과·초과 예상 사유", h2), rt]

    # ---- 작년 대비 증가
    n = 4 if alert else 3
    if not d.yoy.empty:
        ys = [[P("계정", head), P("작년", head), P("올해", head), P("증가", head), P("주요 원인", head)]]
        for r in d.yoy.head(5).itertuples():
            ys.append([P(r.계정, cell), P(short_won(r.작년), cell_r), P(short_won(r.올해), cell_r),
                       Paragraph(f'<font color="{TONE["OVER"][0]}">+{escape(short_won(r.증가))}</font>', cell_r),
                       Paragraph(escape(str(r.원인)).replace(" · ", "<br/>"), cell)])
        yt = Table(ys, colWidths=[W * x for x in (.16, .13, .13, .12, .46)], repeatRows=1)
        yt.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, C(LINE)), ("BACKGROUND", (0, 0), (-1, 0), C(HEAD_BG)),
                                ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
        story += [KeepTogether([P(f"{n}. 작년 같은 기간 대비 늘어난 비용", h2), yt])]
        n += 1

    # ---- 처리 현황
    rec_done = sum(r.status in REC_OK for r in d.recurring)
    rec_bad = [f"{r.name}({REC_LABEL[r.status]})" for r in d.recurring if r.status not in REC_OK + ("WAITING",)]
    ann = ", ".join(f"{a.name}({a.label})" for a in d.annual) or "해당 없음"
    kinds: dict[str, int] = {}
    for a in d.anomalies:
        kinds[a.kind] = kinds.get(a.kind, 0) + 1
    st_rows = [
        [B("세금계산서"), P(f"{d.invoices}건 수신 · 전기 완료 {d.posted}건 · 처리 중 {d.invoices - d.posted}건", cell)],
        [B("정기 전표"), P(f"{len(d.recurring)}건 중 {rec_done}건 처리"
                          + (" · 확인 필요: " + ", ".join(rec_bad) if rec_bad else " · 확인 필요 없음"), cell)],
        [B("연간 항목"), P(ann, cell)],
        [B("이상 신호"), P(", ".join(f"{k} {v}건" for k, v in kinds.items()) or "없음", cell)],
        [B("증빙 미첨부"), P(f"300만원 이상 {len(d.big_no_evidence)}건"
                           + (": " + ", ".join(f"{s} {short_won(a)}" for _, s, _, a in d.big_no_evidence[:4])
                              + (" 외" if len(d.big_no_evidence) > 4 else "") if d.big_no_evidence else ""), cell)],
    ]
    stt = Table(st_rows, colWidths=[W * .16, W * .84])
    stt.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, C(LINE)), ("BACKGROUND", (0, 0), (0, -1), C(HEAD_BG)),
                             ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    story += [KeepTogether([P(f"{n}. 처리 현황", h2), stt])]
    n += 1

    # ---- 조치 계획
    acts = d.actions or ["특이 사항 없음"]
    ac = [[Paragraph(f'<font name="{bold}" color="{ACCENT}">□</font>', cell_c), P(a, cell)] for a in acts[:10]]
    if len(acts) > 10:
        ac.append(["", P(f"외 {len(acts) - 10}건", small)])
    act = Table(ac, colWidths=[W * .05, W * .95])
    act.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.3, C(LINE)), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                             ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    blk = [P(f"{n}. 조치 계획 및 요청 사항", h2), act]
    if memo.strip():
        mt = Table([[Paragraph(f'<font name="{bold}">담당자 의견</font><br/>'
                               + escape(memo.strip()).replace("\n", "<br/>"), base)]], colWidths=[W])
        mt.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.6, C(LINE)), ("BACKGROUND", (0, 0), (-1, -1), C("#F8FAFC")),
                                ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 5),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
        blk += [Spacer(1, 6), mt]
    story += [KeepTogether(blk)]

    def footer(cv, doc):
        cv.saveState()
        cv.setFont(reg, 7.5)
        cv.setFillColor(C(GRAY))
        cv.drawString(18 * mm, 10 * mm, f"예산 및 전표관리 · {y}년 {m}월 총무 예산 보고서 · 숫자는 시스템 자동 계산 값")
        cv.drawRightString(A4[0] - 18 * mm, 10 * mm, f"{doc.page} 쪽")
        cv.setStrokeColor(C(LINE))
        cv.line(18 * mm, 13.5 * mm, A4[0] - 18 * mm, 13.5 * mm)
        cv.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=18 * mm, title=f"{y}년 {m}월 총무 예산 보고서", author=author,
                            subject="총무 예산 월간 보고", creator="예산 및 전표관리")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def page_images(pdf: bytes, scale: float = 1.6) -> list[bytes]:
    """화면 미리보기용: PDF 각 쪽을 PNG 로."""
    import pypdfium2 as pdfium
    out = []
    doc = pdfium.PdfDocument(pdf)
    for page in doc:
        b = io.BytesIO()
        page.render(scale=scale).to_pil().save(b, format="PNG")
        out.append(b.getvalue())
    return out
