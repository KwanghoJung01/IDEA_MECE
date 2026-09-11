"""프로젝트 소스 zip 다운로드 라우트 (로컬 실행용)."""
from __future__ import annotations

import io

from flask import Blueprint, current_app, send_file

from services.packager import build_source_zip
from services.security import ensure_session

bp = Blueprint("package", __name__)


@bp.get("/download/source")
def download_source():
    """Flask 폴더 구조를 유지한 전체 소스를 zip 한 개로 내려준다."""
    ensure_session()
    data = build_source_zip(current_app.config["BASE_DIR"])
    response = send_file(
        io.BytesIO(data),
        mimetype="application/zip",
        as_attachment=True,
        download_name="idea_mece_flask.zip",
        max_age=0,
    )
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Content-Length"] = str(len(data))
    return response
