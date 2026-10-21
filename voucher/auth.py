"""사용자/관리자 계정.

- 관리자(admin): 전기, 기준정보, 홈택스 수집, 사용자 관리 등 전체 기능
- 사용자(user) : 내 부서 비용 조회, 검토 대기 건의 분개 수정 후 '승인 요청', 정기 전표 확인

비밀번호는 PBKDF2-SHA256(솔트 포함)으로만 저장한다.
운영 시에는 사내 SSO(st.login + OIDC)로 바꾸고 이 테이블은 권한(role) 매핑 용도로만 쓰는 것을 권장.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
from dataclasses import dataclass

ITERATIONS = 200_000
ROLE_LABEL = {"admin": "관리자", "user": "사용자"}


@dataclass(frozen=True)
class User:
    user_id: str
    name: str
    role: str
    cost_center: str | None
    cost_center_name: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def role_label(self) -> str:
        return ROLE_LABEL.get(self.role, self.role)


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
    return hmac.compare_digest(dk.hex(), hash_hex)


def _to_user(row: sqlite3.Row) -> User:
    return User(row["user_id"], row["name"], row["role"], row["cost_center"], row["cc_name"])


_SELECT = ("SELECT u.*, c.name AS cc_name FROM users u LEFT JOIN cost_centers c ON c.code = u.cost_center "
           "WHERE u.user_id = ?")


def authenticate(conn: sqlite3.Connection, user_id: str, password: str) -> User | None:
    row = conn.execute(_SELECT, (user_id.strip(),)).fetchone()
    if not row or not row["active"] or not verify_password(password, row["password_hash"]):
        return None
    return _to_user(row)


def get_user(conn: sqlite3.Connection, user_id: str) -> User | None:
    row = conn.execute(_SELECT, (user_id,)).fetchone()
    return _to_user(row) if row and row["active"] else None


def upsert_user(conn: sqlite3.Connection, user_id: str, name: str, role: str, cost_center: str | None,
                password: str | None = None, active: bool = True) -> None:
    if role not in ROLE_LABEL:
        raise ValueError(f"알 수 없는 권한: {role}")
    exists = conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if exists:
        conn.execute("UPDATE users SET name=?, role=?, cost_center=?, active=? WHERE user_id=?",
                     (name, role, cost_center, int(active), user_id))
        if password:
            conn.execute("UPDATE users SET password_hash=? WHERE user_id=?", (hash_password(password), user_id))
    else:
        if not password:
            raise ValueError("새 사용자는 초기 비밀번호가 필요합니다")
        conn.execute("INSERT INTO users(user_id, name, role, cost_center, password_hash, active) VALUES (?,?,?,?,?,?)",
                     (user_id, name, role, cost_center, hash_password(password), int(active)))


def change_password(conn: sqlite3.Connection, user_id: str, old: str, new: str) -> bool:
    row = conn.execute("SELECT password_hash FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if not row or not verify_password(old, row["password_hash"]):
        return False
    conn.execute("UPDATE users SET password_hash=? WHERE user_id=?", (hash_password(new), user_id))
    return True
