"""화면 전용 커스텀 컴포넌트 (Streamlit Custom Components v2).

- task_cards : 카드 전체를 눌러 해당 화면으로 이동하는 '오늘 할 일' 카드
- month_calendar : 날짜를 누르면 그날 일정이 아래에 펼쳐지는 월 달력
- pdf_print_buttons : 보고서 PDF 를 바로 인쇄하거나 새 탭에서 여는 버튼
모두 --st-* 테마 변수를 써서 라이트/다크 테마를 따라간다.
"""
from __future__ import annotations

import streamlit as st

# ------------------------------------------------------------------ 할 일 카드 ----
_CARDS_CSS = """
.grid { display: flex; flex-wrap: wrap; gap: 12px; font-family: var(--st-font); }
.card {
  all: unset; box-sizing: border-box; width: 222px; min-height: 148px; padding: 14px 16px;
  display: flex; flex-direction: column; gap: 6px; cursor: pointer;
  background: var(--st-background-color); color: var(--st-text-color);
  border: 1px solid var(--st-border-color); border-radius: var(--st-base-radius);
  transition: border-color .15s, box-shadow .15s, transform .15s;
}
.card:hover { border-color: var(--st-primary-color); box-shadow: 0 4px 14px rgba(0,0,0,.08); transform: translateY(-2px); }
.card:focus-visible { outline: 2px solid var(--st-primary-color); outline-offset: 2px; }
.card.zero { opacity: .62; }
.title { font-weight: 600; font-size: .92rem; display: flex; gap: 6px; align-items: center; }
.count { font-size: 1.55rem; font-weight: 700; line-height: 1.2; }
.desc { font-size: .78rem; color: var(--st-gray-color); line-height: 1.35; flex: 1; }
.go { font-size: .78rem; color: var(--st-primary-color); font-weight: 600; }
.red { color: var(--st-red-color); } .orange { color: var(--st-orange-color); }
.blue { color: var(--st-blue-color); } .violet { color: var(--st-violet-color); }
.green { color: var(--st-green-color); } .gray { color: var(--st-gray-color); }
"""

_CARDS_JS = """
export default function (component) {
  const { data, parentElement, setTriggerValue } = component
  const root = parentElement.querySelector(".grid")
  if (!root) return
  root.innerHTML = ""
  for (const c of (data && data.cards) || []) {
    const b = document.createElement("button")
    b.type = "button"
    b.className = "card" + (c.count ? "" : " zero")
    b.setAttribute("aria-label", c.title + " " + c.count + "건, " + c.go)
    b.innerHTML =
      '<div class="title"><span>' + c.icon + '</span><span>' + c.title + '</span></div>' +
      '<div class="count ' + (c.count ? c.tone : "gray") + '">' + c.count + '건</div>' +
      '<div class="desc">' + c.desc + '</div>' +
      '<div class="go">' + c.go + ' →</div>'
    b.onclick = () => setTriggerValue("clicked", c.id)
    root.appendChild(b)
  }
}
"""

_CARDS = st.components.v2.component("task_cards", html='<div class="grid"></div>', css=_CARDS_CSS, js=_CARDS_JS)


def task_cards(cards: list[dict], *, key: str) -> str | None:
    """cards: [{id, icon, title, count, desc, go, tone}] → 눌린 카드 id (없으면 None)."""
    res = _CARDS(data={"cards": cards}, key=key, on_clicked_change=lambda: None)
    return res.clicked


# ------------------------------------------------------------------ 월 달력 ----
_CAL_CSS = """
.wrap { font-family: var(--st-font); color: var(--st-text-color); }
.legend { display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: .78rem; margin-bottom: 10px; color: var(--st-gray-color); }
.legend span::before { content: ""; display: inline-block; width: 10px; height: 10px; border-radius: 3px; margin-right: 5px; vertical-align: -1px; background: var(--c); }
.grid { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 4px; }
.dow { font-size: .78rem; font-weight: 600; text-align: center; padding: 4px 0; color: var(--st-gray-color); }
.dow.sun { color: var(--st-red-color); } .dow.sat { color: var(--st-blue-color); }
.day {
  all: unset; box-sizing: border-box; min-height: 92px; padding: 6px; cursor: pointer;
  display: flex; flex-direction: column; gap: 3px; overflow: hidden;
  border: 1px solid var(--st-border-color); border-radius: 8px; background: var(--st-background-color);
}
.day:hover { border-color: var(--st-primary-color); }
.day:focus-visible { outline: 2px solid var(--st-primary-color); }
.day.empty { visibility: hidden; cursor: default; }
.day.sel { border: 2px solid var(--st-primary-color); }
.day.today .num { background: var(--st-primary-color); color: var(--st-background-color); border-radius: 999px; padding: 0 7px; }
.num { font-size: .8rem; font-weight: 600; align-self: flex-start; }
.num.sun, .num.hol { color: var(--st-red-color); } .num.sat { color: var(--st-blue-color); }
.chip { font-size: .7rem; line-height: 1.25; padding: 2px 5px; border-radius: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        background: var(--bg); color: var(--fg); }
.more { font-size: .7rem; color: var(--st-gray-color); }
.detail { margin-top: 12px; border: 1px solid var(--st-border-color); border-radius: var(--st-base-radius); padding: 12px 14px; background: var(--st-secondary-background-color); }
.detail h4 { margin: 0 0 8px; font-size: .95rem; }
.detail ul { margin: 0; padding-left: 0; list-style: none; display: flex; flex-direction: column; gap: 6px; }
.detail li { font-size: .85rem; display: flex; gap: 8px; align-items: baseline; }
.tag { font-size: .7rem; padding: 1px 6px; border-radius: 4px; background: var(--bg); color: var(--fg); white-space: nowrap; }
.sub { color: var(--st-gray-color); font-size: .78rem; }
.none { color: var(--st-gray-color); font-size: .85rem; }
@media (max-width: 640px) { .chip { display: none; } .day { min-height: 54px; } .day .dot { display: block; } }
.dot { display: none; width: 6px; height: 6px; border-radius: 99px; background: var(--st-primary-color); }
"""

_CAL_JS = """
const TYPES = {
  rec:     { name: "정기 전표 기한",   fg: "var(--st-blue-text-color)",   bg: "var(--st-blue-background-color)",   c: "var(--st-blue-color)" },
  annual:  { name: "연간 항목",       fg: "var(--st-violet-text-color)", bg: "var(--st-violet-background-color)", c: "var(--st-violet-color)" },
  ly:      { name: "작년 이 시기 지출", fg: "var(--st-orange-text-color)", bg: "var(--st-orange-background-color)", c: "var(--st-orange-color)" },
  tax:     { name: "세무·마감 일정",   fg: "var(--st-green-text-color)",  bg: "var(--st-green-background-color)",  c: "var(--st-green-color)" },
  holiday: { name: "공휴일",          fg: "var(--st-red-text-color)",    bg: "var(--st-red-background-color)",    c: "var(--st-red-color)" },
}
const DOWS = ["일", "월", "화", "수", "목", "금", "토"]
const selected = new WeakMap()   // 달력마다 선택한 날짜 (다시 그려도 유지)

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]))
}

function draw(component) {
  const { data, parentElement } = component
  const root = parentElement.querySelector(".wrap")
  if (!root || !data) return
  const { year, month, today, days } = data
  const first = new Date(year, month - 1, 1).getDay()
  const last = new Date(year, month, 0).getDate()
  const prefix = year + "-" + String(month).padStart(2, "0")

  let sel = selected.get(parentElement)
  if (!sel || sel.prefix !== prefix) {
    const t = today && today.startsWith(prefix) ? Number(today.slice(8)) : null
    const firstEvent = Object.keys(days).map(Number).sort((a, b) => a - b)[0]
    sel = { prefix, day: t || firstEvent || 1 }
    selected.set(parentElement, sel)
  }

  let html = '<div class="legend">' +
    Object.values(TYPES).map(t => '<span style="--c:' + t.c + '">' + t.name + '</span>').join("") +
    '</div><div class="grid">'
  DOWS.forEach((d, i) => { html += '<div class="dow ' + (i === 0 ? "sun" : i === 6 ? "sat" : "") + '">' + d + '</div>' })
  for (let i = 0; i < first; i++) html += '<div class="day empty"></div>'
  for (let d = 1; d <= last; d++) {
    const evs = days[d] || []
    const dow = (first + d - 1) % 7
    const iso = prefix + "-" + String(d).padStart(2, "0")
    const cls = ["day", iso === today ? "today" : "", d === sel.day ? "sel" : ""].join(" ")
    const numCls = ["num", dow === 0 ? "sun" : dow === 6 ? "sat" : "", evs.some(e => e.type === "holiday") ? "hol" : ""].join(" ")
    let chips = ""
    evs.slice(0, 3).forEach(e => {
      const t = TYPES[e.type]
      chips += '<div class="chip" style="--fg:' + t.fg + ';--bg:' + t.bg + '" title="' + esc(e.label) + '">' + esc(e.short) + '</div>'
    })
    if (evs.length > 3) chips += '<div class="more">+' + (evs.length - 3) + '건 더</div>'
    if (evs.length) chips += '<div class="dot"></div>'
    html += '<button type="button" class="' + cls + '" data-d="' + d + '" aria-label="' + month + '월 ' + d + '일 일정 ' + evs.length + '건">' +
            '<span class="' + numCls + '">' + d + '</span>' + chips + '</button>'
  }
  html += '</div>'

  const evs = days[sel.day] || []
  html += '<div class="detail"><h4>' + month + '월 ' + sel.day + '일 (' + DOWS[(first + sel.day - 1) % 7] + ')</h4>'
  if (!evs.length) {
    html += '<div class="none">이날은 표시할 일정이 없습니다.</div>'
  } else {
    html += '<ul>' + evs.map(e => {
      const t = TYPES[e.type]
      return '<li><span class="tag" style="--fg:' + t.fg + ';--bg:' + t.bg + '">' + t.name + '</span><span>' + esc(e.label) +
             (e.detail ? ' <span class="sub">· ' + esc(e.detail) + '</span>' : '') + '</span></li>'
    }).join("") + '</ul>'
  }
  html += '</div>'
  root.innerHTML = html
  root.querySelectorAll(".day[data-d]").forEach(b => {
    b.onclick = () => { sel.day = Number(b.dataset.d); draw(component) }   // 날짜 선택은 화면 안에서만 (서버 재실행 없음)
  })
}

export default function (component) {
  draw(component)
}
"""

_CAL = st.components.v2.component("month_calendar", html='<div class="wrap"></div>', css=_CAL_CSS, js=_CAL_JS)


def month_calendar(year: int, month: int, today: str, days: dict[int, list[dict]], *, key: str) -> None:
    """days: {일: [{type, short, label, detail}]} — type: rec / annual / ly / tax / holiday"""
    _CAL(data={"year": year, "month": month, "today": today, "days": {str(k): v for k, v in days.items()}}, key=key)


# ------------------------------------------------------------------ PDF 인쇄 ----
_PRINT_CSS = """
.row { display: flex; gap: 8px; flex-wrap: wrap; font-family: var(--st-font); }
button {
  all: unset; box-sizing: border-box; cursor: pointer; padding: 6px 14px; min-height: 40px;
  display: inline-flex; align-items: center; gap: 6px; font-size: .95rem;
  color: var(--st-text-color); background: var(--st-background-color);
  border: 1px solid var(--st-border-color); border-radius: var(--st-button-radius, var(--st-base-radius));
}
button:hover { border-color: var(--st-primary-color); color: var(--st-primary-color); }
button:focus-visible { outline: 2px solid var(--st-primary-color); outline-offset: 2px; }
.msg { font-size: .78rem; color: var(--st-gray-color); align-self: center; }
"""

_PRINT_JS = """
function blobUrl(b64) {
  const bin = atob(b64), bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  return URL.createObjectURL(new Blob([bytes], { type: "application/pdf" }))
}

export default function (component) {
  const { data, parentElement } = component
  const root = parentElement.querySelector(".row")
  if (!root || !data) return
  root.innerHTML = '<button type="button" class="print">🖨️ 인쇄</button>' +
                   '<button type="button" class="open">↗ 새 탭에서 열기</button><span class="msg"></span>'
  const msg = root.querySelector(".msg")
  root.querySelector(".print").onclick = () => {
    const url = blobUrl(data.pdf)
    const old = document.getElementById("report-print-frame")
    if (old) old.remove()
    const f = document.createElement("iframe")
    f.id = "report-print-frame"
    f.style.cssText = "position:fixed;right:0;bottom:0;width:0;height:0;border:0;visibility:hidden"
    f.onload = () => setTimeout(() => {
      try { f.contentWindow.focus(); f.contentWindow.print() }
      catch (e) { window.open(url, "_blank"); msg.textContent = "새 탭에서 Ctrl+P로 인쇄하세요." }
    }, 400)
    f.src = url
    document.body.appendChild(f)
  }
  root.querySelector(".open").onclick = () => {
    window.open(blobUrl(data.pdf), "_blank")
    msg.textContent = "새 탭의 PDF 화면에서 인쇄(Ctrl+P)할 수 있습니다."
  }
}
"""

_PRINT = st.components.v2.component("pdf_print", html='<div class="row"></div>', css=_PRINT_CSS, js=_PRINT_JS)


def pdf_print_buttons(pdf: bytes, *, key: str) -> None:
    """브라우저 인쇄 창을 바로 여는 '인쇄' 버튼 + PDF 를 새 탭에서 여는 버튼."""
    import base64
    _PRINT(data={"pdf": base64.b64encode(pdf).decode()}, key=key)
