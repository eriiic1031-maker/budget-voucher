"""AI 도우미 ② 월간 총무 예산 브리핑 · ③ 질문하기.

원칙: 숫자는 전부 코드(budget·recurring·annual)가 계산하고, AI 는
  - 브리핑: 계산된 사실(JSON)만 보고 문장으로 정리
  - 질문하기: 정해 둔 조회 함수 중 무엇을 부를지 고르고(도구 선택), 결과 숫자로만 답한다
AI 가 없으면 키워드로 조회 함수를 고르고 정해진 문장 틀로 답한다.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from . import ai
from .annual import due_items
from .budget import STATUS_LABEL, forecast_month, load_lines, shift_month, yoy_increase
from .db import now
from .recurring import STATUS_LABEL as REC_LABEL
from .recurring import check_month
from .ui import short_won


def _plain(md: str) -> str:
    return md.replace("**", "")


# ================================================================ 브리핑 ====
def briefing_facts(conn: sqlite3.Connection, month: str, today: date | None = None) -> dict:
    today = today or date.today()
    res = forecast_month(conn, month, today)
    tb, ta, tf = (sum(getattr(s, k) for s in res) for k in ("budget", "actual", "forecast"))
    yoy, now_amt, ly_amt = yoy_increase(conn, month, today, "month")
    rec = check_month(conn, month, today)
    from .anomaly import detect
    return {
        "month": month,
        "budget": tb, "actual": ta, "forecast": tf,
        "alerts": [{"account": s.name, "status": STATUS_LABEL[s.status], "budget": s.budget, "forecast": s.forecast,
                    "reasons": [_plain(r) for r in s.reasons[:3]]}
                   for s in res if s.status in ("OVER", "RISK")],
        "watch": [s.name for s in res if s.status == "WATCH"],
        "yoy": [{"account": r["계정"], "increase": int(r["증가"]), "why": r["원인"]} for _, r in yoy.head(3).iterrows()],
        "yoy_total": {"this_year": now_amt, "last_year": ly_amt},
        "recurring_issues": [{"name": r.name, "status": REC_LABEL[r.status], "due": r.due_date}
                             for r in rec if r.status in ("OVERDUE", "AMOUNT_DIFF", "NOT_POSTED")],
        "annual_pending": [{"name": a.name, "due": a.due_date, "expected": a.expected_amount}
                           for a in due_items(conn, month, today) if a.status != "DONE"],
        "anomalies": [{"kind": a.kind, "supplier": a.supplier, "amount": a.amount} for a in detect(conn, month)][:5],
    }


def _briefing_template(f: dict) -> str:
    y, m = f["month"].split("-")
    rate = f["forecast"] / f["budget"] if f["budget"] else 0
    out = [f"**{int(m)}월 총무 예산 브리핑**",
           f"- 예산 {short_won(f['budget'])} 중 {short_won(f['actual'])} 사용, 월말 예상 {short_won(f['forecast'])} "
           f"(예산의 {rate:.0%})."]
    over = [a for a in f["alerts"] if a["status"] == "초과"]
    risk = [a for a in f["alerts"] if a["status"] != "초과"]
    if over:
        out.append("- 🔴 초과 " + str(len(over)) + "건: " + "; ".join(
            f"{a['account']}({(a['reasons'][1] if len(a['reasons']) > 1 else a['reasons'][0]).rstrip('.')})"
            for a in over[:3]))
    if risk:
        out.append("- 🟠 초과 예상 " + str(len(risk)) + "건: " + "; ".join(
            f"{a['account']} 월말 {short_won(a['forecast'])}/예산 {short_won(a['budget'])}" for a in risk))
    if f["yoy"]:
        out.append("- 작년보다 늘어난 비용: " + "; ".join(
            f"{x['account']} +{short_won(x['increase'])} ({x['why'].split(' · ')[0]})" for x in f["yoy"]))
    todo = [f"{r['name']} {r['status']}" for r in f["recurring_issues"]] + \
           [f"연간 항목 {a['name']}(기한 {a['due'][5:]})" for a in f["annual_pending"]] + \
           [f"이상 신호 {len(f['anomalies'])}건"] * bool(f["anomalies"])
    if todo:
        out.append("- 확인할 일: " + ", ".join(todo) + ".")
    return "\n".join(out)


def briefing(conn: sqlite3.Connection, month: str, today: date | None = None) -> tuple[str, str]:
    """(브리핑 마크다운, 출처 ai/rule) — 결과는 ai_log 에 보관."""
    facts = briefing_facts(conn, month, today)
    text, source = _briefing_template(facts), "rule"
    if ai.status().available:
        prompt = (
            "당신은 한국 회사 총무팀 담당자입니다. 아래 JSON 사실만 사용해 팀장에게 보고할 월간 총무 예산 브리핑을 쓰세요.\n"
            "규칙: 한국어, 5~7개의 '- '로 시작하는 짧은 문장. 첫 줄은 '**N월 총무 예산 브리핑**'. 숫자는 JSON 값만 "
            "쓰고 원 단위는 '억·만원'으로 읽기 쉽게. 마지막 줄은 이번 주에 할 일. 없는 사실은 지어내지 마세요.\n\n"
            + json.dumps(facts, ensure_ascii=False, default=str)
        )
        try:
            text, source = ai.chat([{"role": "user", "content": prompt}], temperature=0.2), "ai"
        except ai.AIUnavailable:
            pass
    conn.execute("INSERT INTO ai_log(feature, month, source, input_json, output_json, created_at) "
                 "VALUES ('briefing',?,?,?,?,?)",
                 (month, source, json.dumps(facts, ensure_ascii=False, default=str), text, now()))
    return text, source


def last_briefing(conn: sqlite3.Connection, month: str) -> tuple[str, str, str] | None:
    r = conn.execute("SELECT output_json, source, created_at FROM ai_log WHERE feature='briefing' AND month=? "
                     "ORDER BY id DESC LIMIT 1", (month,)).fetchone()
    return (r[0], r[1], r[2]) if r else None


# ================================================================ 질문하기 ====
TOOLS = {
    "budget_overview": "이번 달 총무 예산 전체 현황 (예산·사용·월말 예상·경고 계정)",
    "account_detail": "특정 계정의 예산·월말 예상·경고 이유·주요 공급자",
    "yoy": "작년보다 늘어난 비용과 원인 (이번 달 또는 올해 누적)",
    "recurring_status": "정기 전표(임차료·관리비 등) 처리 상태",
    "annual_status": "연간 항목(재산세·자동차세·보험·점검) 처리 상태",
}


@dataclass
class Answer:
    text: str
    tool: str
    args: dict
    source: str
    table: pd.DataFrame | None = field(default=None)


def _parse_month(q: str, base: str) -> str:
    y, m = map(int, base.split("-"))
    if "지난달" in q or "전월" in q:
        return shift_month(base, -1)
    if "다음달" in q or "다음 달" in q:
        return shift_month(base, 1)
    mm = re.search(r"(20\d{2})\s*년\s*(\d{1,2})\s*월", q) or re.search(r"(\d{1,2})\s*월", q)
    if mm:
        if len(mm.groups()) == 2:
            return f"{int(mm.group(1))}-{int(mm.group(2)):02d}"
        mo = int(mm.group(1))
        if 1 <= mo <= 12:
            return f"{y if mo <= m else y - 1}-{mo:02d}"
    return base


def route_rule(q: str, base_month: str, accounts: list[str]) -> tuple[str, dict]:
    month = _parse_month(q, base_month)
    scope = "ytd" if re.search(r"올해|누적|연간 누적|1월부터", q) else "month"
    acct = next((a for a in sorted(accounts, key=len, reverse=True) if a in q), "")
    if re.search(r"연간 항목|재산세|자동차세|주민세|보험|소방|승강기|소독|안전점검", q):
        return "annual_status", {"month": month}
    if acct:
        return "account_detail", {"month": month, "account": acct}
    if re.search(r"늘|증가|작년|전년|올랐", q):
        return "yoy", {"month": month, "scope": scope}
    if re.search(r"정기|임차료|관리비|미수신|기한", q):
        return "recurring_status", {"month": month}
    return "budget_overview", {"month": month}


def run_tool(conn: sqlite3.Connection, tool: str, args: dict, today: date | None = None) -> tuple[dict, pd.DataFrame]:
    month = args.get("month")
    if tool == "account_detail":
        res = {s.name: s for s in forecast_month(conn, month, today)}
        s = res.get(args.get("account"))
        if not s:
            return {"error": f"'{args.get('account')}'는 총무 예산 계정이 아닙니다"}, pd.DataFrame()
        y, m = month.split("-")
        lines = load_lines(conn, f"{month}-01", f"{month}-31")
        vend = (lines[lines.gl == s.gl].groupby("supplier").amount.sum().sort_values(ascending=False).head(5)
                .reset_index().rename(columns={"supplier": "공급자", "amount": "금액"}))
        return ({"account": s.name, "status": STATUS_LABEL[s.status], "budget": s.budget, "actual": s.actual,
                 "forecast": s.forecast, "last_year_same_month": s.ly_actual,
                 "reasons": [_plain(r) for r in s.reasons]}, vend)
    if tool == "yoy":
        df, a, b = yoy_increase(conn, month, today, args.get("scope", "month"))
        return {"this_year": a, "last_year": b, "rows": df.to_dict("records")}, df
    if tool == "recurring_status":
        rows = [{"항목": r.name, "상태": REC_LABEL[r.status], "기한": r.due_date, "예상": r.expected_amount,
                 "실제": r.actual_amount} for r in check_month(conn, month, today)]
        return {"items": rows}, pd.DataFrame(rows)
    if tool == "annual_status":
        rows = [{"항목": a.name, "상태": a.label, "기한": a.due_date, "예상": a.expected_amount, "처리 금액": a.amount}
                for a in due_items(conn, month, today)]
        nxt = [{"항목": a.name, "기한": a.due_date, "예상": a.expected_amount}
               for a in due_items(conn, shift_month(month, 1), today)]
        return {"this_month": rows, "next_month": nxt}, pd.DataFrame(rows)
    res = forecast_month(conn, month, today)
    rows = [{"계정": s.name, "상태": STATUS_LABEL[s.status], "예산": s.budget, "사용": s.actual, "월말 예상": s.forecast}
            for s in res]
    return ({"budget": sum(s.budget for s in res), "actual": sum(s.actual for s in res),
             "forecast": sum(s.forecast for s in res), "accounts": rows}, pd.DataFrame(rows))


def _answer_template(tool: str, data: dict, args: dict) -> str:
    if "error" in data:
        return data["error"]
    mo = args.get("month", "")
    if tool == "account_detail":
        t = (f"**{mo} {data['account']}**는 {data['status']}입니다. 예산 {short_won(data['budget'])}, "
             f"사용 {short_won(data['actual'])}, 월말 예상 {short_won(data['forecast'])} "
             f"(작년 같은 달 {short_won(data['last_year_same_month'])}).")
        return t + ("\n\n" + "\n".join(f"- {r}" for r in data["reasons"]) if data["reasons"] else "")
    if tool == "yoy":
        rows = data["rows"]
        head = (f"{'올해 누적' if args.get('scope') == 'ytd' else mo}: {short_won(data['this_year'])} "
                f"(작년 같은 기간 {short_won(data['last_year'])}).")
        return head + ("\n\n" + "\n".join(f"- {r['계정']} +{short_won(r['증가'])}: {r['원인']}" for r in rows[:5])
                       if rows else " 늘어난 계정이 없습니다.")
    if tool == "recurring_status":
        bad = [i for i in data["items"] if i["상태"] not in ("전기 완료", "수동 확인", "이번 달 해당 없음")]
        done = len(data["items"]) - len(bad)
        return f"{mo} 정기 전표 {len(data['items'])}건 중 {done}건 완료." + (
            "\n\n" + "\n".join(f"- {i['항목']}: {i['상태']} (기한 {i['기한'][5:]})" for i in bad) if bad else "")
    if tool == "annual_status":
        t = "\n".join(f"- {r['항목']}: {r['상태']} (기한 {r['기한'][5:]}, 예상 {short_won(r['예상'])})"
                      for r in data["this_month"]) or "- 이번 달 연간 항목 없음"
        n = ", ".join(f"{r['항목']}({r['기한'][5:]})" for r in data["next_month"])
        return f"**{mo} 연간 항목**\n\n{t}" + (f"\n\n다음 달: {n}" if n else "")
    over = [r for r in data["accounts"] if r["상태"] in ("초과", "초과 예상")]
    return (f"{mo} 총무 예산 {short_won(data['budget'])} 중 {short_won(data['actual'])} 사용, 월말 예상 "
            f"{short_won(data['forecast'])}." + ("\n\n" + "\n".join(f"- {r['계정']}: {r['상태']} "
                                                                   f"(월말 {short_won(r['월말 예상'])}/예산 {short_won(r['예산'])})"
                                                                   for r in over) if over else ""))


def _mo(m: str) -> str:
    y, mm = m.split("-")
    return f"{int(mm)}월"


def _j(word: str, pair: str) -> str:
    """받침에 맞는 조사: _j('임차료', '은는') → '임차료는'."""
    ch = next((c for c in reversed(word) if c.strip() and c not in ")]'\""), "")
    jong = (ord(ch) - 0xAC00) % 28 if "가" <= ch <= "힣" else 0
    if pair == "으로":
        return word + ("으로" if jong and jong != 8 else "로")
    return word + (pair[0] if jong else pair[1])


def _clean(r: str) -> str:
    return r.replace("**", "").rstrip(".").strip()


def _example_answer(tool: str, data: dict, args: dict) -> str:
    """로컬 AI 미연결 시 '예시 답안' — 같은 근거 데이터로, AI 가 답할 법한 말투의 문장으로 만든다."""
    if "error" in data:
        return data["error"]
    mo = _mo(args.get("month", "2026-10"))
    if tool == "account_detail":
        a, b, u, f = data["account"], data["budget"], data["actual"], data["forecast"]
        if data["status"] == "초과":
            s1 = f"{mo} {_j(a, '은는')} 이미 예산 {_j(short_won(b), '을를')} {short_won(u - b)} 넘었습니다(지금까지 {short_won(u)} 사용)."
        elif data["status"] in ("초과 예상", "주의"):
            s1 = f"{mo} {_j(a, '은는')} 아직 예산 안이지만, 월말에는 {_j(short_won(f), '으로')} 예산 {short_won(b)}의 {f / b:.0%}가 예상됩니다."
        else:
            s1 = f"{mo} {_j(a, '은는')} 예산 {short_won(b)} 중 {_j(short_won(u), '을를')} 써서 정상 범위입니다."
        why = [_clean(r) for r in data["reasons"] if not r.startswith(("이미 예산을", "월말 예상"))][:2]
        s2 = (f" 원인은 {'두 가지' if len(why) > 1 else '이렇습니다'}입니다. " if len(why) > 1 else " 원인은 ")
        s2 = (s2 + " ".join(f"{'①②'[i]} {w}." for i, w in enumerate(why))) if len(why) > 1 else (
            f" 원인은 {why[0]}입니다." if why else "")
        s3 = (f" 이대로면 월말 {short_won(f)}(예산의 {f / b:.0%})이 예상되니 남은 기간 지출을 점검해 보시길 권합니다."
              if b and f > b else " 지금 속도라면 예산 안에서 마감할 것으로 보입니다.")
        return s1 + s2 + s3
    if tool == "yoy":
        rows, now_, ly = data["rows"], data["this_year"], data["last_year"]
        period = "올해 1월부터 오늘까지" if args.get("scope") == "ytd" else f"{mo} 1일부터 오늘까지"
        diff = now_ - ly
        s1 = (f"{period} 총무 비용은 {_j(short_won(now_), '으로')} 작년 같은 기간보다 "
              f"{short_won(abs(diff))} {'많습니다' if diff >= 0 else '적습니다'}.")
        if not rows:
            return s1 + " 작년보다 늘어난 계정은 없습니다."
        top = rows[0]
        s2 = (f" 가장 많이 늘어난 계정은 {_j(top['계정'], '으로')} {short_won(top['증가'])} 늘었고, 원인은 "
              f"{top['원인'].split(' · ')[0]}입니다.")
        s3 = (" 그다음은 " + ", ".join(f"{r['계정']}(+{short_won(r['증가'])})" for r in rows[1:3]) + "입니다."
              if len(rows) > 1 else "")
        return s1 + s2 + s3
    if tool == "recurring_status":
        items = data["items"]
        ok = [i for i in items if i["상태"] in ("전기 완료", "수동 확인", "이번 달 해당 없음")]
        late = [i for i in items if i["상태"] == "기한 경과"]
        diff = [i for i in items if i["상태"] == "금액 상이"]
        wait = [i for i in items if i["상태"] in ("수신 대기",)]
        unposted = [i for i in items if i["상태"] == "수신·미전기"]
        text = f"{mo} 정기 전표 {len(items)}건 중 {len(ok)}건이 처리됐습니다."
        if late:
            text += (" " + _j(", ".join(f"{i['항목']}(기한 {i['기한'][5:].replace('-', '/')})" for i in late), "은는")
                     + " 기한이 지났는데 아직 들어오지 않았으니 공급자에게 발행을 요청하세요.")
        if diff:
            text += " " + _j(", ".join(i["항목"] for i in diff), "은는") + " 예상 금액과 달라 사유 확인이 필요합니다."
        if unposted:
            text += " " + _j(", ".join(i["항목"] for i in unposted), "은는") + " 계산서는 들어왔으니 검토·전기에서 전기하면 됩니다."
        if wait:
            text += f" 나머지 {len(wait)}건은 기한 전이라 기다리면 됩니다."
        return text
    if tool == "annual_status":
        rows = data["this_month"]
        if not rows:
            text = f"{mo}에는 처리할 연간 항목이 없습니다."
        else:
            parts = [f"{r['항목']}({r['상태']}" + (f", {short_won(r['처리 금액'])}" if r.get("처리 금액") else
                                                f", 예상 {short_won(r['예상'])}") + ")" for r in rows]
            text = f"{mo} 연간 항목은 " + ", ".join(parts) + "입니다."
            if any(r["상태"] != "처리 완료" for r in rows):
                text += " 아직 처리하지 않은 항목은 납부 후 전표 점검 › 연간 항목에서 체크해 주세요."
        if data["next_month"]:
            text += " 다음 달에는 " + _j(", ".join(r["항목"] for r in data["next_month"]), "이가") + " 있습니다."
        return text
    acc = data["accounts"]
    over = [r for r in acc if r["상태"] == "초과"]
    risk = [r for r in acc if r["상태"] == "초과 예상"]
    b, f = data["budget"], data["forecast"]
    text = (f"{mo} 총무 예산 {short_won(b)} 중 {_j(short_won(data['actual']), '을를')} 썼고, 월말에는 {short_won(f)}"
            f"(예산의 {f / b:.0%})이 예상됩니다." if b else f"{mo} 총무 예산이 편성되지 않았습니다.")
    if over:
        worst = max(over, key=lambda r: r["사용"] - r["예산"])
        text += (f" 초과 {len(over)}건 중 가장 큰 건은 {worst['계정']}"
                 f"(예산보다 {short_won(worst['사용'] - worst['예산'])} 초과)입니다.")
    if risk:
        text += " 초과가 예상되는 계정은 " + ", ".join(r["계정"] for r in risk) + "입니다."
    return text


def ask(conn: sqlite3.Connection, question: str, base_month: str, today: date | None = None) -> Answer:
    accounts = [r[0] for r in conn.execute("SELECT name FROM gl_accounts WHERE owner='총무'")]
    tool, args = route_rule(question, base_month, accounts)
    source = "rule"
    if ai.status().available:
        schema = {"type": "object", "properties": {
            "tool": {"type": "string", "enum": list(TOOLS)},
            "month": {"type": "string", "pattern": r"^20\d{2}-\d{2}$"},
            "account": {"type": "string", "enum": accounts + [""]},
            "scope": {"type": "string", "enum": ["month", "ytd"]}},
            "required": ["tool", "month", "account", "scope"]}
        prompt = (f"오늘 기준 월은 {base_month}입니다. 질문에 답하려면 어떤 조회 함수를 써야 하는지 고르세요.\n"
                  + "\n".join(f"- {k}: {v}" for k, v in TOOLS.items()) + f"\n\n질문: {question}")
        try:
            r = ai.chat([{"role": "user", "content": prompt}], schema=schema)
            tool, args = r["tool"], {"month": r["month"], "account": r["account"], "scope": r["scope"]}
            source = "ai"
        except ai.AIUnavailable:
            pass
    data, table = run_tool(conn, tool, args, today)
    text = _example_answer(tool, data, args)       # 로컬 AI 미연결: 예시 답안
    if source == "ai":
        prompt = ("아래 조회 결과(JSON)만 근거로 질문에 한국어로 2~4문장 답하세요. 숫자는 결과에 있는 값만 쓰고, "
                  "금액은 '억·만원'으로. 결과에 없으면 모른다고 하세요.\n\n"
                  f"질문: {question}\n조회 함수: {tool} {json.dumps(args, ensure_ascii=False)}\n결과: "
                  + json.dumps(data, ensure_ascii=False, default=str)[:6000])
        try:
            text = ai.chat([{"role": "user", "content": prompt}], temperature=0.1)
        except ai.AIUnavailable:
            source = "example"
    else:
        source = "example"
    conn.execute("INSERT INTO ai_log(feature, month, source, input_json, output_json, created_at) VALUES "
                 "('qa',?,?,?,?,?)", (args.get("month"), source,
                                      json.dumps({"q": question, "tool": tool, "args": args}, ensure_ascii=False),
                                      text, now()))
    return Answer(text, tool, args, source, table)
