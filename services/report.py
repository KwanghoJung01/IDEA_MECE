"""
보고서 생성 로직
===============

구성
----
1. **도입부** — 배경 진단 + 핵심 결론 요약 + 영역 조망
2. **본문 섹션** — 최상위 영역별로 1개 섹션. 각 섹션은 해당 가지의 모든 세부
   내용을 근거로 "해석·재구성된 서술형" 본문으로 집필된다(제목 나열 금지).
3. **결론부** — 의사결정 권고 + 단/중/장기 실행 로드맵 + 점검 지표와 위험

성능
----
* 섹션은 서로 독립적이므로 제한된 워커(기본 3)로 병렬 생성한다.
  직렬 대비 체감 시간이 크게 줄지만, 워커 수를 묶어 레이트리밋을 피한다.
* 스레드에는 Flask 컨텍스트가 필요한 객체를 넘기지 않는다(평문 인자만 전달).
* 큰 문자열을 누적하지 않고 섹션 단위로 즉시 블록으로 변환해 메모리 사용을 억제한다.

품질 통제
--------
* 모든 생성 텍스트는 sanitizer 를 거쳐 내부 용어/마크다운이 제거된다.
* 섹션 생성이 일부 실패해도 보고서 전체가 실패하지 않도록, 실패 섹션은
  해당 영역의 검토 내용을 기반으로 한 대체 서술로 채운다.
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from . import gemini, prompts
from .sanitizer import Block, Run, audit_blocks, filter_terms, normalize_raw, parse_report, scrub

logger = logging.getLogger(__name__)

__all__ = ["ReportError", "ReportResult", "build_report"]

MAX_SECTIONS = 12           # 섹션(최상위 영역) 수 상한 — 호출 비용/시간 통제
MAX_ITEMS_PER_SECTION = 40  # 섹션 프롬프트에 넣을 세부 항목 수 상한


class ReportError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(slots=True)
class ReportResult:
    title: str
    topic: str
    blocks: list[Block]
    summary: str                       # 메일 본문용 요약 (평문)
    node_count: int
    section_count: int
    generated_at: datetime
    violations: int = 0                # 최종 감사에서 제거된 금지 문자 수
    failed_sections: list[str] = field(default_factory=list)
    omitted_sections: int = 0          # 상한을 넘어 보고서에서 제외된 최상위 영역 수


# ───────────────────── 트리 조립 ─────────────────────
def _group_branches(nodes: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    최상위 항목별로 자신의 하위 전체를 평면 목록으로 묶는다.
    (프로젝트 전체를 한 번 읽은 결과만 사용하므로 추가 쿼리가 없다)
    """
    children_map: dict[int | None, list[dict[str, Any]]] = {}
    for node in nodes:
        children_map.setdefault(node["parent_id"], []).append(node)
    for siblings in children_map.values():
        siblings.sort(key=lambda n: (n["sort_order"], n["id"]))

    branches: list[dict[str, Any]] = []
    for root in children_map.get(None, []):
        flat: list[dict[str, Any]] = []
        stack = list(reversed(children_map.get(root["id"], [])))
        while stack:
            current = stack.pop()
            flat.append(current)
            kids = children_map.get(current["id"])
            if kids:
                stack.extend(reversed(kids))
        branches.append({"root": root, "items": flat})
    return branches


# ───────────────────── 섹션 생성 ─────────────────────
def _write_section(
    *,
    topic: str,
    branch: dict[str, Any],
    api_key: str,
    model: str,
    endpoint: str,
    timeout: int,
) -> tuple[str, str | None]:
    """섹션 1개 집필. 반환: (본문 원문, 실패 시 영역명)"""
    root = branch["root"]
    items = branch["items"][:MAX_ITEMS_PER_SECTION]
    outline = prompts.build_outline_digest(items, limit=MAX_ITEMS_PER_SECTION) if items else "세부 내용 없음"
    prompt = prompts.build_section_prompt(
        topic=topic,
        branch_title=str(root["title"]),
        branch_content=str(root.get("content") or ""),
        outline=outline,
        item_count=len(items),
    )
    try:
        text = gemini.generate_text(
            api_key=api_key,
            model=model,
            endpoint=endpoint,
            system_instruction=prompts.REPORT_SYSTEM_INSTRUCTION,
            user_prompt=prompt,
            timeout=timeout,
            temperature=0.6,
            max_output_tokens=4096,
        )
        return text, None
    except gemini.GeminiError as exc:
        logger.warning("섹션 집필 실패(%s): %s", root["title"], exc)
        return "", str(root["title"])


def _fallback_section(branch: dict[str, Any]) -> str:
    """AI 호출이 실패한 섹션을 검토 내용 기반 서술로 대체한다."""
    root = branch["root"]
    lines = [str(root["title"])]
    intro = str(root.get("content") or "").strip()
    if intro:
        lines.append(intro)
    lines.append(
        "이 영역에서는 다음 사안들이 중점 검토 대상으로 확인되었다. "
        "각 사안은 추진 우선순위와 실행 가능성을 기준으로 추가 검증이 필요하다."
    )
    for item in branch["items"][:MAX_ITEMS_PER_SECTION]:
        title = str(item.get("title") or "").strip()
        content = str(item.get("content") or "").strip()
        if not title:
            continue
        lines.append(f"{title}. {content}" if content else f"{title}.")
    lines.append(
        "본 영역은 일시적인 집필 오류로 요약 수준의 정리만 수록되었다. "
        "보고서를 다시 생성하면 상세 서술로 보완된다."
    )
    return "\n\n".join(lines)


def _split_title_body(raw: str) -> tuple[str, str]:
    """섹션 원문에서 첫 줄(제목)과 본문을 분리한다."""
    text = normalize_raw(raw)
    if not text:
        return "", ""
    lines = text.split("\n")
    head = lines[0].strip()
    # 줄머리 기호/번호 제거
    head = scrub(head).strip(" :·-–—")
    # 목록 번호("1.", "2)")만 제거한다. lstrip으로 숫자를 통째로 깎으면
    # "2026년 전략" 같은 정상 제목이 "년 전략"으로 훼손된다.
    head = re.sub(r"^\s*(?:\d{1,2}[.)]|\(\d{1,2}\))\s*", "", head).strip()
    head = filter_terms(head)
    body = "\n".join(lines[1:]).strip()
    if not body:  # 제목만 있고 본문이 없으면 전체를 본문으로 취급
        return "", text
    if len(head) > 80:  # 첫 줄이 길면 제목이 아니라 본문 문단이다
        return "", text
    return head, body


# ───────────────────── 본체 ─────────────────────
def build_report(
    *,
    topic: str,
    nodes: Sequence[dict[str, Any]],
    api_key: str,
    model: str,
    endpoint: str,
    timeout: int,
    workers: int = 3,
) -> ReportResult:
    if not nodes:
        raise ReportError("먼저 아이디어를 생성해 주세요. 생성된 내용이 없으면 보고서를 만들 수 없습니다.")

    branches = _group_branches(nodes)
    if not branches:
        raise ReportError("보고서를 구성할 핵심 영역이 없습니다. 아이디어를 먼저 생성해 주세요.")

    omitted = max(0, len(branches) - MAX_SECTIONS)
    branches = branches[:MAX_SECTIONS]
    blocks: list[Block] = []
    failed: list[str] = []

    # ── 1) 도입부 ──────────────────────────────────────────────
    overview_raw = ""
    try:
        overview_raw = gemini.generate_text(
            api_key=api_key,
            model=model,
            endpoint=endpoint,
            system_instruction=prompts.REPORT_SYSTEM_INSTRUCTION,
            user_prompt=prompts.build_overview_prompt(
                topic=topic,
                branch_titles=[str(b["root"]["title"]) for b in branches],
                total_items=len(nodes),
            ),
            timeout=timeout,
            temperature=0.55,
            max_output_tokens=3072,
        )
    except gemini.GeminiError as exc:
        # 도입부는 보고서의 근간이므로 실패 시 즉시 사용자에게 원인을 알린다.
        raise ReportError(str(exc), status=getattr(exc, "status", 502)) from exc

    blocks.append(Block("heading", [Run("검토 배경 및 핵심 요약")], 1))
    overview_blocks = parse_report(overview_raw, base_heading_level=2)
    blocks.extend(overview_blocks)

    summary_source = " ".join(
        b.text for b in overview_blocks if b.kind == "paragraph"
    ).strip()
    summary = summary_source[:700]

    # ── 2) 본문 섹션 (제한 병렬) ───────────────────────────────
    worker_count = max(1, min(int(workers), len(branches)))
    results: list[tuple[str, str | None]]
    if len(branches) == 1:
        results = [
            _write_section(
                topic=topic,
                branch=branches[0],
                api_key=api_key,
                model=model,
                endpoint=endpoint,
                timeout=timeout,
            )
        ]
    else:
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            results = list(
                pool.map(
                    lambda b: _write_section(
                        topic=topic,
                        branch=b,
                        api_key=api_key,
                        model=model,
                        endpoint=endpoint,
                        timeout=timeout,
                    ),
                    branches,
                )
            )

    digest_lines: list[str] = []
    for index, (branch, (raw_text, failure)) in enumerate(zip(branches, results), start=1):
        if failure is not None:
            failed.append(failure)
            raw_text = _fallback_section(branch)

        head, body = _split_title_body(raw_text)
        section_title = head or str(branch["root"]["title"])
        section_title = scrub(filter_terms(section_title)).strip() or f"핵심 영역 {index}"
        blocks.append(Block("heading", [Run(f"{index}. {section_title}")], 1))

        # 파싱은 섹션당 한 번만 수행하고 결과를 재사용한다.
        section_blocks = parse_report(body or raw_text, base_heading_level=2)
        blocks.extend(section_blocks)

        first_para = next((b.text for b in section_blocks if b.kind == "paragraph"), "")
        digest_lines.append(f"{section_title}: {first_para[:200]}")

    # ── 3) 결론부 ──────────────────────────────────────────────
    try:
        closing_raw = gemini.generate_text(
            api_key=api_key,
            model=model,
            endpoint=endpoint,
            system_instruction=prompts.REPORT_SYSTEM_INSTRUCTION,
            user_prompt=prompts.build_closing_prompt(
                topic=topic, section_digest="\n".join(digest_lines)
            ),
            timeout=timeout,
            temperature=0.55,
            max_output_tokens=2560,
        )
    except gemini.GeminiError as exc:
        logger.warning("결론부 집필 실패: %s", exc)
        closing_raw = ""

    if closing_raw.strip():
        blocks.append(Block("heading", [Run("결론 및 실행 제언")], 1))
        blocks.extend(parse_report(closing_raw, base_heading_level=2))

    # ── 4) 최종 감사 (마크다운 잔류 0 보장) ────────────────────
    violations = audit_blocks(blocks)
    blocks = [b for b in blocks if b.runs]
    if not blocks:
        raise ReportError("보고서 본문을 생성하지 못했습니다. 잠시 후 다시 시도해 주세요.", status=502)

    clean_topic = scrub(topic).strip()
    return ReportResult(
        title=f"{clean_topic} 사업화 검토 보고서",
        topic=clean_topic,
        blocks=blocks,
        summary=summary,
        node_count=len(nodes),
        section_count=len(branches),
        generated_at=datetime.now(),
        violations=violations,
        failed_sections=failed,
        omitted_sections=omitted,
    )
