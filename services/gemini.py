"""
Gemini API 연동 서비스
=====================

보안
----
* API 키는 URL 쿼리스트링이 아니라 ``x-goog-api-key`` 헤더로 전송한다.
  (URL에 넣으면 프록시/액세스 로그에 키가 남을 수 있다)
* 모델명은 호출 전에 화이트리스트로 검증된 값만 들어온다(URL 경로 조작 차단).
* 예외 메시지에 키가 포함되지 않도록 응답 본문을 가공해서 노출한다.

성능
----
* 프로세스 전역 ``requests.Session`` 1개를 재사용해 TLS 핸드셰이크 비용을 없앤다.
* 커넥션 풀을 고정 크기로 유지하므로 동시 요청이 늘어도 소켓이 무한 증가하지 않는다.
* 일시적 오류(429/5xx)는 지수 백오프로 제한 재시도한다.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

__all__ = ["GeminiError", "generate_json", "generate_text"]

_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9.\-]{3,64}$")
_session_lock = threading.Lock()
_session: requests.Session | None = None


class GeminiError(RuntimeError):
    """사용자에게 그대로 보여줄 수 있는 한국어 오류."""

    def __init__(self, message: str, *, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def _get_session() -> requests.Session:
    """전역 세션(커넥션 풀) 지연 생성."""
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                sess = requests.Session()
                retry = Retry(
                    total=2,
                    connect=2,
                    read=1,
                    backoff_factor=1.2,
                    status_forcelist=(429, 500, 502, 503, 504),
                    allowed_methods=frozenset({"POST"}),
                    raise_on_status=False,
                    respect_retry_after_header=True,
                )
                adapter = HTTPAdapter(pool_connections=4, pool_maxsize=12, max_retries=retry)
                sess.mount("https://", adapter)
                sess.headers.update({"User-Agent": "IDEA-MECE/1.0", "Accept": "application/json"})
                _session = sess
    return _session


def _friendly_error(status_code: int, body: str) -> GeminiError:
    """HTTP 상태코드를 실무자가 이해할 수 있는 안내로 변환한다."""
    snippet = re.sub(r"\s+", " ", body or "")[:300]
    if status_code in (400,):
        if "API key not valid" in body or "API_KEY_INVALID" in body:
            return GeminiError("API 키가 유효하지 않습니다. 설정에서 키를 다시 확인해 주세요.", status=400)
        return GeminiError(f"요청이 거부되었습니다. 입력 내용을 확인해 주세요. ({snippet})", status=400)
    if status_code in (401, 403):
        return GeminiError(
            "API 키 권한이 없습니다. 키가 올바른지, 해당 모델 사용이 허용된 키인지 확인해 주세요.", status=403
        )
    if status_code == 404:
        return GeminiError(
            "선택한 모델을 사용할 수 없습니다. 설정에서 다른 모델을 선택해 주세요.", status=404
        )
    if status_code == 429:
        return GeminiError(
            "무료 사용량 한도에 도달했습니다. 잠시 후 다시 시도하거나 생성 개수를 줄여 주세요.", status=429
        )
    if 500 <= status_code < 600:
        return GeminiError("AI 서비스가 일시적으로 불안정합니다. 잠시 후 다시 시도해 주세요.", status=503)
    return GeminiError(f"AI 호출에 실패했습니다. (코드 {status_code})", status=502)


def _call(
    *,
    api_key: str,
    model: str,
    endpoint: str,
    system_instruction: str,
    user_prompt: str,
    timeout: int,
    temperature: float,
    max_output_tokens: int,
    response_schema: dict[str, Any] | None = None,
) -> str:
    if not api_key:
        raise GeminiError("API 키가 설정되지 않았습니다. 우측 상단 설정에서 키를 입력해 주세요.", status=401)
    if not _MODEL_NAME_RE.match(model):
        raise GeminiError("허용되지 않은 모델입니다.", status=400)

    payload: dict[str, Any] = {
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": temperature,
            "topP": 0.95,
            "maxOutputTokens": max_output_tokens,
            "candidateCount": 1,
        },
    }
    if response_schema is not None:
        payload["generationConfig"]["responseMimeType"] = "application/json"
        payload["generationConfig"]["responseSchema"] = response_schema

    url = f"{endpoint.rstrip('/')}/{model}:generateContent"
    try:
        response = _get_session().post(
            url,
            json=payload,
            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
            timeout=(10, timeout),
        )
    except requests.Timeout as exc:
        raise GeminiError(
            "AI 응답이 지연되어 요청을 종료했습니다. 생성 개수를 줄여 다시 시도해 주세요.", status=504
        ) from exc
    except requests.RequestException as exc:
        logger.warning("Gemini 연결 실패: %s", type(exc).__name__)
        raise GeminiError("AI 서비스에 연결할 수 없습니다. 네트워크 상태를 확인해 주세요.", status=502) from exc

    if response.status_code != 200:
        # 응답 본문에 키가 포함될 여지를 없애기 위해 키 문자열을 마스킹한다.
        body = response.text.replace(api_key, "[REDACTED]") if api_key else response.text
        raise _friendly_error(response.status_code, body)

    try:
        data = response.json()
    except ValueError as exc:
        raise GeminiError("AI 응답을 해석할 수 없습니다. 잠시 후 다시 시도해 주세요.") from exc
    finally:
        response.close()

    return _extract_text(data)


def _extract_text(data: dict[str, Any]) -> str:
    feedback = data.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        raise GeminiError(
            "입력 내용이 AI 안전 정책에 의해 차단되었습니다. 주제 표현을 바꿔 다시 시도해 주세요.", status=400
        )

    candidates = data.get("candidates") or []
    if not candidates:
        raise GeminiError("AI가 응답을 생성하지 못했습니다. 잠시 후 다시 시도해 주세요.")

    candidate = candidates[0]
    finish = str(candidate.get("finishReason") or "")
    parts = ((candidate.get("content") or {}).get("parts")) or []
    text = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict)).strip()

    if not text:
        if finish == "SAFETY":
            raise GeminiError("AI 안전 정책으로 응답이 차단되었습니다. 주제를 조정해 주세요.", status=400)
        if finish == "MAX_TOKENS":
            raise GeminiError("응답이 분량 한도를 초과했습니다. 생성 개수를 줄여 주세요.", status=400)
        raise GeminiError("AI 응답이 비어 있습니다. 잠시 후 다시 시도해 주세요.")
    return text


def generate_text(
    *,
    api_key: str,
    model: str,
    endpoint: str,
    system_instruction: str,
    user_prompt: str,
    timeout: int = 120,
    temperature: float = 0.65,
    max_output_tokens: int = 4096,
) -> str:
    """서술형 텍스트 생성(보고서 집필용)."""
    return _call(
        api_key=api_key,
        model=model,
        endpoint=endpoint,
        system_instruction=system_instruction,
        user_prompt=user_prompt,
        timeout=timeout,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )


def generate_json(
    *,
    api_key: str,
    model: str,
    endpoint: str,
    system_instruction: str,
    user_prompt: str,
    response_schema: dict[str, Any],
    timeout: int = 120,
    temperature: float = 0.8,
    max_output_tokens: int = 3072,
) -> dict[str, Any]:
    """구조화 출력(JSON) 생성. 스키마를 강제해 파싱 실패 가능성을 낮춘다."""
    raw = _call(
        api_key=api_key,
        model=model,
        endpoint=endpoint,
        system_instruction=system_instruction,
        user_prompt=user_prompt,
        timeout=timeout,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        response_schema=response_schema,
    )
    return _parse_json(raw)


def _parse_json(raw: str) -> dict[str, Any]:
    """JSON 파싱 + 경미한 형식 오류 복구."""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z0-9]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        # 앞뒤 잡음 제거 후 재시도
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            start, end = text.find("["), text.rfind("]")
            if start == -1 or end <= start:
                raise GeminiError("AI 응답 형식이 올바르지 않습니다. 다시 시도해 주세요.") from None
            try:
                parsed = {"items": json.loads(text[start : end + 1])}
            except ValueError:
                raise GeminiError("AI 응답 형식이 올바르지 않습니다. 다시 시도해 주세요.") from None
        else:
            snippet = text[start : end + 1]
            try:
                parsed = json.loads(snippet)
            except ValueError:
                # 흔한 오류: 후행 쉼표
                try:
                    parsed = json.loads(re.sub(r",\s*([}\]])", r"\1", snippet))
                except ValueError:
                    raise GeminiError("AI 응답 형식이 올바르지 않습니다. 다시 시도해 주세요.") from None

    if isinstance(parsed, list):
        return {"items": parsed}
    if not isinstance(parsed, dict):
        raise GeminiError("AI 응답 형식이 올바르지 않습니다. 다시 시도해 주세요.")
    return parsed
