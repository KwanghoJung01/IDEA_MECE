"""
보안 유틸리티
=============

1) **API 키 격리 보관 (KeyVault)**
   Gemini API 키는 서버 전역(환경변수/DB/파일)에 저장하지 않는다.
   - 브라우저 세션 쿠키에는 난수 핸들(sid)만 담기고,
   - 실제 키는 프로세스 메모리의 ``KeyVault``에 ``sid``별로 분리 보관된다.
   - TTL이 지나면 자동 폐기되고, 접근마다 만료분을 청소하므로 누수가 없다.
   - 키는 절대 클라이언트로 되돌려주지 않으며(마스킹 값만 제공), 로그에도 남기지 않는다.
   따라서 동일 배포 URL에 여러 사용자가 동시에 접속해도 키가 섞이지 않는다.

   ※ 메모리 보관 방식이므로 워커는 1개(+스레드)로 운영한다(Procfile 참고).
     다중 워커가 필요하면 KeyVault를 Redis 등 외부 저장소로 교체하면 된다.

2) **CSRF 방어**
   세션에 난수 토큰을 심고, 상태 변경 요청(POST/PATCH/DELETE)은
   ``X-CSRF-Token`` 헤더와 상수시간 비교로 검증한다.

3) **입력 검증/정규화**
   길이 제한, 제어문자 제거, 정수 범위 검증을 한 곳에서 처리한다.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
import unicodedata
from dataclasses import dataclass
from functools import wraps
from typing import Any, Callable

from flask import current_app, jsonify, request, session

__all__ = [
    "ApiCredentials",
    "KeyVault",
    "csrf_protect",
    "ensure_session",
    "get_csrf_token",
    "mask_key",
    "sanitize_text",
    "validate_count",
    "normalize_api_key",
    "validate_api_key",
    "validate_email",
    "validate_emails",
    "validate_model",
    "vault",
]

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}$")
_EMAIL_FIND_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}")
# API 키에 쓰일 수 있는 문자 범위.
# ※ 예전에는 영문·숫자·밑줄·하이픈만 허용해 마침표(.)가 들어간 키가 거부되었다.
#   실제 키에는 마침표를 비롯한 여러 기호가 들어올 수 있으므로
#   "공백·제어문자가 없는 눈에 보이는 아스키 문자"까지 넓혔다.
#   공백과 줄바꿈을 계속 막으므로 헤더 인젝션 위험은 없다.
_API_KEY_RE = re.compile(r"^[\x21-\x7E]{20,200}$")
# 붙여넣기 과정에서 섞여 들어오는 보이지 않는 문자
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\u00a0]")

MAX_VAULT_ENTRIES = 5000  # 메모리 상한 (초과 시 가장 먼저 만료될 항목부터 제거)


@dataclass(slots=True)
class ApiCredentials:
    """세션 단위 자격정보 — 메모리에만 존재한다."""

    api_key: str
    model: str
    email: str = ""
    expires_at: float = 0.0


class KeyVault:
    """TTL 기반 메모리 금고. 스레드 안전하며 만료 항목을 자동 수거한다."""

    __slots__ = ("_store", "_lock")

    def __init__(self) -> None:
        self._store: dict[str, ApiCredentials] = {}
        self._lock = threading.Lock()

    def put(self, sid: str, api_key: str, model: str, email: str, ttl: int) -> None:
        if not sid:
            return
        entry = ApiCredentials(
            api_key=api_key, model=model, email=email, expires_at=time.time() + ttl
        )
        with self._lock:
            self._purge_locked()
            if len(self._store) >= MAX_VAULT_ENTRIES and sid not in self._store:
                oldest = min(self._store.items(), key=lambda kv: kv[1].expires_at)[0]
                self._store.pop(oldest, None)
            self._store[sid] = entry

    def get(self, sid: str) -> ApiCredentials | None:
        if not sid:
            return None
        now = time.time()
        with self._lock:
            entry = self._store.get(sid)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._store.pop(sid, None)
                return None
            return entry

    def update_meta(self, sid: str, *, model: str | None = None, email: str | None = None) -> bool:
        """키는 그대로 두고 모델/이메일만 갱신."""
        with self._lock:
            entry = self._store.get(sid)
            if entry is None or entry.expires_at <= time.time():
                return False
            if model is not None:
                entry.model = model
            if email is not None:
                entry.email = email
            return True

    def drop(self, sid: str) -> None:
        with self._lock:
            self._store.pop(sid, None)

    def sweep(self) -> int:
        with self._lock:
            return self._purge_locked()

    def _purge_locked(self) -> int:
        now = time.time()
        expired = [k for k, v in self._store.items() if v.expires_at <= now]
        for k in expired:
            self._store.pop(k, None)
        return len(expired)


vault = KeyVault()


# ───────────────────────── 세션 ─────────────────────────
def ensure_session() -> None:
    """세션에 필요한 난수 핸들을 준비한다(값 자체는 비밀이 아님)."""
    session.permanent = True
    if not session.get("sid"):
        session["sid"] = secrets.token_urlsafe(24)
    if not session.get("owner_key"):
        session["owner_key"] = secrets.token_urlsafe(32)
    if not session.get("csrf"):
        session["csrf"] = secrets.token_urlsafe(32)
    if "model" not in session:
        session["model"] = current_app.config["DEFAULT_MODEL"]


def get_csrf_token() -> str:
    ensure_session()
    return str(session.get("csrf", ""))


def csrf_protect(view: Callable) -> Callable:
    """상태 변경 엔드포인트용 CSRF 검증 데코레이터."""

    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any):
        expected = session.get("csrf")
        supplied = request.headers.get("X-CSRF-Token", "")
        if not expected or not supplied or not secrets.compare_digest(str(expected), supplied):
            return jsonify({"ok": False, "error": "보안 토큰이 유효하지 않습니다. 화면을 새로 고친 뒤 다시 시도해 주세요."}), 403
        return view(*args, **kwargs)

    return wrapper


# ───────────────────────── 검증/정규화 ─────────────────────────
def sanitize_text(value: Any, max_length: int, *, allow_newlines: bool = False) -> str:
    """
    제어문자·유니코드 꼼수를 제거하고 길이를 제한한다.
    (출력 단계에서도 이스케이프하지만, 저장 전에 1차 정화한다.)
    """
    if value is None:
        return ""
    text = str(value)
    text = unicodedata.normalize("NFC", text)
    # 방향 제어/제로폭 문자 제거 (시각적 스푸핑 방지)
    text = text.replace("​", "").replace("﻿", "")
    text = re.sub(r"[‪-‮⁦-⁩]", "", text)
    if allow_newlines:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = _CONTROL_RE.sub("", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = "\n".join(line.rstrip() for line in text.split("\n"))
    else:
        text = _CONTROL_RE.sub("", text.replace("\n", " ").replace("\t", " "))
        text = re.sub(r"\s{2,}", " ", text)
    text = text.strip()
    return text[:max_length]


def validate_count(value: Any, maximum: int) -> int:
    """생성 개수 검증 (1 ~ maximum)."""
    try:
        count = int(value)
    except (TypeError, ValueError):
        raise ValueError("생성 개수는 숫자로 입력해 주세요.") from None
    if count < 1:
        raise ValueError("생성 개수는 1개 이상이어야 합니다.")
    if count > maximum:
        raise ValueError(f"한 번에 생성할 수 있는 아이디어는 최대 {maximum}개입니다.")
    return count


def validate_model(value: Any) -> str:
    """화이트리스트 기반 모델 검증 (임의 문자열로 외부 URL 조작 방지)."""
    allowed = {m for m, _label in current_app.config["ALLOWED_MODELS"]}
    model = str(value or "").strip()
    if model not in allowed:
        return str(current_app.config["DEFAULT_MODEL"])
    return model


def normalize_api_key(value: Any) -> str:
    """보이지 않는 문자·앞뒤 공백·감싼 따옴표를 걷어낸다."""
    key = _INVISIBLE_RE.sub("", str(value or ""))
    return key.strip().strip("\"'`").strip()


def validate_api_key(value: Any) -> str:
    """키를 검사하고 정규화된 값을 반환한다. 무엇이 잘못됐는지 구체적으로 알린다."""
    key = normalize_api_key(value)
    if not key:
        raise ValueError("API 키를 입력해 주세요.")
    if any(ch.isspace() for ch in key):
        raise ValueError("API 키에 공백이나 줄바꿈이 섞여 있습니다. 키 전체를 한 번에 복사해 붙여넣어 주세요.")
    if not key.isascii():
        raise ValueError("API 키에 한글이나 특수한 문자가 섞여 있습니다. 키만 정확히 복사했는지 확인해 주세요.")
    if len(key) < 20:
        raise ValueError(f"API 키가 너무 짧습니다({len(key)}자). 키 전체를 복사했는지 확인해 주세요.")
    if len(key) > 200:
        raise ValueError("API 키가 너무 깁니다. 키 외의 내용이 함께 붙여넣어지지 않았는지 확인해 주세요.")
    if not _API_KEY_RE.match(key):
        raise ValueError("API 키 형식을 확인해 주세요.")
    return key


MAX_RECIPIENTS = 30


def validate_emails(value: Any) -> str:
    """
    자유 입력에서 이메일 주소만 뽑아 쉼표로 정규화한다.
    "hong@a.com, kim@b.com" 은 물론 "홍길동 <hong@a.com>" 처럼
    메일 프로그램에서 복사한 형태도 그대로 받아들인다.
    """
    text = str(value or "").strip()
    if not text:
        return ""

    found = _EMAIL_FIND_RE.findall(text)
    emails: list[str] = []
    seen: set[str] = set()
    for email in found:
        if len(email) > 254 or not _EMAIL_RE.match(email):
            continue
        key = email.lower()
        if key in seen:          # 중복 수신자 제거
            continue
        seen.add(key)
        emails.append(email)

    # 인식된 주소를 걷어낸 뒤에도 '@' 가 남으면 형식이 잘못된 항목이 있다는 뜻
    rest = text
    for email in found:
        rest = rest.replace(email, " ")
    if "@" in rest:
        raise ValueError("형식이 올바르지 않은 이메일이 있습니다. 쉼표로 구분해 다시 확인해 주세요.")
    if len(emails) > MAX_RECIPIENTS:
        raise ValueError(f"수신자는 최대 {MAX_RECIPIENTS}명까지 등록할 수 있습니다.")
    return ", ".join(emails)


def validate_email(value: Any) -> str:
    """단일 주소 검증(하위 호환). 내부적으로 다중 검증을 사용한다."""
    return validate_emails(value)


def mask_key(key: str) -> str:
    """화면 표시용 마스킹 (원본 키는 절대 반환하지 않는다)."""
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:4]}{'•' * 12}{key[-4:]}"
