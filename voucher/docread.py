"""AI 도우미 ⑤ 문서 읽기 — 고지서·청구서·영수증(PDF/이미지)에서 금액·납부기한·항목을 뽑는다.

세금계산서가 아닌 재산세 고지서, 보험 갱신 안내문, 점검 청구서 등을 연간 항목 처리로 바로 등록하기 위한 기능.
  - 로컬 AI(비전 모델)가 있으면: 첫 쪽 이미지 + 글자층을 함께 보고 JSON 으로 뽑는다
  - 없으면: PDF 글자층에서 '납부할 세액/청구금액/합계', '납부기한' 같은 표현을 찾아 뽑는다 (이미지는 AI 필요)
뽑은 값은 화면에서 사람이 확인·수정한 뒤 저장한다.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass

from . import ai

KEYWORDS = [  # (문서에 나오는 말, 연간 항목 이름에 들어 있는 말)
    ("재산세", "재산세"), ("자동차세", "자동차세"), ("주민세", "주민세"), ("화재보험", "화재보험"),
    ("자동차보험", "차량 보험"), ("차량보험", "차량 보험"), ("소방", "소방"), ("승강기", "승강기"),
    ("소독", "소독"), ("방역", "소독"), ("전기안전", "전기 정기 안전점검"), ("전기 안전", "전기 정기 안전점검"),
]


@dataclass
class DocResult:
    doc_type: str = ""
    item_name: str = ""
    issuer: str = ""
    amount: int | None = None
    due_date: str | None = None          # YYYY-MM-DD
    period: str = ""
    text: str = ""
    source: str = "rule"                 # ai / rule
    note: str = ""


def _pdf(data: bytes) -> tuple[str, bytes | None]:
    """PDF → (글자층 텍스트, 첫 쪽 PNG)."""
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(data)
    text = "\n".join(pdf[i].get_textpage().get_text_range() for i in range(min(len(pdf), 3)))
    buf = io.BytesIO()
    pdf[0].render(scale=1.6).to_pil().save(buf, format="PNG")
    return text, buf.getvalue()


def _amount(text: str) -> int | None:
    pats = [r"(?:납부할\s*(?:세액|금액)|청구\s*금액|합\s*계\s*금액|총\s*(?:납부|청구)?\s*금액|합\s*계)\s*[:：]?\s*([\d,]{4,})\s*원?",
            r"([\d,]{5,})\s*원"]
    for p in pats:
        nums = [int(x.replace(",", "")) for x in re.findall(p, text) if x.replace(",", "").isdigit()]
        if nums:
            return max(nums) if p == pats[1] else nums[0]
    return None


def _due(text: str) -> str | None:
    m = re.search(r"(?:납부\s*기한|납기|납부\s*기간|지급\s*기한|기\s*한)[^\d]{0,12}(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})",
                  text)
    if not m:
        ds = re.findall(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})", text)
        m = ds[-1] if ds else None
        return f"{int(m[0])}-{int(m[1]):02d}-{int(m[2]):02d}" if m else None
    return f"{int(m.group(1))}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _from_text(text: str) -> DocResult:
    hit = next(((k, v) for k, v in KEYWORDS if k in text.replace("\n", " ")), None)
    first = next((l.strip() for l in text.splitlines() if l.strip()), "")
    issuer = next((l.strip() for l in text.splitlines() if re.search(r"(주\)|\(주|시청|구청|군청|공사|보험)", l)), "")
    return DocResult(doc_type="고지서" if "세" in (hit[0] if hit else "") else "청구서",
                     item_name=hit[1] if hit else first[:30], issuer=issuer.split("·")[0].strip()[:40],
                     amount=_amount(text),
                     due_date=_due(text), text=text, source="rule")


def read(data: bytes, filename: str) -> DocResult:
    name = filename.lower()
    text, image = "", None
    if name.endswith(".pdf"):
        text, image = _pdf(data)
    elif name.endswith((".png", ".jpg", ".jpeg", ".webp")):
        image = data
    else:
        text = data.decode("utf-8", "ignore")
    fallback = _from_text(text) if text.strip() else DocResult(
        text=text, note="글자층이 없는 이미지라 로컬 AI가 있어야 읽을 수 있어요. 값을 직접 입력하세요.")
    if not ai.status().available:
        return fallback
    schema = {"type": "object", "properties": {
        "doc_type": {"type": "string"}, "item_name": {"type": "string"}, "issuer": {"type": "string"},
        "amount": {"type": "integer"}, "due_date": {"type": "string"}, "period": {"type": "string"}},
        "required": ["doc_type", "item_name", "issuer", "amount", "due_date", "period"]}
    prompt = ("한국의 세금 고지서·청구서·영수증입니다. 아래 항목을 JSON 으로 뽑으세요.\n"
              "doc_type(고지서/청구서/영수증), item_name(무엇에 대한 비용인지, 예: 재산세(건물분)), issuer(발행 기관·회사), "
              "amount(납부·청구할 총액, 원 단위 정수), due_date(납부·지급 기한 YYYY-MM-DD), period(대상 기간).\n"
              "없으면 빈 문자열이나 0.\n\n문서 글자(있으면):\n" + text[:3000])
    try:
        out = ai.chat([{"role": "user", "content": prompt}], schema=schema, images=[image] if image else None)
    except ai.AIUnavailable:
        return fallback
    due = out.get("due_date") or ""
    if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", due):
        due = fallback.due_date
    return DocResult(out.get("doc_type", ""), out.get("item_name", "") or fallback.item_name,
                     out.get("issuer", "") or fallback.issuer, int(out.get("amount") or 0) or fallback.amount,
                     due, out.get("period", ""), text, "ai")


def match_annual(conn, item_name: str) -> int | None:
    """뽑은 항목 이름 → 연간 항목 id."""
    items = conn.execute("SELECT id, name FROM annual_items WHERE active=1").fetchall()
    for k, v in KEYWORDS:
        if k in item_name or v in item_name:
            for it in items:
                if v in it["name"] and (("건물" in item_name) == ("건물" in it["name"]) or "재산세" not in v):
                    return it["id"]
    for it in items:
        if it["name"][:3] in item_name:
            return it["id"]
    return None
