"""라우트 공통 헬퍼 (응답 포맷, 세션 프로젝트 조회, 자격정보 확인)."""
from __future__ import annotations

from typing import Any

from flask import current_app, jsonify, request, session

from models import TreeRepository
from services.security import ensure_session, vault

__all__ = [
    "credentials_or_error",
    "current_project",
    "json_body",
    "ok",
    "fail",
    "repository",
]


def ok(**payload: Any):
    return jsonify({"ok": True, **payload})


def fail(message: str, status: int = 400, **extra: Any):
    return jsonify({"ok": False, "error": message, **extra}), status


def json_body() -> dict[str, Any]:
    """JSON 본문을 안전하게 얻는다(형식 오류 시 빈 dict)."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def repository() -> TreeRepository:
    ensure_session()
    return TreeRepository()


def current_project(repo: TreeRepository):
    """현재 브라우저 세션이 소유한 프로젝트(없으면 None)."""
    return repo.get_project_by_owner(str(session.get("owner_key") or ""))


def credentials_or_error() -> tuple[Any, Any]:
    """
    세션 금고에서 API 자격정보를 꺼낸다.
    반환: (credentials, None) 또는 (None, 오류응답)
    """
    ensure_session()
    creds = vault.get(str(session.get("sid") or ""))
    if creds is None or not creds.api_key:
        return None, fail(
            "Gemini API 키가 설정되지 않았습니다. 우측 상단 [설정]에서 키를 입력해 주세요.",
            401,
            need_settings=True,
        )
    model = creds.model or str(current_app.config["DEFAULT_MODEL"])
    allowed = {m for m, _ in current_app.config["ALLOWED_MODELS"]}
    if model not in allowed:
        model = str(current_app.config["DEFAULT_MODEL"])
    creds.model = model
    return creds, None
