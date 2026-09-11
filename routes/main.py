"""메인 화면 및 상태 확인 라우트."""
from __future__ import annotations

from flask import Blueprint, current_app, render_template

from services.security import ensure_session, get_csrf_token

bp = Blueprint("main", __name__)


@bp.get("/")
def index():
    """
    초기 화면.
    ※ 어떤 샘플 데이터도 주입하지 않는다(주제 입력란은 항상 빈 값).
    """
    ensure_session()
    cfg = current_app.config
    models = [{"id": mid, "label": label} for mid, label in cfg["ALLOWED_MODELS"]]
    bootstrap = {
        "csrfToken": get_csrf_token(),
        "defaultModel": cfg["DEFAULT_MODEL"],
        "models": models,
        "limits": {
            "maxChildren": cfg["MAX_CHILDREN_PER_REQUEST"],
            "maxNodes": cfg["MAX_NODES_PER_PROJECT"],
            "maxDepth": cfg["MAX_TREE_DEPTH"],
            "maxTopic": cfg["MAX_TOPIC_LENGTH"],
            "maxTitle": cfg["MAX_TITLE_LENGTH"],
            "maxContent": cfg["MAX_CONTENT_TEXT_LENGTH"],
        },
    }
    return render_template(
        "index.html",
        bootstrap=bootstrap,
        models=cfg["ALLOWED_MODELS"],
        default_model=cfg["DEFAULT_MODEL"],
    )


@bp.get("/healthz")
def healthz():
    return {"status": "ok"}, 200
