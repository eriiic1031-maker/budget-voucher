"""증빙자료(첨부파일) 보관 — 전표(세금계산서)와 연간 항목 처리에 붙인다.

- 파일은 data/attachments/<대상>/<키>/ 에 저장하고, DB 에는 이름·형식·크기·해시·저장 경로만 남긴다.
- 같은 대상에 같은 내용(해시)의 파일은 한 번만 저장한다.
- 허용 형식: PDF, 이미지(JPG·PNG·WEBP·GIF), 엑셀, 워드, 한글(HWP), 텍스트, ZIP / 파일당 20MB.
운영에서 SAP 에 증빙을 남기려면 GOS(Generic Object Services) 첨부나 사내 문서 서버로 옮기면 된다.
"""
from __future__ import annotations

import hashlib
import io
import mimetypes
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .config import settings
from .db import audit, now

ALLOWED = {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".gif", ".xlsx", ".xls", ".csv", ".docx", ".hwp", ".hwpx",
           ".txt", ".zip", ".xml"}
IMAGE = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_BYTES = 20 * 1024 * 1024


class AttachmentError(ValueError):
    pass


@dataclass
class Attachment:
    id: int
    target_type: str
    target_key: str
    filename: str
    content_type: str
    size: int
    stored_path: str
    note: str | None
    uploaded_by: str
    uploaded_at: str

    @property
    def ext(self) -> str:
        return Path(self.filename).suffix.lower()

    @property
    def is_image(self) -> bool:
        return self.ext in IMAGE

    @property
    def is_pdf(self) -> bool:
        return self.ext == ".pdf"

    @property
    def size_label(self) -> str:
        return f"{self.size / 1024:,.0f}KB" if self.size < 1024 * 1024 else f"{self.size / 1024 / 1024:,.1f}MB"


def root() -> Path:
    return Path(settings.db_path).parent / "attachments"


def _safe(name: str) -> str:
    name = Path(name).name
    return re.sub(r"[^\w가-힣.\-() ]", "_", name)[:120] or "file"


def save(conn: sqlite3.Connection, target_type: str, target_key: str | int, filename: str, data: bytes,
         actor: str, note: str = "") -> int:
    """저장하고 id 를 돌려준다. 같은 내용이 이미 있으면 그 id."""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED:
        raise AttachmentError(f"{ext or '확장자 없음'} 형식은 올릴 수 없습니다 (PDF·이미지·엑셀·워드·한글·ZIP)")
    if len(data) > MAX_BYTES:
        raise AttachmentError("파일당 20MB까지 올릴 수 있습니다")
    if not data:
        raise AttachmentError("빈 파일입니다")
    key = str(target_key)
    digest = hashlib.sha256(data).hexdigest()
    dup = conn.execute("SELECT id FROM attachments WHERE target_type=? AND target_key=? AND sha256=?",
                       (target_type, key, digest)).fetchone()
    if dup:
        return dup[0]
    folder = root() / target_type / key.replace(":", "_")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{digest[:8]}_{_safe(filename)}"
    path.write_bytes(data)
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    cur = conn.execute(
        "INSERT INTO attachments(target_type, target_key, filename, content_type, size, sha256, stored_path, note,"
        " uploaded_by, uploaded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (target_type, key, Path(filename).name, ctype, len(data), digest, str(path), note or None, actor, now()))
    if target_type == "invoice":
        audit(conn, int(key), "ATTACH", {"file": Path(filename).name, "size": len(data)}, actor)
    return cur.lastrowid


def list_for(conn: sqlite3.Connection, target_type: str, target_key: str | int) -> list[Attachment]:
    rows = conn.execute("SELECT * FROM attachments WHERE target_type=? AND target_key=? ORDER BY id",
                        (target_type, str(target_key))).fetchall()
    return [Attachment(r["id"], r["target_type"], r["target_key"], r["filename"], r["content_type"], r["size"],
                       r["stored_path"], r["note"], r["uploaded_by"], r["uploaded_at"]) for r in rows]


def counts(conn: sqlite3.Connection, target_type: str = "invoice") -> dict[str, int]:
    return dict(conn.execute("SELECT target_key, COUNT(*) FROM attachments WHERE target_type=? GROUP BY target_key",
                             (target_type,)).fetchall())


def read_bytes(a: Attachment) -> bytes | None:
    p = Path(a.stored_path)
    return p.read_bytes() if p.exists() else None


def delete(conn: sqlite3.Connection, attachment_id: int, actor: str) -> None:
    r = conn.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
    if not r:
        return
    conn.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
    Path(r["stored_path"]).unlink(missing_ok=True)
    if r["target_type"] == "invoice":
        audit(conn, int(r["target_key"]), "DETACH", {"file": r["filename"]}, actor)


def preview_png(a: Attachment, data: bytes | None = None) -> bytes | None:
    """이미지는 그대로, PDF 는 첫 쪽을 PNG 로 (브라우저 PDF 뷰어 없이 미리보기)."""
    data = data or read_bytes(a)
    if not data:
        return None
    if a.is_image:
        return data
    if a.is_pdf:
        try:
            import pypdfium2 as pdfium
            buf = io.BytesIO()
            pdfium.PdfDocument(data)[0].render(scale=1.3).to_pil().save(buf, format="PNG")
            return buf.getvalue()
        except Exception:
            return None
    return None
