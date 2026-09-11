"""애플리케이션 설정 — 모든 민감정보는 환경변수에서만 읽어온다."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    """환경변수를 안전하게 정수로 변환한다 (잘못된 값이면 기본값 사용)."""
    raw = os.environ.get(name, "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value < minimum:
        return minimum
    if maximum is not None and value > maximum:
        return maximum
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


class Config:
    """기본(운영) 설정."""

    # ── 보안 ──────────────────────────────────────────────────────────
    # SECRET_KEY가 없으면 임의 생성한다. 단일 인스턴스에서는 동작하지만
    # 재시작 시 세션이 초기화되므로 배포 환경에서는 반드시 환경변수로 지정한다.
    SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_urlsafe(48)
    SECRET_KEY_FROM_ENV = bool(os.environ.get("SECRET_KEY"))

    SESSION_COOKIE_NAME = "idea_mece_sid"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", True)
    PERMANENT_SESSION_LIFETIME = _env_int("SESSION_LIFETIME_SECONDS", 60 * 60 * 12, minimum=600)

    # 요청 본문 크기 상한 (과대 입력으로 인한 메모리 고갈 방지)
    MAX_CONTENT_LENGTH = 1 * 1024 * 1024  # 1MB
    JSON_SORT_KEYS = False

    # ── 저장소 ────────────────────────────────────────────────────────
    BASE_DIR = BASE_DIR
    DATABASE_PATH = str(
        (BASE_DIR / os.environ.get("DATABASE_PATH", "instance/idea_mece.sqlite3")).resolve()
        if not os.path.isabs(os.environ.get("DATABASE_PATH", ""))
        else Path(os.environ["DATABASE_PATH"]).resolve()
    )
    DOWNLOAD_DIR = str(BASE_DIR / "downloads")

    # ── 트리/생성 한도 ────────────────────────────────────────────────
    MAX_CHILDREN_PER_REQUEST = _env_int("MAX_CHILDREN_PER_REQUEST", 10, minimum=1, maximum=20)
    MAX_NODES_PER_PROJECT = _env_int("MAX_NODES_PER_PROJECT", 2000, minimum=10, maximum=50000)
    MAX_TREE_DEPTH = _env_int("MAX_TREE_DEPTH", 100, minimum=2, maximum=500)
    MAX_TOPIC_LENGTH = 300
    MAX_TITLE_LENGTH = 300
    MAX_CONTENT_TEXT_LENGTH = 4000

    # ── 수명주기 (메모리/디스크 누수 방지) ────────────────────────────
    API_KEY_TTL_SECONDS = _env_int("API_KEY_TTL_SECONDS", 7200, minimum=300)
    DOWNLOAD_TTL_SECONDS = _env_int("DOWNLOAD_TTL_SECONDS", 3600, minimum=300)

    # ── Gemini ────────────────────────────────────────────────────────
    GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models"
    GEMINI_TIMEOUT = _env_int("GEMINI_TIMEOUT", 120, minimum=15, maximum=600)
    # 무료 등급에서 사용 가능한 모델 목록 (UI 드롭다운과 서버 화이트리스트를 공유)
    ALLOWED_MODELS = (
        ("gemini-3.6-flash", "Gemini 3.6 Flash (최신·권장)"),
        ("gemini-3.5-flash-lite", "Gemini 3.5 Flash Lite (빠름)"),
        ("gemini-3.1-flash-lite", "Gemini 3.1 Flash Lite (경량)"),
    )
    DEFAULT_MODEL = "gemini-3.6-flash"

    # 보고서 섹션 동시 생성 워커 수 (과도한 동시 호출은 레이트리밋 유발)
    REPORT_WORKERS = _env_int("REPORT_WORKERS", 3, minimum=1, maximum=6)


class DevelopmentConfig(Config):
    DEBUG = False  # 디버그 콘솔은 원격 코드 실행 위험이 있어 기본 비활성화
    SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE", False)


def get_config() -> type[Config]:
    env = os.environ.get("FLASK_ENV", "production").strip().lower()
    return DevelopmentConfig if env in {"development", "dev", "local"} else Config
