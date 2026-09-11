"""
설정 라우트 (API 키 / 모델 / 이메일)
==================================

API 키는 응답으로 절대 돌려주지 않는다. 설정 여부와 마스킹된 표시값만 제공한다.
키는 서버 메모리의 세션별 금고에만 존재하며, DB·파일·로그에 기록되지 않는다.
"""
from __future__ import annotations

from flask import Blueprint, current_app, session

from services.security import (
    csrf_protect,
    ensure_session,
    mask_key,
    validate_api_key,
    validate_emails,
    validate_model,
    vault,
)

from .helpers import fail, json_body, ok

bp = Blueprint("settings", __name__, url_prefix="/api/settings")


def _state() -> dict:
    sid = str(session.get("sid") or "")
    creds = vault.get(sid)
    return {
        "configured": bool(creds and creds.api_key),
        "masked_key": mask_key(creds.api_key) if creds else "",
        "model": (creds.model if creds else None) or session.get("model") or current_app.config["DEFAULT_MODEL"],
        "email": (creds.email if creds else None) or str(session.get("email") or ""),
        "models": [{"id": mid, "label": label} for mid, label in current_app.config["ALLOWED_MODELS"]],
        "ttl_minutes": int(current_app.config["API_KEY_TTL_SECONDS"] // 60),
    }


@bp.get("")
def read_settings():
    ensure_session()
    return ok(settings=_state())


@bp.post("")
@csrf_protect
def save_settings():
    ensure_session()
    data = json_body()
    sid = str(session.get("sid") or "")
    existing = vault.get(sid)

    try:
        model = validate_model(data.get("model"))
        email = validate_emails(data.get("email"))
        raw_key = str(data.get("api_key") or "").strip()
        if raw_key:
            api_key = validate_api_key(raw_key)
        elif existing and existing.api_key:
            api_key = existing.api_key          # 키 재입력 없이 모델/이메일만 변경
        else:
            return fail("Gemini API 키를 입력해 주세요.", 400)
    except ValueError as exc:
        return fail(str(exc), 400)

    # 비밀이 아닌 값만 쿠키 세션에 보관 (키는 절대 보관하지 않음)
    session["model"] = model
    session["email"] = email

    vault.put(
        sid,
        api_key=api_key,
        model=model,
        email=email,
        ttl=int(current_app.config["API_KEY_TTL_SECONDS"]),
    )
    return ok(settings=_state(), message="설정이 저장되었습니다. 키는 이 브라우저 세션에만 보관됩니다.")


@bp.delete("")
@csrf_protect
def clear_settings():
    """세션 금고에서 키를 즉시 폐기한다."""
    ensure_session()
    vault.drop(str(session.get("sid") or ""))
    return ok(settings=_state(), message="API 키가 삭제되었습니다.")
