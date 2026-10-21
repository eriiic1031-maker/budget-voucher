"""증빙자료 화면 조각 — 검토·전기, 세금계산서 조회, 연간 항목에서 같이 쓴다."""
from __future__ import annotations

import streamlit as st

from .attachments import ALLOWED, AttachmentError, delete, list_for, preview_png, read_bytes, save
from .db import connect
from .ui import time_ago


def attachment_panel(target_type: str, target_key: str | int, actor: str, *, can_edit: bool = True,
                     key: str, title: str = "📎 증빙자료", hint: str = "") -> int:
    """첨부 목록 + 올리기. 첨부 개수를 돌려준다."""
    with connect() as conn:
        files = list_for(conn, target_type, target_key)
    st.markdown(f"**{title}** · {len(files)}개" + (f"  \n:gray[{hint}]" if hint else ""))

    for a in files:
        with st.container(border=True):
            with st.container(horizontal=True, vertical_alignment="center"):
                icon = "🖼️" if a.is_image else "📄" if a.is_pdf else "📎"
                st.markdown(f"{icon} **{a.filename}**")
                st.caption(f"{a.size_label} · {a.uploaded_by} · {time_ago(a.uploaded_at)}"
                           + (f" · {a.note}" if a.note else ""))
                st.space("stretch")
                data = read_bytes(a)
                if data is not None:
                    st.download_button("받기", data, file_name=a.filename, mime=a.content_type,
                                       icon=":material/download:", key=f"{key}_dl_{a.id}", type="tertiary")
                if can_edit and st.button("삭제", icon=":material/delete:", key=f"{key}_del_{a.id}", type="tertiary"):
                    with connect() as conn:
                        delete(conn, a.id, actor)
                    st.rerun()
            if (a.is_image or a.is_pdf) and data is not None:
                with st.expander("미리보기", expanded=False):
                    png = preview_png(a, data)
                    if png:
                        st.image(png, caption="PDF 첫 쪽" if a.is_pdf else None,
                                 alt=f"{a.filename} 미리보기", width="stretch")
                    else:
                        st.caption("미리보기를 만들 수 없는 파일입니다. 받아서 열어 주세요.")
            elif data is None:
                st.caption(":red[저장된 파일을 찾을 수 없습니다 (서버가 다시 시작되어 지워졌을 수 있음).]")

    if can_edit:
        up = st.file_uploader("증빙 올리기 (견적서·계약서·거래명세서·영수증·사진)",
                              type=[e.lstrip(".") for e in sorted(ALLOWED)], accept_multiple_files=True,
                              key=f"{key}_up", help="PDF, JPG, PNG, 엑셀, 워드, 한글(HWP), ZIP · 파일당 20MB")
        if up:
            note = st.text_input("메모 (선택)", key=f"{key}_note", placeholder="예: 견적서(3사 비교 중 최저가)")
            if st.button(f"{len(up)}개 저장", type="primary", icon=":material/attach_file:", key=f"{key}_save"):
                errs = []
                with connect() as conn:
                    for f in up:
                        try:
                            save(conn, target_type, target_key, f.name, f.getvalue(), actor, note.strip())
                        except AttachmentError as e:
                            errs.append(f"{f.name}: {e}")
                if errs:
                    st.error("\n".join(errs))
                else:
                    st.session_state.pop(f"{key}_up", None)
                    st.toast(f"증빙 {len(up)}개를 저장했습니다")
                    st.rerun()
    return len(files)
