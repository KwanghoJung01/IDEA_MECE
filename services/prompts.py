"""
프롬프트 엔지니어링 (MECE 아이디어 생성 · 보고서 집필)
=====================================================

MECE 품질을 결정하는 3가지 장치
------------------------------
1. **단일 분류축 강제** — 모델이 먼저 "이번 분해에 사용할 하나의 기준(axis)"을
   출력하도록 스키마에 포함시킨다. 기준이 섞이는 순간 중복(ME 위반)이
   발생하므로, 축을 먼저 확정하게 하는 것이 가장 효과적인 통제 수단이다.
   (축 값은 내부 품질 통제용이며 화면/보고서에는 노출하지 않는다.)
2. **누적 맥락의 계층적 압축** — 상위로 올라갈수록 정보를 줄인다.
   · 최상위 주제: 전문(全文)
   · 직계 부모: 제목 + 설명 전문
   · 가까운 조상 2개: 제목 + 설명 요약
   · 더 먼 조상: 제목만
   깊이가 깊어져도 프롬프트 길이가 거의 늘지 않아(대략 상수 + 제목 길이)
   생성 속도와 비용이 일정하게 유지된다.
3. **배제 목록 주입** — 이미 존재하는 같은 수준 항목과 부모의 형제 항목을
   "침범 금지 영역"으로 함께 전달해 사후 중복을 차단한다.
"""
from __future__ import annotations

from typing import Any, Sequence

# ── 길이 예산 (프롬프트 길이 최적화: 총 2~3KB 수준 유지) ──────────────
ROOT_TOPIC_BUDGET = 300
PARENT_CONTENT_BUDGET = 420
NEAR_ANCESTOR_CONTENT_BUDGET = 150
NEAR_ANCESTOR_COUNT = 2
TITLE_BUDGET = 70
MAX_EXCLUSION_ITEMS = 24
SECTION_CONTENT_BUDGET = 700


def _clip(text: Any, limit: int) -> str:
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


# ════════════════════════════════════════════════════════════════════
#  1. 아이디어 생성
# ════════════════════════════════════════════════════════════════════
IDEA_SYSTEM_INSTRUCTION = """당신은 대기업 전략기획실의 수석 비즈니스 컨설턴트입니다.
주어진 상위 과제를 하위 과제로 분해하는 역할을 맡았습니다.

[분해 원칙 — 반드시 모두 충족]
1. 분류 기준 단일화: 이번 분해에 사용할 기준을 단 하나만 정하고, 모든 항목을 그 기준으로만 나눕니다. 기준이 섞이면 실패입니다.
2. 상호배타: 두 항목의 의미 영역이 조금이라도 겹치면 안 됩니다. 한 사안은 오직 한 항목에만 속해야 합니다.
3. 전체포괄: 모든 항목을 합치면 상위 과제의 범위가 빠짐없이 채워져야 합니다. 누락된 영역이 있으면 실패입니다.
4. 동일 추상수준: 모든 항목의 구체성 수준이 같아야 합니다. 어떤 항목은 큰 영역이고 어떤 항목은 세부 실행안이면 실패입니다.
5. 실무성: 추상적인 구호가 아니라 기업이 실제로 검토·실행할 수 있는 내용이어야 합니다.
6. 금지 영역 침범 금지: '제외 영역'으로 제시된 항목과 같은 내용을 다시 만들지 않습니다.

[출력 규칙]
- 반드시 한국어로 작성합니다.
- title: 항목 이름. 12~30자의 간결한 명사형 구문. 번호·기호·마크다운 없이 순수 텍스트.
- content: 해당 항목의 핵심 내용을 2~3문장으로 서술. 무엇을 어떻게 하는 것인지, 왜 유효한지가 담겨야 합니다.
  마크다운 기호(*, #, -, `)와 줄바꿈을 쓰지 않고 평문 문장으로만 씁니다.
- axis: 이번 분해에 사용한 단일 분류 기준을 15자 내외로 적습니다.
- 요청받은 개수를 정확히 지킵니다."""

IDEA_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "axis": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["title", "content"],
            },
        },
    },
    "required": ["axis", "items"],
}


def build_idea_prompt(
    *,
    root_topic: str,
    ancestors: Sequence[dict[str, Any]],
    parent: dict[str, Any] | None,
    existing_children: Sequence[str],
    parent_siblings: Sequence[str],
    count: int,
) -> str:
    """
    하위 아이디어 생성용 사용자 프롬프트를 조립한다.

    ancestors        : 루트→부모 순서의 조상 목록 (부모 포함 가능, 부모는 parent로 별도 전달)
    parent           : 직계 부모 노드(None이면 최상위 아이디어 생성)
    existing_children: 이미 만들어진 같은 수준 항목 제목 (중복 방지)
    parent_siblings  : 부모의 형제 항목 제목 (영역 침범 방지)
    """
    lines: list[str] = [f"[전체 주제]\n{_clip(root_topic, ROOT_TOPIC_BUDGET)}"]

    if parent is None:
        lines.append(
            "[분해 대상]\n위 전체 주제 자체입니다. 이 주제를 구성하는 최상위 영역으로 나누어 주세요."
        )
        target_depth = 1
    else:
        # 조상 맥락: 먼 조상은 제목만, 가까운 조상 2개는 요약까지 (누적 맥락의 압축 전달)
        chain = [a for a in ancestors if int(a.get("id", 0)) != int(parent.get("id", 0))]
        if chain:
            ctx: list[str] = []
            near_start = max(0, len(chain) - NEAR_ANCESTOR_COUNT)
            for idx, anc in enumerate(chain):
                label = f"{idx + 1}단계: {_clip(anc.get('title'), TITLE_BUDGET)}"
                if idx >= near_start and anc.get("content"):
                    label += f" — {_clip(anc.get('content'), NEAR_ANCESTOR_CONTENT_BUDGET)}"
                ctx.append(label)
            lines.append("[상위 맥락 (위에서 아래로)]\n" + "\n".join(ctx))

        parent_block = f"제목: {_clip(parent.get('title'), TITLE_BUDGET * 2)}"
        if parent.get("content"):
            parent_block += f"\n설명: {_clip(parent.get('content'), PARENT_CONTENT_BUDGET)}"
        lines.append("[분해 대상 — 바로 이 항목을 나눕니다]\n" + parent_block)
        target_depth = int(parent.get("depth", 0)) + 2

    if existing_children:
        items = [_clip(t, TITLE_BUDGET) for t in existing_children[:MAX_EXCLUSION_ITEMS]]
        lines.append(
            "[이미 만들어진 같은 수준 항목 — 중복 금지, 남은 빈틈을 채우세요]\n"
            + " / ".join(items)
        )

    if parent_siblings:
        items = [_clip(t, TITLE_BUDGET) for t in parent_siblings[:MAX_EXCLUSION_ITEMS]]
        lines.append(
            "[인접 영역 — 이 영역들을 침범하지 마세요]\n" + " / ".join(items)
        )

    guidance = (
        f"[요청]\n위 분해 대상을 상호배타적이고 전체포괄적인 하위 항목 정확히 {count}개로 나누세요.\n"
        f"현재 단계는 전체 주제로부터 {target_depth}번째 하위 단계이므로, "
        "상위 단계보다 한 단계 더 구체적이되 모든 항목의 구체성은 서로 같아야 합니다."
    )
    if existing_children:
        guidance += "\n이미 만들어진 항목과는 절대 겹치지 않게, 아직 다뤄지지 않은 영역만 만드세요."
    lines.append(guidance)

    return "\n\n".join(lines)


# ════════════════════════════════════════════════════════════════════
#  2. 보고서 집필
# ════════════════════════════════════════════════════════════════════
# 개발 과정의 내부 용어가 보고서에 노출되지 않도록 하는 1차 방어선(프롬프트 차원).
# 2차 방어선은 services/sanitizer.py 의 용어 필터가 담당한다.
_FORBIDDEN_VOCAB = (
    "MECE, 상호배타, 전체포괄, 트리, 노드, 부모/자식, 계층 구조, 깊이, 단계(depth), "
    "분류축, 프롬프트, AI, 인공지능, 모델, 생성, 도출 과정, 데이터 구조"
)

REPORT_SYSTEM_INSTRUCTION = f"""당신은 대기업 경영진에게 제출되는 전략 보고서를 집필하는 수석 컨설턴트입니다.
실무자가 보고서를 받아 즉시 검토·실행 판단에 활용할 수 있는 수준으로 집필합니다.

[집필 원칙]
1. 서술형 완성 문장: 항목 나열이나 요약 메모가 아니라, 맥락과 근거가 이어지는 문단으로 씁니다.
2. 구체성: 실행 방법, 대상 고객/시장, 기대 효과, 선결 과제, 위험 요인을 구체적으로 서술합니다.
   숫자나 기준이 필요한 곳은 "어떤 지표로 판단해야 하는지"를 제시합니다.
3. 근거 제시: 왜 유효한지, 어떤 조건에서 성립하는지 논리적으로 설명합니다.
4. 분량: 지시된 분량을 지키고, 문단 사이는 빈 줄 하나로 구분합니다.

[절대 금지 — 위반 시 보고서가 폐기됩니다]
- 다음 용어와 그 유사 표현을 단 한 번도 쓰지 않습니다: {_FORBIDDEN_VOCAB}
- 자료의 구성 방식이나 작성 과정을 설명하지 않습니다. 결과 내용만 씁니다.
  ("상위 항목", "하위 항목", "~를 분해하면", "본 구조는" 같은 표현 금지)
- 마크다운 기호를 사용하지 않습니다. 별표(*), 샵(#), 백틱(`), 밑줄 강조(_), 하이픈 불릿(-) 모두 금지입니다.
  강조가 필요하면 기호가 아니라 문장 표현으로 강조합니다.
- 표, 코드블록, 링크를 쓰지 않습니다.
- 제목을 쓸 때는 기호 없이 한 줄에 제목 문구만 적습니다.

[출력 형식]
- 순수한 한국어 평문. 제목 줄과 본문 문단만 사용합니다."""


def build_overview_prompt(*, topic: str, branch_titles: Sequence[str], total_items: int) -> str:
    """보고서 도입부(개요·총평) 집필 프롬프트."""
    areas = " / ".join(_clip(t, TITLE_BUDGET) for t in branch_titles[:12])
    return (
        f"[보고서 주제]\n{_clip(topic, ROOT_TOPIC_BUDGET)}\n\n"
        f"[보고서가 다루는 핵심 영역]\n{areas}\n\n"
        f"[검토된 세부 과제 규모]\n총 {total_items}건\n\n"
        "[요청]\n위 주제에 대한 전략 보고서의 도입부를 집필하세요. 다음 내용을 문단으로 이어서 씁니다.\n"
        "첫째, 이 주제가 지금 왜 중요한지에 대한 배경과 시장 환경 진단.\n"
        "둘째, 이 보고서가 제시하는 핵심 결론을 3~4문장으로 압축한 요약.\n"
        "셋째, 핵심 영역들이 전체 전략에서 각각 어떤 역할을 하는지에 대한 조망.\n"
        "제목 줄은 쓰지 말고 본문 문단만 작성하세요. 전체 분량은 700자에서 1,100자 사이로 맞추세요."
    )


def build_section_prompt(
    *, topic: str, branch_title: str, branch_content: str, outline: str, item_count: int
) -> str:
    """핵심 영역(최상위 가지) 1개에 대한 본문 섹션 집필 프롬프트."""
    target = 1200 if item_count <= 4 else 1700
    return (
        f"[보고서 주제]\n{_clip(topic, ROOT_TOPIC_BUDGET)}\n\n"
        f"[집필 대상 영역]\n{_clip(branch_title, TITLE_BUDGET * 2)}\n"
        f"{_clip(branch_content, SECTION_CONTENT_BUDGET)}\n\n"
        f"[이 영역에서 검토된 세부 내용]\n{outline}\n\n"
        "[요청]\n위 영역에 대한 보고서 본문을 집필하세요.\n"
        "세부 내용을 그대로 옮겨 적지 말고, 각 사안을 해석하고 연결해 하나의 논리적인 서술로 재구성하세요.\n"
        "반드시 포함할 내용: 이 영역의 사업적 의미, 구체적인 추진 방안, 실행 시 예상되는 효과와 판단 지표, "
        "선결 과제와 위험 요인, 그리고 우선순위 관점의 제언.\n"
        "첫 줄에 이 영역을 대표하는 제목을 기호 없이 한 줄로 적고, 한 줄 띄운 뒤 본문 문단을 작성하세요.\n"
        f"본문 분량은 약 {target}자 전후로 맞추세요."
    )


def build_closing_prompt(*, topic: str, section_digest: str) -> str:
    """결론·실행 로드맵 집필 프롬프트."""
    return (
        f"[보고서 주제]\n{_clip(topic, ROOT_TOPIC_BUDGET)}\n\n"
        f"[본문에서 다룬 영역과 핵심 논지]\n{section_digest}\n\n"
        "[요청]\n보고서의 결론부를 집필하세요. 다음을 문단으로 이어서 씁니다.\n"
        "첫째, 전체 내용을 관통하는 핵심 판단과 의사결정 권고.\n"
        "둘째, 단기(3개월 내), 중기(6~12개월), 장기(1년 이상)로 나눈 실행 순서와 각 구간의 목표.\n"
        "셋째, 성공 여부를 판단할 핵심 점검 지표와 추진 과정에서 특히 경계해야 할 위험.\n"
        "제목 줄은 쓰지 말고 본문 문단만 작성하세요. 전체 분량은 600자에서 900자 사이로 맞추세요."
    )


def build_outline_digest(items: Sequence[dict[str, Any]], *, limit: int = 40) -> str:
    """
    섹션 프롬프트에 넣을 세부 내용 요약 블록.
    깊이 정보를 숫자가 아닌 들여쓰기로만 표현해, 내부 구조 용어가 유출되지 않게 한다.
    """
    lines: list[str] = []
    base_depth = min((int(i.get("depth", 0)) for i in items), default=0)
    for item in items[:limit]:
        indent = "  " * max(0, int(item.get("depth", 0)) - base_depth)
        text = f"{indent}{_clip(item.get('title'), TITLE_BUDGET * 2)}"
        content = _clip(item.get("content"), 220)
        if content:
            text += f": {content}"
        lines.append(text)
    if len(items) > limit:
        lines.append(f"  (이하 {len(items) - limit}건의 세부 사안 포함)")
    return "\n".join(lines)
