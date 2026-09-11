"""
보고서 후처리 (다중 검증 정화 계층)
==================================

목표
----
(1) 마크다운 기호(``*``, ``**``, ``#``~``#####``, 백틱, ``~~``, 불릿 기호)가
    최종 워드 파일에 **단 하나도** 남지 않게 한다.
(2) 개발 과정에서 쓰인 내부 용어(MECE, 트리, 노드, 계층, 깊이 등)가
    보고서 본문에 노출되지 않게 한다.

5겹 방어
--------
L1. 프롬프트 차원 금지 지시 (services/prompts.py)
L2. 정규화 — 줄바꿈/제어문자/제로폭 문자 정리
L3. 용어 필터 — 내부 용어를 자연스러운 실무 표현으로 치환
L4. 구조 파싱 — 줄머리 기호(#, -, 1.)를 "서식 정보"로 변환하고 기호 자체는 제거
                인라인 강조(**,*,__,_,`,~~)를 굵게/기울임 서식으로 변환하고 기호 제거
L5. 잔여 문자 스크럽 + 최종 감사(audit) — 런(run) 단위로 금지 문자를 재검사하여
    남아 있으면 제거하고 위반 건수를 반환한다. 워드 생성 직전/직후 두 번 호출된다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

__all__ = [
    "Block",
    "Run",
    "audit_blocks",
    "audit_text",
    "clean_inline",
    "filter_terms",
    "normalize_raw",
    "parse_report",
    "scrub",
]

# ───────────────────── L2. 정규화 ─────────────────────
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def normalize_raw(text: str) -> str:
    if not text:
        return ""
    out = str(text).replace("\r\n", "\n").replace("\r", "\n")
    out = _ZERO_WIDTH_RE.sub("", out)
    out = _CONTROL_RE.sub("", out)
    # 코드블록 펜스 제거 (모델이 습관적으로 감싸는 경우)
    out = re.sub(r"^\s*```[A-Za-z0-9_\-]*\s*$", "", out, flags=re.MULTILINE)
    out = re.sub(r"^\s*~~~[A-Za-z0-9_\-]*\s*$", "", out, flags=re.MULTILINE)
    # 수평선 제거
    out = re.sub(r"^\s*([-*_=])\1{2,}\s*$", "", out, flags=re.MULTILINE)
    # 표 구분선 제거
    out = re.sub(r"^\s*\|?[\s:\-|]{5,}\|?\s*$", "", out, flags=re.MULTILINE)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


# ───────────────────── L3. 용어 필터 ─────────────────────
# 긴 표현을 먼저 치환해야 짧은 치환이 문맥을 깨뜨리지 않는다.
_TERM_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), repl)
    for pattern, repl in (
        # (a) 자기 참조 문장 먼저 제거 — "본 구조는 ~" 류
        (r"(?:본|이)\s*(?:트리|계층|분해)?\s*(?:구조|체계)(?:는|가|를|에서|의)\s*", ""),
        (r"(?:본|이)\s*보고서의?\s*(?:구성|구조)(?:는|은|를|이)\s*", ""),
        # (b) 방법론 용어
        (r"MECE\s*(?:원칙|관점|기법|방식)", "중복과 누락을 배제하는 원칙"),
        (r"상호\s*배타적(?:이고|이며)?\s*전체\s*포괄적(?:으로|인|이며|이고)?", "중복과 누락 없이"),
        (r"상호\s*배타성(?:과|와)?\s*전체\s*포괄성", "중복 없는 완전성"),
        (r"상호\s*배타적(?:으로|인|이며|이고|이다)?", "서로 겹치지 않게"),
        (r"전체\s*포괄적(?:으로|인|이며|이고|이다)?", "빠짐없이"),
        (r"상호\s*배타성", "영역 구분"),
        (r"전체\s*포괄성", "범위 완전성"),
        (r"MECE", "중복·누락 없는 정리"),
        # (c) 자료구조 용어 — 뒤따르는 조사는 건드리지 않고 단어만 치환한다
        (r"(?:최상위|상위|하위|부모|자식|리프|루트)\s*노드", "항목"),
        (r"노드", "항목"),
        (r"(?:트리|계층)\s*구조", "구성 체계"),
        (r"(?<![A-Za-z가-힣])트리(?=[\s,.)\]를을은는이가의에로와과도만]|$)", "구성 체계"),
        (r"계층\s*적?\s*으?로\s*분해", "단계적으로 구체화"),
        (r"깊이\s*\d+\s*단계", "세부 단계"),
        (r"(?:depth|Depth|DEPTH)\s*\d*\s*(?:단계)?", "세부 단계"),
        (r"분류\s*축", "구분 기준"),
        (r"분기\s*(?:노드|항목)", "핵심 영역"),
        # (d) 생성 과정을 드러내는 "표현"만 치환한다.
        #     ※ 'AI', '인공지능', 'LLM', '프롬프트' 같은 낱말 자체는 치환하지 않는다.
        #       보고서 주제가 AI 사업일 수도 있어, 낱말 단위 치환은 정상 내용을 훼손한다.
        #       따라서 "무엇이 무엇을 생성했다"는 과정 서술 형태만 잡아낸다.
        (r"(?:상위|하위)\s*항목(?:을|이|은|는|의|로|과|와|에서|에)?\s*(?:분해|전개)(?:하면|한다면|하여|해서|해|하면서)", "구체화하면"),
        (r"(?:를|을)\s*(?:분해|전개)(?:하면|한다면|하여|해서)", "를 구체화하면"),
        (
            r"(?:생성형\s*)?(?:AI|A\.I\.|인공지능|언어\s*모델|LLM|Gemini|제미나이)"
            r"(?:가|이|를|을|는|의|로|와|과|에|에서|에게|에 의해|을 통해|를 통해)?\s*"
            r"(?:생성|도출|작성|제시|제안|산출)(?:한|하였|했|해|하여|된|되었|됐)[가-힣]*",
            "본 검토에서 도출한",
        ),
        (r"작성\s*지침(?:에|을|의)?\s*(?:따라|기반으로)\s*", ""),
    )
)

# 치환으로 생길 수 있는 조사 불일치를 보정한다.
# 전역 보정은 "효과→효와" 같은 오손상 위험이 있으므로,
# 반드시 치환어 바로 뒤에 붙은 조사만 교정한다.
_REPL_WORDS = (
    "항목", "구성 체계", "구분 기준", "작성 지침", "분석 도구", "분석",
    "세부 단계", "핵심 영역", "범위 완전성", "영역 구분", "분량",
    "중복·누락 없는 정리",
)
_PARTICLE_PAIRS = (
    ("을", "를"), ("이", "가"), ("은", "는"), ("과", "와"), ("으로", "로"),
    ("이나", "나"), ("이라", "라"), ("이란", "란"), ("으로서", "로서"), ("으로써", "로써"),
)
_PARTICLE_ALTS = "|".join(
    sorted({p for pair in _PARTICLE_PAIRS for p in pair}, key=len, reverse=True)
)
_PARTICLE_FIX_RE = re.compile(
    r"(" + "|".join(re.escape(w) for w in _REPL_WORDS) + r")(" + _PARTICLE_ALTS + r")"
    r"(?![가-힣])"
)
# 종성 있는 음절 뒤 / 없는 음절 뒤에 각각 와야 하는 형태
_CONSONANT_FORM = {v: c for c, v in _PARTICLE_PAIRS} | {c: c for c, _v in _PARTICLE_PAIRS}
_VOWEL_FORM = {c: v for c, v in _PARTICLE_PAIRS} | {v: v for _c, v in _PARTICLE_PAIRS}
_RO_FAMILY = {"으로", "로", "으로서", "로서", "으로써", "로써"}


def _has_final_consonant(syllable: str) -> bool:
    """한글 음절의 종성 유무. (가-힣 범위는 (초성,중성,종성) 조합이 규칙적이다)"""
    code = ord(syllable)
    if not 0xAC00 <= code <= 0xD7A3:
        return False
    return (code - 0xAC00) % 28 != 0


def _is_rieul_final(syllable: str) -> bool:
    code = ord(syllable)
    if not 0xAC00 <= code <= 0xD7A3:
        return False
    return (code - 0xAC00) % 28 == 8  # 종성 ㄹ


def _fix_particles(text: str) -> str:
    """치환어 바로 뒤에 붙은 조사만 종성 규칙에 맞게 교정한다."""

    def repl(match: "re.Match[str]") -> str:
        word, particle = match.group(1), match.group(2)
        last = word[-1]
        if not ("가" <= last <= "힣"):
            return match.group(0)
        closed = _has_final_consonant(last)
        # '로/으로' 계열은 종성 ㄹ 뒤에서도 '로'를 쓴다 (예: 서울로)
        if particle in _RO_FAMILY and _is_rieul_final(last):
            closed = False
        table = _CONSONANT_FORM if closed else _VOWEL_FORM
        return f"{word}{table.get(particle, particle)}"

    return _PARTICLE_FIX_RE.sub(repl, text)


def filter_terms(text: str) -> str:
    """내부 용어를 실무 표현으로 치환한다."""
    if not text:
        return ""
    out = text
    for pattern, repl in _TERM_RULES:
        out = pattern.sub(repl, out)
    out = _fix_particles(out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\s+([,.)\]])", r"\1", out)
    out = re.sub(r"^\s*(?:그리고|또한|따라서)\s*,", "", out)
    return out.strip()


# ───────────────────── L4/L5. 기호 제거 ─────────────────────
# 최종 텍스트에 남아서는 안 되는 문자들
_FORBIDDEN_CHARS = "*#`~"
_RESIDUAL_RE = re.compile(r"[*#`~]+")
_BULLET_GLYPH_RE = re.compile(r"^\s*[•·▪◦‣∙※o]\s+")
_LIST_MARKER_RE = re.compile(
    r"^\s*(?:[-*+•·▪◦‣]|(?P<num>\d{1,2})[.)]|\(\d{1,2}\)|[가-하][.)])\s+"
)
_HEADING_RE = re.compile(r"^\s{0,3}(?P<hashes>#{1,6})\s*(?P<text>.*)$")
_QUOTE_RE = re.compile(r"^\s{0,3}>+\s?")

_INLINE_RE = re.compile(
    r"\*\*\*(?P<bi>[^\n]+?)\*\*\*"
    r"|\*\*(?P<b>[^\n]+?)\*\*"
    r"|__(?P<ub>[^\n]+?)__"
    r"|\*(?P<i>[^*\n]+?)\*"
    r"|(?<![0-9A-Za-z_])_(?P<ui>[^_\n]+?)_(?![0-9A-Za-z_])"
    r"|`+(?P<code>[^`\n]*?)`+"
    r"|~~(?P<strike>[^\n]+?)~~"
)


@dataclass(slots=True)
class Run:
    """서식이 적용된 텍스트 조각."""

    text: str
    bold: bool = False
    italic: bool = False


@dataclass(slots=True)
class Block:
    """문서 블록. kind: heading | paragraph | bullet | numbered"""

    kind: str
    runs: list[Run] = field(default_factory=list)
    level: int = 0

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)


def scrub(text: str) -> str:
    """런 단위 최종 스크럽: 금지 문자 제거 + 공백 정리."""
    if not text:
        return ""
    out = _RESIDUAL_RE.sub("", text)
    out = out.replace("\u00a0", " ")
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out


def clean_inline(text: str) -> list[Run]:
    """
    인라인 마크다운을 서식 런으로 변환한다.
    기호는 전부 제거되고, 의미(굵게/기울임)만 서식으로 보존된다.
    """
    if not text:
        return []
    runs: list[Run] = []
    cursor = 0
    for match in _INLINE_RE.finditer(text):
        if match.start() > cursor:
            plain = scrub(text[cursor : match.start()])
            if plain:
                runs.append(Run(plain))
        if match.group("bi") is not None:
            body, bold, italic = match.group("bi"), True, True
        elif match.group("b") is not None:
            body, bold, italic = match.group("b"), True, False
        elif match.group("ub") is not None:
            body, bold, italic = match.group("ub"), True, False
        elif match.group("i") is not None:
            body, bold, italic = match.group("i"), True, False  # 강조는 굵게로 통일
        elif match.group("ui") is not None:
            body, bold, italic = match.group("ui"), True, False
        elif match.group("code") is not None:
            body, bold, italic = match.group("code"), False, False
        else:
            body, bold, italic = match.group("strike") or "", False, False
        body = scrub(body).strip()
        if body:
            runs.append(Run(body, bold=bold, italic=italic))
        cursor = match.end()

    if cursor < len(text):
        tail = scrub(text[cursor:])
        if tail:
            runs.append(Run(tail))

    # 인접한 동일 서식 런 병합 (워드 파일 크기/렌더링 최적화)
    merged: list[Run] = []
    for run in runs:
        if merged and merged[-1].bold == run.bold and merged[-1].italic == run.italic:
            merged[-1].text += run.text
        else:
            merged.append(run)

    # 양끝 공백 정리
    if merged:
        merged[0].text = merged[0].text.lstrip()
        merged[-1].text = merged[-1].text.rstrip()
    return [r for r in merged if r.text]


def parse_report(raw: str, *, base_heading_level: int = 2) -> list[Block]:
    """
    정화된 평문을 워드 블록 목록으로 변환한다.
    ``base_heading_level``은 마크다운 ``#`` 하나가 매핑될 워드 제목 수준이다.
    """
    text = filter_terms(normalize_raw(raw))
    blocks: list[Block] = []

    for raw_line in text.split("\n"):
        line = _QUOTE_RE.sub("", raw_line).rstrip()
        if not line.strip():
            continue

        heading = _HEADING_RE.match(line)
        if heading and heading.group("text").strip():
            level = min(5, base_heading_level + len(heading.group("hashes")) - 1)
            runs = clean_inline(heading.group("text").strip())
            if runs:
                blocks.append(Block("heading", runs, level))
            continue

        marker = _LIST_MARKER_RE.match(line)
        if marker:
            body = line[marker.end() :].strip()
            runs = clean_inline(body)
            if runs:
                kind = "numbered" if marker.group("num") else "bullet"
                blocks.append(Block(kind, runs, 0))
            continue

        stripped = _BULLET_GLYPH_RE.sub("", line).strip()

        # 굵게만으로 이루어진 짧은 줄은 소제목으로 승격 (모델의 흔한 출력 형태)
        bold_only = re.fullmatch(r"\*\*(?P<t>[^*\n]{2,60})\*\*[:：]?", stripped)
        if bold_only:
            runs = clean_inline(bold_only.group("t"))
            if runs:
                blocks.append(Block("heading", runs, min(5, base_heading_level + 1)))
            continue

        runs = clean_inline(stripped)
        if runs:
            blocks.append(Block("paragraph", runs, 0))

    return blocks


# ───────────────────── 최종 감사 ─────────────────────
def audit_text(text: str) -> int:
    """텍스트에 남은 금지 문자 수를 센다."""
    if not text:
        return 0
    return sum(text.count(ch) for ch in _FORBIDDEN_CHARS)


def audit_blocks(blocks: Iterable[Block]) -> int:
    """
    블록 전체를 재검사해 금지 문자가 남아 있으면 제거하고 위반 건수를 반환한다.
    (워드 생성 직전 최종 관문)
    """
    violations = 0
    for block in blocks:
        for run in block.runs:
            found = audit_text(run.text)
            if found:
                violations += found
                run.text = scrub(run.text)
        block.runs[:] = [r for r in block.runs if r.text.strip()]
    return violations
