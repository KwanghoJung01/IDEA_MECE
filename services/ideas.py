"""아이디어 생성 오케스트레이션 (맥락 수집 → AI 호출 → 정규화/중복제거 → 저장)."""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Sequence

from . import gemini, prompts
from .sanitizer import scrub

__all__ = ["GenerationError", "generate_children"]

_DEDUP_STRIP_RE = re.compile(r"[\s\-·~!@#$%^&*()_+=\[\]{};:'\",.<>/?\\|`]+")


class GenerationError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _dedup_key(title: str) -> str:
    """표기 차이를 무시한 중복 판정 키."""
    return _DEDUP_STRIP_RE.sub("", str(title or "")).lower()


def _normalize_items(
    raw_items: Sequence[Any],
    *,
    exclude_keys: set[str],
    count: int,
    max_title: int,
    max_content: int,
) -> list[tuple[str, str]]:
    """AI 응답을 저장 가능한 (제목, 내용) 목록으로 정규화한다."""
    cleaned: list[tuple[str, str]] = []
    seen = set(exclude_keys)

    for item in raw_items:
        if not isinstance(item, dict):
            continue
        title = scrub(str(item.get("title") or "")).strip(" :·-–—")
        content = scrub(str(item.get("content") or "")).strip()
        # 제목에 번호가 붙어 오는 경우 제거
        title = re.sub(r"^\s*(?:\d{1,2}[.)]|\(\d{1,2}\)|[가-하][.)])\s*", "", title)
        title = " ".join(title.split())[:max_title]
        content = " ".join(content.split())[:max_content]
        if not title:
            continue
        key = _dedup_key(title)
        if not key or key in seen:
            continue
        seen.add(key)
        cleaned.append((title, content))
        if len(cleaned) >= count:
            break
    return cleaned


def generate_children(
    *,
    repo,
    project: sqlite3.Row,
    parent: sqlite3.Row | None,
    count: int,
    api_key: str,
    model: str,
    endpoint: str,
    timeout: int,
    max_title: int,
    max_content: int,
) -> list[dict[str, Any]]:
    """
    부모 노드(없으면 루트)의 하위 아이디어를 생성해 저장한 뒤 생성 결과를 반환한다.
    """
    project_id = int(project["id"])
    topic = str(project["topic"])

    # ── 맥락 수집 (쿼리 3회 이내로 제한해 깊은 트리에서도 빠르게 동작) ──
    if parent is None:
        ancestors: list[dict[str, Any]] = []
        parent_payload = None
        existing_rows = repo.list_children(project_id, None)
        parent_siblings: list[str] = []
    else:
        ancestor_rows = repo.list_ancestors(project_id, parent)
        ancestors = [
            {"id": int(r["id"]), "title": r["title"], "content": r["content"]}
            for r in ancestor_rows
        ]
        parent_payload = {
            "id": int(parent["id"]),
            "title": parent["title"],
            "content": parent["content"],
            "depth": int(parent["depth"]),
        }
        existing_rows = repo.list_children(project_id, int(parent["id"]))
        sibling_rows = repo.list_children(
            project_id,
            int(parent["parent_id"]) if parent["parent_id"] is not None else None,
        )
        parent_siblings = [
            str(r["title"]) for r in sibling_rows if int(r["id"]) != int(parent["id"])
        ]

    existing_titles = [str(r["title"]) for r in existing_rows]

    user_prompt = prompts.build_idea_prompt(
        root_topic=topic,
        ancestors=ancestors,
        parent=parent_payload,
        existing_children=existing_titles,
        parent_siblings=parent_siblings,
        count=count,
    )

    # 요청 개수에 비례한 출력 한도 (과소/과대 할당 방지)
    max_tokens = min(8192, 700 + count * 420)

    payload = gemini.generate_json(
        api_key=api_key,
        model=model,
        endpoint=endpoint,
        system_instruction=prompts.IDEA_SYSTEM_INSTRUCTION,
        user_prompt=user_prompt,
        response_schema=prompts.IDEA_RESPONSE_SCHEMA,
        timeout=timeout,
        temperature=0.85,
        max_output_tokens=max_tokens,
    )

    raw_items = payload.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise GenerationError("AI가 아이디어를 생성하지 못했습니다. 다시 시도해 주세요.", status=502)

    items = _normalize_items(
        raw_items,
        exclude_keys={_dedup_key(t) for t in existing_titles},
        count=count,
        max_title=max_title,
        max_content=max_content,
    )
    if not items:
        raise GenerationError(
            "기존 항목과 겹치지 않는 새로운 아이디어를 찾지 못했습니다. "
            "상위 항목의 설명을 구체적으로 수정한 뒤 다시 시도해 주세요.",
            status=409,
        )

    return repo.add_children(project_id, parent, items)
