"""IMAP 메일함에서 전자세금계산서 XML 첨부(또는 zip 안의 XML)를 수집한다.

- 읽지 않은 메일만 보고, 처리한 메일은 읽음 처리된다.
- 첨부 원본은 data/inbox/YYYYMM/ 에 보관해 감사 추적이 가능하게 한다.
- 메일에 XML 없이 '조회 링크'만 오는 발행사는 이 방식으로 못 받는다 → 홈택스 연동 API 로 보완.
"""
from __future__ import annotations

import email
import imaplib
import io
import zipfile
from datetime import datetime
from email.header import decode_header, make_header
from pathlib import Path
from typing import Iterator

from .config import settings


def _decode(s: str | None) -> str:
    return str(make_header(decode_header(s))) if s else ""


def _xml_parts(msg: email.message.Message) -> Iterator[tuple[str, bytes]]:
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        name = _decode(part.get_filename())
        payload = part.get_payload(decode=True)
        if not name or not payload:
            continue
        low = name.lower()
        if low.endswith(".xml"):
            yield name, payload
        elif low.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                for zi in zf.infolist():
                    if zi.filename.lower().endswith(".xml") and zi.file_size < 5_000_000:
                        yield zi.filename, zf.read(zi)


def fetch_xml_attachments(save_dir: Path | None = None) -> list[tuple[Path, bytes, str]]:
    """(저장경로, XML bytes, 메일제목) 목록을 반환."""
    if not (settings.imap_host and settings.imap_user and settings.imap_password):
        raise RuntimeError("IMAP_HOST / IMAP_USER / IMAP_PASSWORD 를 .env 에 설정하세요")

    save_dir = (save_dir or settings.inbox_dir) / datetime.now().strftime("%Y%m")
    save_dir.mkdir(parents=True, exist_ok=True)
    out = []

    with imaplib.IMAP4_SSL(settings.imap_host) as imap:
        imap.login(settings.imap_user, settings.imap_password)
        imap.select(settings.imap_folder)
        _, data = imap.search(None, "UNSEEN")
        for num in data[0].split():
            _, msg_data = imap.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            subject = _decode(msg.get("Subject"))
            for name, xml in _xml_parts(msg):
                safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in Path(name).name)
                path = save_dir / f"{datetime.now():%Y%m%d%H%M%S}_{num.decode()}_{safe}"
                path.write_bytes(xml)
                out.append((path, xml, subject))
            imap.store(num, "+FLAGS", "\\Seen")
    return out
