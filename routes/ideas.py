"""아이디어 트리 CRUD 및 생성 라우트."""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, session

from models import NodeLimitError, TreeRepository
from services.ideas import GenerationError, generate_children
from services.gemini import GeminiError
from services.security import csrf_protect, ensure_session, sanitize_text, validate_count

from .helpers import credentials_or_error, current_project, fail, json_body, ok, repository

logger = logging.getLogger(__name__)
bp = Blueprint("ideas", __name__, url_prefix="/api")


def _node_payload(repo: TreeRepository, project_id: int) -> list[dict]:
    return TreeRepository.serialize(repo.list_all(project_id))


@bp.get("/tree")
def read_tree():
    """현재 세션의 트리 전체를 반환한다(없으면 빈 상태)."""
    repo = repository()
    project = current_project(repo)
    if project is None:
        return ok(topic="", nodes=[], node_count=0)
    nodes = _node_payload(repo, int(project["id"]))
    return ok(topic=project["topic"], nodes=nodes, node_count=len(nodes))


@bp.post("/tree")
@csrf_protect
def create_tree():
    """주제를 받아 새 프로젝트를 만들고 최상위 아이디어를 생성한다."""
    cfg = current_app.config
    data = json_body()

    topic = sanitize_text(data.get("topic"), cfg["MAX_TOPIC_LENGTH"])
    if len(topic) < 2:
        return fail("주제를 2자 이상 입력해 주세요.", 400)
    try:
        count = validate_count(data.get("count"), cfg["MAX_CHILDREN_PER_REQUEST"])
    except ValueError as exc:
        return fail(str(exc), 400)

    creds, error = credentials_or_error()
    if error:
        return error

    repo = repository()
    project_id = repo.reset_project(str(session["owner_key"]), topic)
    project = repo.get_project_by_owner(str(session["owner_key"]))

    try:
        created = generate_children(
            repo=repo,
            project=project,
            parent=None,
            count=count,
            api_key=creds.api_key,
            model=creds.model,
            endpoint=cfg["GEMINI_ENDPOINT"],
            timeout=cfg["GEMINI_TIMEOUT"],
            max_title=cfg["MAX_TITLE_LENGTH"],
            max_content=cfg["MAX_CONTENT_TEXT_LENGTH"],
        )
    except (GeminiError, GenerationError) as exc:
        return fail(str(exc), getattr(exc, "status", 502))
    except NodeLimitError as exc:
        return fail(str(exc), 409)

    return ok(topic=topic, nodes=created, node_count=repo.count_nodes(project_id))


@bp.post("/nodes/<int:node_id>/children")
@csrf_protect
def expand_node(node_id: int):
    """특정 항목의 하위 아이디어를 생성한다(무한 확장)."""
    cfg = current_app.config
    data = json_body()
    try:
        count = validate_count(data.get("count"), cfg["MAX_CHILDREN_PER_REQUEST"])
    except ValueError as exc:
        return fail(str(exc), 400)

    creds, error = credentials_or_error()
    if error:
        return error

    repo = repository()
    project = current_project(repo)
    if project is None:
        return fail("진행 중인 작업이 없습니다. 먼저 주제를 입력해 아이디어를 생성해 주세요.", 404)

    parent = repo.get_node(int(project["id"]), node_id)
    if parent is None:
        return fail("항목을 찾을 수 없습니다. 화면을 새로 고친 뒤 다시 시도해 주세요.", 404)

    try:
        created = generate_children(
            repo=repo,
            project=project,
            parent=parent,
            count=count,
            api_key=creds.api_key,
            model=creds.model,
            endpoint=cfg["GEMINI_ENDPOINT"],
            timeout=cfg["GEMINI_TIMEOUT"],
            max_title=cfg["MAX_TITLE_LENGTH"],
            max_content=cfg["MAX_CONTENT_TEXT_LENGTH"],
        )
    except (GeminiError, GenerationError) as exc:
        return fail(str(exc), getattr(exc, "status", 502))
    except NodeLimitError as exc:
        return fail(str(exc), 409)

    return ok(
        parent_id=node_id,
        nodes=created,
        node_count=repo.count_nodes(int(project["id"])),
    )


@bp.patch("/nodes/<int:node_id>")
@csrf_protect
def update_node(node_id: int):
    """항목 내용 수정."""
    cfg = current_app.config
    data = json_body()
    repo = repository()
    project = current_project(repo)
    if project is None:
        return fail("진행 중인 작업이 없습니다.", 404)

    node = repo.get_node(int(project["id"]), node_id)
    if node is None:
        return fail("항목을 찾을 수 없습니다.", 404)

    title = sanitize_text(data.get("title"), cfg["MAX_TITLE_LENGTH"])
    if len(title) < 1:
        return fail("제목은 비워 둘 수 없습니다.", 400)
    content = sanitize_text(
        data.get("content"), cfg["MAX_CONTENT_TEXT_LENGTH"], allow_newlines=True
    )

    if not repo.update_node(int(project["id"]), node_id, title, content):
        return fail("수정에 실패했습니다. 화면을 새로 고친 뒤 다시 시도해 주세요.", 409)

    return ok(node={"id": node_id, "title": title, "content": content})


@bp.delete("/nodes/<int:node_id>")
@csrf_protect
def delete_node(node_id: int):
    """항목과 그 하위 전체를 삭제한다(cascade)."""
    repo = repository()
    project = current_project(repo)
    if project is None:
        return fail("진행 중인 작업이 없습니다.", 404)

    node = repo.get_node(int(project["id"]), node_id)
    if node is None:
        return fail("항목을 찾을 수 없습니다.", 404)

    removed = repo.delete_subtree(int(project["id"]), node)
    return ok(
        node_id=node_id,
        removed=removed,
        node_count=repo.count_nodes(int(project["id"])),
    )


@bp.post("/reset")
@csrf_protect
def reset_tree():
    """현재 세션의 작업 내용을 전부 삭제한다."""
    ensure_session()
    repo = repository()
    project = current_project(repo)
    if project is not None:
        repo.delete_project(int(project["id"]))
    session.pop("reports", None)
    return ok(message="작업 내용이 초기화되었습니다.")
