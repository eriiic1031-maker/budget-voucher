"""AI 도우미 ① 검토 대기 건의 계정·부서 추천.

규칙(공급자·키워드)에 없는 세금계산서가 들어오면
  1) 과거에 전기한 건 중 품목·공급자가 비슷한 건을 찾고 (글자 2개씩 겹침 + 같은 공급자 가산)
  2) 로컬 AI 가 그 사례와 계정·부서 목록을 보고 계정·부서·불공제 여부와 이유를 고른다
  3) AI 를 못 쓰면 비슷한 과거 건의 다수결로 대신한다
추천은 분개 표에 미리 채우기만 하고, 확정은 사람이 'SAP 전기'를 눌러서 한다.
추천과 최종 전기 결과를 ai_log 에 함께 남겨 정확도를 본다.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field

from . import ai
from .db import now

_NOISE = re.compile(r"\d{2,3}\s?[가-힣]\s?\d{4}|[\d,.\s()/·\-~]+")   # 차량번호·숫자·기호 제거


def _grams(text: str) -> set[str]:
    t = _NOISE.sub("", text or "")
    return {t[i:i + 2] for i in range(len(t) - 1)} or ({t} if t else set())


@dataclass
class Suggestion:
    gl_account: str | None
    cost_center: str | None
    non_deductible: bool = False
    reason: str = ""
    confidence: float = 0.0
    source: str = "none"             # ai | history | none
    examples: list[dict] = field(default_factory=list)

    @property
    def source_label(self) -> str:
        return {"ai": "🤖 로컬 AI", "history": "📚 과거 비슷한 건", "none": "추천 없음"}[self.source]


def similar_cases(conn: sqlite3.Connection, supplier_bizno: str, item_text: str, k: int = 5) -> list[dict]:
    """과거 전기된 세금계산서 중 품목·공급자가 비슷한 건 (점수 높은 순)."""
    target = _grams(item_text)
    gl_names = dict(conn.execute("SELECT code, name FROM gl_accounts"))
    cc_names = dict(conn.execute("SELECT code, name FROM cost_centers"))
    best: dict[tuple, dict] = {}
    for r in conn.execute("SELECT supplier_bizno, supplier_name, items_json, proposal_json FROM invoices "
                          "WHERE status = 'POSTED' ORDER BY issue_date DESC LIMIT 3000"):
        lines = json.loads(r["proposal_json"] or "{}").get("lines", [])
        if not lines:
            continue
        items = " ".join(i.get("name", "") for i in json.loads(r["items_json"]))
        g = _grams(items)
        score = len(target & g) / len(target | g) if target and g else 0.0
        if r["supplier_bizno"] == supplier_bizno:
            score += 0.5
        if score < 0.15:
            continue
        top = max(lines, key=lambda l: l.get("amount", 0))
        # 여러 부서로 배부된 건은 '공통 배부'로 본다
        multi = len({l.get("cost_center") for l in lines}) > 1
        key = (items, top.get("gl_account"), "MULTI" if multi else top.get("cost_center"))
        if key in best and best[key]["score"] >= score:
            best[key]["count"] += 1
            continue
        best[key] = {
            "item": items, "supplier": r["supplier_name"], "score": round(score, 3),
            "gl_account": top.get("gl_account"), "gl_name": gl_names.get(top.get("gl_account"), ""),
            "cost_center": None if multi else top.get("cost_center"),
            "cc_name": "공통 배부" if multi else cc_names.get(top.get("cost_center"), ""),
            "non_deductible": top.get("tax_code") == "V3", "count": (best.get(key) or {}).get("count", 0) + 1,
        }
    return sorted(best.values(), key=lambda x: (-x["score"], -x["count"]))[:k]


def _from_history(cases: list[dict]) -> Suggestion:
    if not cases:
        return Suggestion(None, None, reason="비슷한 과거 건이 없어 추천할 수 없습니다. 직접 골라 주세요.")
    votes: Counter = Counter()
    for c in cases:
        votes[(c["gl_account"], c["cost_center"])] += c["score"] * c["count"]
    (gl, cc), w = votes.most_common(1)[0]
    lead = next(c for c in cases if (c["gl_account"], c["cost_center"]) == (gl, cc))
    n = sum(c["count"] for c in cases if (c["gl_account"], c["cost_center"]) == (gl, cc))
    return Suggestion(
        gl, cc, lead["non_deductible"],
        f"과거 비슷한 건 '{lead['item']}'({lead['supplier']}) 등 {n}건이 {lead['gl_name']}·{lead['cc_name']}로 처리됨",
        round(min(0.95, w / sum(votes.values()) * min(1.0, lead["score"] + 0.2)), 2), "history", cases)


def suggest(conn: sqlite3.Connection, supplier_bizno: str, supplier_name: str, item_text: str,
            amount: int) -> Suggestion:
    cases = similar_cases(conn, supplier_bizno, item_text)
    fallback = _from_history(cases)
    if not ai.status().available:
        return fallback
    accounts = conn.execute("SELECT code, name, owner FROM gl_accounts WHERE code != '11350000'").fetchall()
    ccs = conn.execute("SELECT code, name FROM cost_centers").fetchall()
    schema = {
        "type": "object",
        "properties": {
            "gl_account": {"type": "string", "enum": [a["code"] for a in accounts]},
            "cost_center": {"type": "string", "enum": [c["code"] for c in ccs] + ["COMMON"]},
            "non_deductible": {"type": "boolean"},
            "reason": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["gl_account", "cost_center", "non_deductible", "reason", "confidence"],
    }
    ex = "\n".join(f"- '{c['item']}'({c['supplier']}) → {c['gl_account']} {c['gl_name']} / {c['cc_name']} ({c['count']}건)"
                   for c in cases) or "- 없음"
    prompt = (
        "당신은 한국 회사 총무팀의 회계 담당자입니다. 매입 세금계산서의 비용 계정과 귀속 부서를 고르세요.\n"
        f"공급자: {supplier_name}\n품목: {item_text}\n공급가액: {amount:,}원\n\n"
        "계정 목록:\n" + "\n".join(f"- {a['code']} {a['name']} (예산 주관 {a['owner']})" for a in accounts) +
        "\n\n부서 목록:\n" + "\n".join(f"- {c['code']} {c['name']}" for c in ccs) +
        "\n- COMMON 여러 부서 공통(배부)\n\n과거 비슷한 처리 사례:\n" + ex +
        "\n\n규칙: 과거 사례가 있으면 우선 따르세요. 비영업용 승용차 관련 비용·접대성 비용만 non_deductible=true. "
        "reason 은 근거를 한국어 한 문장으로. confidence 는 0~1."
    )
    try:
        out = ai.chat([{"role": "user", "content": prompt}], schema=schema)
    except ai.AIUnavailable:
        return fallback
    cc = None if out["cost_center"] == "COMMON" else out["cost_center"]
    return Suggestion(out["gl_account"], cc, bool(out["non_deductible"]), str(out["reason"])[:200],
                      round(max(0.0, min(1.0, float(out["confidence"]))), 2), "ai", cases)


# ------------------------------------------------------------------ 기록 ----
def log_suggestion(conn: sqlite3.Connection, invoice_id: int, s: Suggestion) -> None:
    conn.execute("DELETE FROM ai_log WHERE feature='account' AND invoice_id=? AND decided_at IS NULL", (invoice_id,))
    out = {k: v for k, v in asdict(s).items() if k != "examples"}
    conn.execute("INSERT INTO ai_log(feature, invoice_id, source, output_json, created_at) VALUES ('account',?,?,?,?)",
                 (invoice_id, s.source, json.dumps(out, ensure_ascii=False), now()))


def last_suggestion(conn: sqlite3.Connection, invoice_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM ai_log WHERE feature='account' AND invoice_id=? ORDER BY id DESC LIMIT 1",
                     (invoice_id,)).fetchone()
    return {**json.loads(r["output_json"]), "source": r["source"], "accepted": r["accepted"]} if r else None


def record_outcome(conn: sqlite3.Connection, invoice_id: int, lines: list) -> None:
    """전기 확정 시: 추천한 계정·부서가 그대로 쓰였는지 기록 (정확도)."""
    r = conn.execute("SELECT id, output_json FROM ai_log WHERE feature='account' AND invoice_id=? "
                     "AND decided_at IS NULL ORDER BY id DESC LIMIT 1", (invoice_id,)).fetchone()
    if not r:
        return
    sug = json.loads(r["output_json"])
    if not sug.get("gl_account"):
        accepted = 0
    else:
        accepted = int(all(l.gl_account == sug["gl_account"] and
                           (sug.get("cost_center") is None or l.cost_center == sug["cost_center"]) for l in lines))
    final = [{"gl_account": l.gl_account, "cost_center": l.cost_center} for l in lines]
    conn.execute("UPDATE ai_log SET final_json=?, accepted=?, decided_at=? WHERE id=?",
                 (json.dumps(final, ensure_ascii=False), accepted, now(), r["id"]))


def accuracy(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT source, accepted FROM ai_log WHERE feature='account' AND decided_at IS NOT NULL "
                        "AND output_json NOT LIKE '%\"gl_account\": null%'").fetchall()
    total = len(rows)
    ok = sum(r["accepted"] for r in rows)
    by = Counter(r["source"] for r in rows)
    return {"total": total, "accepted": ok, "rate": ok / total if total else None, "by_source": dict(by)}
