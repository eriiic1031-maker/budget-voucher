"""수집 + 처리 배치. Windows 작업 스케줄러 등으로 주기 실행.

    .venv\\Scripts\\python collect.py              # 메일함 수집 (10~30분 간격 권장)
    .venv\\Scripts\\python collect.py --hometax    # 홈택스 수집·대사: 전월+당월 (하루 1~2회 권장)
    .venv\\Scripts\\python collect.py 폴더경로     # 폴더 안의 XML 일괄 처리
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

from voucher.db import connect, init_db
from voucher.pipeline import ingest_xml


def collect_hometax() -> None:
    from voucher.hometax import sync

    today = date.today()
    start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)   # 전월 1일 (지연 발행분 포함)
    with connect() as conn:
        r = sync(conn, start, today, actor="batch")
    print(f"홈택스 {start}~{today}: 조회 {r.fetched}건, 미수집 {r.new_in_hometax}건, 등록 {r.ingested}건")
    for e in r.errors:
        print("  실패:", e)


def collect_files(files: list[tuple[Path, bytes, str]], source: str) -> None:
    with connect() as conn:
        for path, xml, subject in files:
            r = ingest_xml(conn, xml, source, str(path))
            print(f"{path.name:40s} {r.status:12s} {subject}")
    print(f"처리 {len(files)}건")


def main() -> None:
    init_db()
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg == "--hometax":
        collect_hometax()
    elif arg:
        collect_files([(p, p.read_bytes(), "") for p in sorted(Path(arg).glob("*.xml"))], "folder")
    else:
        from voucher.mail import fetch_xml_attachments
        collect_files(fetch_xml_attachments(), "mail")


if __name__ == "__main__":
    main()
