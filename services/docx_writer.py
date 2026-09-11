"""
워드(.docx) 보고서 생성
======================

* 모든 텍스트는 sanitizer 를 통과한 블록만 사용한다. 본 모듈은 마지막 관문으로,
  문서를 조립한 뒤 **생성된 문서의 모든 런(run)을 다시 검사**해 마크다운 기호가
  남아 있으면 제거한다(최종 2차 감사). 따라서 파일에는 기호가 남을 수 없다.
* 한글 문서는 ``w:eastAsia`` 글꼴 지정이 없으면 Word에서 다른 글꼴로 대체되므로
  모든 런에 동아시아 글꼴을 함께 지정한다.
* 표지 + 본문 + 쪽번호 바닥글 구성의 전문 보고서 서식을 적용한다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Sequence

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.shared import Cm, Pt, RGBColor

from .sanitizer import Block, audit_text, scrub

__all__ = ["write_report_docx"]

# ── 색상 팔레트 (네이비 기반, 고급스러운 전문 보고서 톤) ──────────────
NAVY_DEEP = RGBColor(0x12, 0x23, 0x3F)    # 표지/대제목
NAVY = RGBColor(0x1B, 0x3A, 0x63)         # 제목 1
NAVY_MID = RGBColor(0x2C, 0x51, 0x7D)     # 제목 2
SLATE = RGBColor(0x3F, 0x4A, 0x5A)        # 제목 3 / 보조
BODY_INK = RGBColor(0x1F, 0x24, 0x2B)     # 본문
GOLD = RGBColor(0xA8, 0x85, 0x3C)         # 강조 라인
MUTED = RGBColor(0x6B, 0x75, 0x84)        # 표지 부가정보

KOREAN_FONT = "맑은 고딕"
LATIN_FONT = "Malgun Gothic"


# ───────────────────── 저수준 헬퍼 ─────────────────────
def _style_run(run, *, size: float, bold: bool = False, italic: bool = False, color: RGBColor | None = None) -> None:
    font = run.font
    font.name = LATIN_FONT
    font.size = Pt(size)
    font.bold = bold
    font.italic = italic
    if color is not None:
        font.color.rgb = color
    # 한글(동아시아) 글꼴 지정 — 없으면 Word가 임의 글꼴로 대체한다.
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:ascii"), LATIN_FONT)
    rfonts.set(qn("w:hAnsi"), LATIN_FONT)
    rfonts.set(qn("w:eastAsia"), KOREAN_FONT)
    rfonts.set(qn("w:cs"), LATIN_FONT)


def _paragraph_spacing(
    paragraph,
    *,
    before: float = 0,
    after: float = 6,
    line: float = 1.6,
    indent_left: float | None = None,
    first_line: float | None = None,
) -> None:
    fmt = paragraph.paragraph_format
    fmt.space_before = Pt(before)
    fmt.space_after = Pt(after)
    fmt.line_spacing = line
    if indent_left is not None:
        fmt.left_indent = Cm(indent_left)
    if first_line is not None:
        fmt.first_line_indent = Cm(first_line)
    # 한글 문서 가독성: 단어 단위 줄바꿈 허용, 문단 분리 방지
    ppr = paragraph._p.get_or_add_pPr()
    for tag, value in (("w:widowControl", "1"), ("w:wordWrap", "1")):
        el = ppr.find(qn(tag))
        if el is None:
            el = OxmlElement(tag)
            ppr.append(el)
        el.set(qn("w:val"), value)


def _bottom_border(paragraph, color: str = "1B3A63", size: int = 8) -> None:
    ppr = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(size))
    bottom.set(qn("w:space"), "4")
    bottom.set(qn("w:color"), color)
    borders.append(bottom)
    ppr.append(borders)


def _add_page_number_footer(section, label: str) -> None:
    """바닥글: 좌측 문서명 + 우측 'n / 총m' 쪽번호 필드."""
    footer = section.footer
    paragraph = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    paragraph.text = ""
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _paragraph_spacing(paragraph, before=0, after=0, line=1.0)

    left = paragraph.add_run(f"{label}    ")
    _style_run(left, size=8.5, color=MUTED)

    def field(instr: str) -> None:
        begin = OxmlElement("w:fldChar")
        begin.set(qn("w:fldCharType"), "begin")
        instr_el = OxmlElement("w:instrText")
        instr_el.set(qn("xml:space"), "preserve")
        instr_el.text = instr
        end = OxmlElement("w:fldChar")
        end.set(qn("w:fldCharType"), "end")
        run = paragraph.add_run()
        _style_run(run, size=8.5, color=MUTED)
        run._element.append(begin)
        run._element.append(instr_el)
        run._element.append(end)

    field(" PAGE ")
    dash = paragraph.add_run(" / ")
    _style_run(dash, size=8.5, color=MUTED)
    field(" NUMPAGES ")


def _set_margins(section) -> None:
    section.top_margin = Cm(2.4)
    section.bottom_margin = Cm(2.2)
    section.left_margin = Cm(2.5)
    section.right_margin = Cm(2.5)


# ───────────────────── 본체 ─────────────────────
def write_report_docx(
    path: str,
    *,
    title: str,
    topic: str,
    blocks: Sequence[Block],
    generated_at: datetime,
    node_count: int,
    section_count: int,
) -> int:
    """
    보고서 워드 파일을 생성하고, 최종 감사에서 제거한 금지 문자 수를 반환한다.
    """
    document = Document()

    # 기본 스타일 (본문)
    normal = document.styles["Normal"]
    normal.font.name = LATIN_FONT
    normal.font.size = Pt(10.5)
    normal.font.color.rgb = BODY_INK
    normal_rpr = normal.element.get_or_add_rPr()
    rfonts = normal_rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = OxmlElement("w:rFonts")
        normal_rpr.insert(0, rfonts)
    rfonts.set(qn("w:eastAsia"), KOREAN_FONT)
    rfonts.set(qn("w:ascii"), LATIN_FONT)
    rfonts.set(qn("w:hAnsi"), LATIN_FONT)

    clean_title = scrub(title).strip() or "사업화 검토 보고서"
    clean_topic = scrub(topic).strip()

    section = document.sections[0]
    _set_margins(section)

    # ── 표지 ──────────────────────────────────────────────
    spacer = document.add_paragraph()
    _paragraph_spacing(spacer, before=120, after=0, line=1.0)

    label_p = document.add_paragraph()
    label_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _paragraph_spacing(label_p, before=0, after=10, line=1.2)
    label_run = label_p.add_run("B U S I N E S S   R E P O R T")
    _style_run(label_run, size=10, bold=True, color=GOLD)

    title_p = document.add_paragraph()
    title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _paragraph_spacing(title_p, before=0, after=14, line=1.35)
    title_run = title_p.add_run(clean_title)
    _style_run(title_run, size=24, bold=True, color=NAVY_DEEP)

    rule_p = document.add_paragraph()
    rule_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _paragraph_spacing(rule_p, before=0, after=18, line=1.0)
    _bottom_border(rule_p, color="A8853C", size=12)

    if clean_topic:
        topic_p = document.add_paragraph()
        topic_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _paragraph_spacing(topic_p, before=0, after=28, line=1.4)
        topic_run = topic_p.add_run(f"검토 주제 : {clean_topic}")
        _style_run(topic_run, size=12, color=SLATE)

    meta_lines = (
        f"작성일   {generated_at.strftime('%Y년 %m월 %d일')}",
        f"구성      핵심 영역 {section_count}개 · 세부 검토 과제 {node_count}건",
        "용도      내부 검토 및 의사결정 참고용",
    )
    for line in meta_lines:
        meta_p = document.add_paragraph()
        meta_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _paragraph_spacing(meta_p, before=0, after=4, line=1.3)
        meta_run = meta_p.add_run(line)
        _style_run(meta_run, size=9.5, color=MUTED)

    # 표지 다음 페이지부터 본문
    break_p = document.add_paragraph()
    _paragraph_spacing(break_p, before=0, after=0, line=1.0)
    break_p.add_run().add_break(WD_BREAK.PAGE)

    # ── 본문 ──────────────────────────────────────────────
    heading_specs = {
        1: (15.0, NAVY, 18.0, 8.0, True),
        2: (12.5, NAVY_MID, 14.0, 6.0, False),
        3: (11.5, SLATE, 12.0, 4.0, False),
        4: (11.0, SLATE, 10.0, 4.0, False),
        5: (10.5, SLATE, 9.0, 3.0, False),
    }

    for block in blocks:
        if not block.runs:
            continue

        if block.kind == "heading":
            level = min(5, max(1, int(block.level) or 1))
            size, color, before, after, ruled = heading_specs[level]
            paragraph = document.add_paragraph()
            _paragraph_spacing(paragraph, before=before, after=after, line=1.35)
            paragraph.paragraph_format.keep_with_next = True
            for run_spec in block.runs:
                run = paragraph.add_run(run_spec.text)
                _style_run(run, size=size, bold=True, color=color)
            if ruled:
                _bottom_border(paragraph, color="C9D2DE", size=6)
            continue

        if block.kind in ("bullet", "numbered"):
            paragraph = document.add_paragraph(
                style="List Bullet" if block.kind == "bullet" else "List Number"
            )
            _paragraph_spacing(paragraph, before=0, after=4, line=1.5, indent_left=0.8)
            for run_spec in block.runs:
                run = paragraph.add_run(run_spec.text)
                _style_run(
                    run,
                    size=10.5,
                    bold=run_spec.bold,
                    italic=run_spec.italic,
                    color=BODY_INK,
                )
            continue

        paragraph = document.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        _paragraph_spacing(paragraph, before=0, after=8, line=1.65, first_line=0.4)
        for run_spec in block.runs:
            run = paragraph.add_run(run_spec.text)
            _style_run(
                run, size=10.5, bold=run_spec.bold, italic=run_spec.italic, color=BODY_INK
            )

    _add_page_number_footer(section, clean_title)

    # ── 최종 2차 감사: 생성된 문서의 모든 런을 재검사 ──────────
    violations = _audit_document(document)

    document.core_properties.title = clean_title
    document.core_properties.subject = clean_topic
    document.core_properties.author = "사업기획 검토"
    document.core_properties.comments = "내부 검토용 보고서"
    document.save(path)
    return violations


def _audit_document(document) -> int:
    """
    문서 내 모든 단락(본문·표·머리글/바닥글 포함)의 런을 검사하여
    금지 문자가 남아 있으면 제거한다. 반환값은 제거한 문자 수.
    """
    violations = 0

    def check(paragraphs: Iterable) -> int:
        removed = 0
        for paragraph in paragraphs:
            for run in paragraph.runs:
                found = audit_text(run.text)
                if found:
                    removed += found
                    run.text = scrub(run.text)
        return removed

    violations += check(document.paragraphs)
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                violations += check(cell.paragraphs)
    for section in document.sections:
        violations += check(section.footer.paragraphs)
        violations += check(section.header.paragraphs)
    return violations
