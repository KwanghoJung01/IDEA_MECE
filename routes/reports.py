"""보고서 생성 및 다운로드 라우트."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from flask import Blueprint, current_app, send_file, session

from models import TreeRepository
from services import downloads
from services.docx_writer import write_report_docx
from services.gemini import GeminiError
from services.report import MAX_SECTIONS as MAX_REPORT_SECTIONS, ReportError, build_report
from services.security import csrf_protect, ensure_session

from .helpers import credentials_or_error, current_project, fail, ok, repository

logger = logging.getLogger(__name__)
bp = Blueprint("reports", __name__, url_prefix="/api/report")

MAX_TRACKED_REPORTS = 5      # 세션 쿠키 크기 억제
MAX_MAIL_BODY = 1200         # mailto 길이 한계 고려


def _register_report(token: str, stored: str, display: str) -> None:
    """세션에 토큰→내부파일명 매핑을 보관한다(최대 5건, 오래된 건 폐기)."""
    reports = session.get("reports")
    if not isinstance(reports, list):
        reports = []
    reports.append({"t": token, "f": stored, "n": display})
    if len(reports) > MAX_TRACKED_REPORTS:
        dropped = reports[:-MAX_TRACKED_REPORTS]
        reports = reports[-MAX_TRACKED_REPORTS:]
        # 추적이 끊긴 파일은 즉시 삭제해 디스크에 남지 않게 한다.
        for item in dropped:
            path = downloads.resolve(current_app.config["DOWNLOAD_DIR"], str(item.get("f", "")))
            if path is not None:
                try:
                    path.unlink()
                except OSError:
                    pass
    session["reports"] = reports


def _lookup_report(token: str) -> dict | None:
    for item in session.get("reports") or []:
        if isinstance(item, dict) and item.get("t") == token:
            return item
    return None


def _mail_payload(topic: str, summary: str, filename: str) -> dict[str, str]:
    subject = f"[검토 보고서] {topic}" if topic else "[검토 보고서] 사업화 검토"
    guide = (
        "\n\n──────────────────────────────\n"
        f"· 첨부 예정 파일: {filename}\n"
        "· 위 파일은 브라우저로 내려받은 뒤 이 메일에 직접 첨부해 주세요.\n"
        "  (브라우저 보안 정책상 첨부파일은 자동으로 붙지 않습니다)\n"
        "· 내용 확인 후 발송 버튼을 눌러 주시기 바랍니다.\n"
    )
    body = (summary or "").strip()
    if len(body) > MAX_MAIL_BODY:
        body = body[:MAX_MAIL_BODY].rstrip() + "…"
    intro = "안녕하십니까.\n\n요청하신 검토 보고서를 송부드립니다.\n\n[핵심 요약]\n"
    return {"subject": subject, "body": f"{intro}{body}{guide}"}


@bp.post("")
@csrf_protect
def create_report():
    cfg = current_app.config
    creds, error = credentials_or_error()
    if error:
        return error

    repo = repository()
    project = current_project(repo)
    if project is None:
        return fail("먼저 주제를 입력해 아이디어를 생성해 주세요.", 404)

    nodes = TreeRepository.serialize(repo.list_all(int(project["id"])))
    if not nodes:
        return fail("생성된 아이디어가 없습니다. 먼저 아이디어를 생성해 주세요.", 400)

    # 만료된 과거 파일 정리 (디스크 누적 방지)
    downloads.sweep(cfg["DOWNLOAD_DIR"], cfg["DOWNLOAD_TTL_SECONDS"])

    try:
        result = build_report(
            topic=str(project["topic"]),
            nodes=nodes,
            api_key=creds.api_key,
            model=creds.model,
            endpoint=cfg["GEMINI_ENDPOINT"],
            timeout=cfg["GEMINI_TIMEOUT"],
            workers=cfg["REPORT_WORKERS"],
        )
    except (ReportError, GeminiError) as exc:
        return fail(str(exc), getattr(exc, "status", 502))
    finally:
        del nodes  # 대용량 리스트 조기 해제

    stored = downloads.new_stored_name()
    directory = Path(cfg["DOWNLOAD_DIR"])
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / stored

    try:
        violations = write_report_docx(
            str(target),
            title=result.title,
            topic=result.topic,
            blocks=result.blocks,
            generated_at=result.generated_at,
            node_count=result.node_count,
            section_count=result.section_count,
        )
    except Exception:  # noqa: BLE001 - 파일 생성 실패는 사용자에게 일반 메시지로 알린다
        logger.exception("워드 파일 생성 실패")
        return fail("워드 파일을 만드는 중 오류가 발생했습니다. 다시 시도해 주세요.", 500)
    finally:
        result.blocks = []  # 블록 리스트 즉시 해제

    if violations or result.violations:
        logger.info("정화 단계에서 제거된 서식 기호: %d", violations + result.violations)

    display = downloads.safe_display_name(result.topic, time.localtime())
    token = downloads.new_stored_name().split(".")[0]
    _register_report(token, stored, display)

    warnings = []
    if result.omitted_sections:
        warnings.append(
            f"핵심 영역이 많아 상위 {MAX_REPORT_SECTIONS}개만 보고서에 담았습니다. "
            f"({result.omitted_sections}개 제외) 필요 없는 영역을 삭제한 뒤 다시 생성해 주세요."
        )
    if result.failed_sections:
        warnings.append(
            "일부 영역(" + ", ".join(result.failed_sections[:3]) +
            ")은 일시적 오류로 요약 수준으로 작성되었습니다. 다시 생성하면 보완됩니다."
        )

    return ok(
        filename=display,
        download_url=f"/api/report/{token}/download",
        summary=result.summary,
        section_count=result.section_count,
        node_count=result.node_count,
        mail=_mail_payload(result.topic, result.summary, display),
        warnings=warnings,
    )


@bp.get("/<token>/download")
def download_report(token: str):
    """
    토큰은 세션에 보관된 매핑으로만 해석된다.
    사용자 입력이 파일 경로로 쓰이지 않으므로 경로 조작이 불가능하다.
    """
    ensure_session()
    if not token or len(token) > 64:
        return fail("잘못된 요청입니다.", 400)

    item = _lookup_report(token)
    if item is None:
        return fail("다운로드 링크가 만료되었습니다. 보고서를 다시 생성해 주세요.", 404)

    path = downloads.resolve(current_app.config["DOWNLOAD_DIR"], str(item.get("f", "")))
    if path is None:
        return fail("보고서 파일이 만료되어 삭제되었습니다. 다시 생성해 주세요.", 410)

    response = send_file(
        path,
        as_attachment=True,
        download_name=str(item.get("n") or "report.docx"),
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        max_age=0,
        conditional=True,
    )
    response.headers["Cache-Control"] = "no-store, private"
    return response
