"""환경 설정. 값은 환경변수(.env)로 덮어쓸 수 있다. 비밀번호류는 코드에 넣지 않는다."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(default_factory=lambda: Path(_env("DB_PATH", str(BASE_DIR / "data" / "voucher.db"))))
    inbox_dir: Path = field(default_factory=lambda: Path(_env("INBOX_DIR", str(BASE_DIR / "data" / "inbox"))))

    # 자사 정보
    company_bizno: str = _env("COMPANY_BIZNO", "1018199999")
    company_code: str = _env("SAP_COMPANY_CODE", "1000")
    currency: str = "KRW"

    # SAP 연결: mock(로컬 시뮬레이션) | rfc(pyrfc로 BAPI 호출)
    sap_mode: str = _env("SAP_MODE", "mock")
    sap_user: str = _env("SAP_USER", "VOUCHER_BOT")

    # SAP 전표 유형 / 세금코드 / 계정
    doc_type_invoice: str = _env("SAP_DOC_TYPE_INVOICE", "KR")   # 매입 송장
    doc_type_credit: str = _env("SAP_DOC_TYPE_CREDIT", "KG")     # 매입 대변메모(수정세금계산서 -)
    tax_code_deductible: str = _env("SAP_TAX_DEDUCTIBLE", "V1")  # 매입 10% 공제
    tax_code_nondeductible: str = _env("SAP_TAX_NONDEDUCTIBLE", "V3")  # 매입 10% 불공제
    tax_code_exempt: str = _env("SAP_TAX_EXEMPT", "V0")          # 면세(계산서)
    vat_input_gl: str = _env("SAP_VAT_INPUT_GL", "11350000")     # 부가세대급금

    # 규칙에 auto_post=1 이고 모든 검증을 통과하면 사람 승인 없이 바로 전기
    auto_post: bool = _env("AUTO_POST", "false").lower() == "true"

    # 메일 수집 (IMAP)
    imap_host: str = _env("IMAP_HOST", "")
    imap_user: str = _env("IMAP_USER", "")
    imap_password: str = _env("IMAP_PASSWORD", "")
    imap_folder: str = _env("IMAP_FOLDER", "INBOX")

    # 홈택스 매입 세금계산서 수집: mock(데모) | popbill(팝빌 홈택스 연동 API)
    hometax_provider: str = _env("HOMETAX_PROVIDER", "mock")
    popbill_link_id: str = _env("POPBILL_LINK_ID", "")
    popbill_secret_key: str = _env("POPBILL_SECRET_KEY", "")
    popbill_is_test: bool = _env("POPBILL_IS_TEST", "true").lower() == "true"
    popbill_user_id: str = _env("POPBILL_USER_ID", "")


settings = Settings()
