"""로컬 AI(Ollama) 연결.

[보안 정책] 로컬 AI 가 아니면 연동하지 않는다.
  회사 재무 데이터(거래처·사업자번호·금액·계정)가 외부로 나가는 것을 막기 위해, 외부 클라우드 AI API 는
  학습 미사용 기업용이라도, 사업자번호를 가리더라도 쓰지 않는다. 내 PC 또는 사내망(사설 IP)의 Ollama 만 허용하고,
  OLLAMA_URL 이 외부 주소이면 연결 자체를 막는다.

- 회사 재무 데이터가 외부로 나가지 않도록 내 PC/사내 서버의 Ollama 만 쓴다.
- 기본 모델은 qwen2.5vl:3b (글·이미지 모두 가능, 한국어 무난, 3.2GB). .env 의 OLLAMA_MODEL 로 바꾼다.
- AI 를 못 쓰는 상황(Ollama 꺼짐, 메모리 부족, 클라우드 데모)에서는 AIUnavailable 을 던지고,
  호출하는 쪽이 규칙·과거 기록 방식으로 대신한다 → 화면과 기능은 같다.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")
MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5vl:3b")
VISION_MODEL = os.environ.get("OLLAMA_VISION_MODEL", MODEL)
AI_MODE = os.environ.get("AI_MODE", "auto").lower()      # auto | off
TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "180"))


def is_local_address(url: str) -> bool:
    """내 PC(loopback) 또는 사내망(사설 IP·.local·.internal)이면 True. 외부 주소는 False."""
    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost",) or host.endswith((".local", ".internal", ".lan")):
        return True
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            ips = [ipaddress.ip_address(i[4][0]) for i in socket.getaddrinfo(host, None)]
        except OSError:
            return False
    return bool(ips) and all(ip.is_loopback or ip.is_private for ip in ips)


POLICY = ("로컬 AI가 아니면 연동하지 않습니다. 회사 재무 데이터가 외부로 나가지 않도록 내 PC 또는 사내망의 "
          "Ollama만 쓰고, 외부 클라우드 AI API는 쓰지 않습니다.")


class AIUnavailable(RuntimeError):
    """AI 를 쓸 수 없음 → 규칙 방식으로 대체."""


@dataclass
class Status:
    available: bool
    model: str
    reason: str

    @property
    def label(self) -> str:
        return f"로컬 AI 연결됨 · {self.model}" if self.available else f"규칙 방식으로 동작 ({self.reason})"


_status_cache: tuple[float, Status] | None = None
FAIL_TTL = 600           # 실행 중 실패(메모리 부족 등)하면 10분간 규칙 방식으로 — 매번 기다리지 않게
_disabled = False        # 테스트·예시 데이터 생성 중에는 AI 를 끈다


def disable(flag: bool = True) -> None:
    global _disabled, _status_cache
    _disabled = flag
    _status_cache = None


def status(refresh: bool = False) -> Status:
    """Ollama 가 켜져 있고 모델이 있는지 (1분 캐시)."""
    global _status_cache
    if _disabled or AI_MODE == "off":
        return Status(False, MODEL, "AI 끔")
    if not is_local_address(OLLAMA_URL):
        return Status(False, MODEL, "외부 AI 주소 차단 · 보안 정책")
    if _status_cache and not refresh:
        ttl = 60 if _status_cache[1].available else FAIL_TTL
        if time.time() - _status_cache[0] < ttl:
            return _status_cache[1]
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=1.5) as r:
            names = {m["name"] for m in json.load(r).get("models", [])}
        st = Status(True, MODEL, "") if MODEL in names else Status(False, MODEL, f"모델 {MODEL} 없음")
    except Exception:
        st = Status(False, MODEL, "Ollama 꺼짐")
    _status_cache = (time.time(), st)
    return st


def _mark_failed(reason: str) -> None:
    """실행 중 실패(메모리 부족 등)하면 FAIL_TTL 동안 AI 를 쓰지 않는다. '연결 다시 확인'으로 바로 재시도."""
    global _status_cache
    _status_cache = (time.time(), Status(False, MODEL, reason))


def _post(path: str, body: dict, timeout: float) -> dict:
    if not is_local_address(OLLAMA_URL):            # 이중 확인: 어떤 경로로도 외부로 보내지 않는다
        raise AIUnavailable("외부 AI 주소 차단 · 보안 정책")
    req = urllib.request.Request(f"{OLLAMA_URL}{path}", json.dumps(body).encode("utf-8"),
                                 {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:200]
        reason = "AI 메모리 부족" if "buffer" in detail or "memory" in detail.lower() else f"AI 오류 {e.code}"
        _mark_failed(reason)
        raise AIUnavailable(reason) from e
    except Exception as e:     # 시간 초과, 연결 끊김
        _mark_failed("AI 응답 없음")
        raise AIUnavailable("AI 응답 없음") from e


def chat(messages: list[dict], *, schema: dict | None = None, images: list[bytes] | None = None,
         model: str | None = None, timeout: float | None = None, temperature: float = 0.0):
    """Ollama /api/chat. schema 를 주면 그 JSON 형식으로만 답하게 하고 dict 를 돌려준다."""
    if not status().available:
        raise AIUnavailable(status().reason)
    msgs = [dict(m) for m in messages]
    if images:
        msgs[-1]["images"] = [base64.b64encode(b).decode() for b in images]
    body = {"model": model or (VISION_MODEL if images else MODEL), "messages": msgs, "stream": False,
            "options": {"temperature": temperature}, "think": False}
    if schema:
        body["format"] = schema
    content = _post("/api/chat", body, timeout or TIMEOUT)["message"]["content"]
    if not schema:
        return content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        raise AIUnavailable("AI 답이 형식에 맞지 않음") from e
