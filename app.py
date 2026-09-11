"""
IDEA_MECE — AI 기반 비즈니스 아이디어 도출 · 보고서 자동화
=========================================================

실행 (로컬)
    python app.py
실행 (배포)
    gunicorn --workers 1 --threads 8 --timeout 300 wsgi:app

※ API 키는 서버에 저장되지 않습니다. 사용자가 화면의 [설정]에서 입력한 키는
  해당 브라우저 세션에만 격리 보관되며(services/security.py), 서버 메모리에서
  유효시간이 지나면 자동 폐기됩니다.
"""
from __future__ import annotations

import itertools
import logging
import os
import sys

from flask import Flask, jsonify, request

import models
from config import get_config
from routes import BLUEPRINTS
from services import downloads
from services.security import ensure_session, vault

logger = logging.getLogger(__name__)

# 요청 카운터 기반 주기적 정리 (별도 스레드를 만들지 않아 누수 위험이 없다)
_request_counter = itertools.count()
SWEEP_EVERY = 200


def _configure_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # 액세스 로그에 쿼리스트링이 남지 않도록 werkzeug 로깅 수준을 조정
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def create_app() -> Flask:
    config = get_config()
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config.from_object(config)

    _configure_logging(bool(app.config.get("DEBUG")))

    if not app.config.get("SECRET_KEY_FROM_ENV"):
        logger.warning(
            "SECRET_KEY 환경변수가 설정되지 않아 임시 키를 사용합니다. "
            "배포 환경에서는 반드시 .env 또는 호스팅 환경변수에 SECRET_KEY를 지정하세요."
        )

    # 저장소 준비
    os.makedirs(app.config["DOWNLOAD_DIR"], exist_ok=True)
    models.init_app(app)

    # 시작 시 남아 있던 만료 파일 정리
    downloads.sweep(app.config["DOWNLOAD_DIR"], app.config["DOWNLOAD_TTL_SECONDS"])

    for blueprint in BLUEPRINTS:
        app.register_blueprint(blueprint)

    _register_hooks(app)
    _register_error_handlers(app)
    return app


def _register_hooks(app: Flask) -> None:
    @app.before_request
    def _prepare():
        # 정적 파일 요청에는 세션 처리를 건너뛰어 오버헤드를 없앤다.
        if request.endpoint == "static":
            return None
        ensure_session()
        if next(_request_counter) % SWEEP_EVERY == 0:
            vault.sweep()
            downloads.sweep(app.config["DOWNLOAD_DIR"], app.config["DOWNLOAD_TTL_SECONDS"])
        return None

    @app.after_request
    def _security_headers(response):
        # 외부 리소스를 일절 쓰지 않으므로 가장 엄격한 CSP를 적용할 수 있다.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'none'; "
            "object-src 'none'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), camera=(), microphone=(), payment=()"
        )
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if app.config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        # API 응답은 캐시하지 않는다 (사용자별 데이터)
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, private"
        return response


def _register_error_handlers(app: Flask) -> None:
    def _wants_json() -> bool:
        return request.path.startswith("/api/") or "application/json" in (
            request.headers.get("Accept", "")
        )

    @app.errorhandler(400)
    def _bad_request(_e):
        return jsonify({"ok": False, "error": "요청 형식이 올바르지 않습니다."}), 400

    @app.errorhandler(404)
    def _not_found(_e):
        if _wants_json():
            return jsonify({"ok": False, "error": "요청한 경로를 찾을 수 없습니다."}), 404
        return jsonify({"ok": False, "error": "페이지를 찾을 수 없습니다."}), 404

    @app.errorhandler(405)
    def _not_allowed(_e):
        return jsonify({"ok": False, "error": "허용되지 않은 요청 방식입니다."}), 405

    @app.errorhandler(413)
    def _too_large(_e):
        return jsonify({"ok": False, "error": "입력 내용이 너무 큽니다. 분량을 줄여 주세요."}), 413

    @app.errorhandler(500)
    def _server_error(_e):
        return jsonify({"ok": False, "error": "서버 내부 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."}), 500

    @app.errorhandler(Exception)
    def _unhandled(exc: Exception):
        # 내부 예외 상세는 로그에만 남기고, 사용자에게는 일반 메시지만 노출한다.
        logger.exception("처리되지 않은 오류: %s", type(exc).__name__)
        return jsonify({"ok": False, "error": "처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."}), 500


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    # 스마트폰 등 같은 네트워크의 다른 기기에서도 접속할 수 있도록 0.0.0.0 바인딩
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
