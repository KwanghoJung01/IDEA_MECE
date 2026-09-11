"""
생성 파일 관리
=============

* 다운로드는 **토큰 → 세션에 보관된 내부 파일명** 매핑으로만 이루어진다.
  사용자가 보낸 문자열이 파일 경로에 직접 쓰이는 일이 없으므로
  경로 조작(``../``)이나 타인 파일 접근이 원천적으로 불가능하다.
* 내부 파일명은 서버가 만든 16진 토큰으로만 구성되며, 정규식으로 한 번 더 검증한다.
* TTL이 지난 파일은 생성 시점마다 일괄 정리해 디스크 누적을 막는다.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import time
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["STORED_NAME_RE", "new_stored_name", "resolve", "safe_display_name", "sweep"]

STORED_NAME_RE = re.compile(r"^[0-9a-f]{32}\.docx$")
_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def new_stored_name() -> str:
    """추측 불가능한 내부 저장 파일명."""
    return f"{secrets.token_hex(16)}.docx"


def safe_display_name(topic: str, generated_at: time.struct_time | None = None) -> str:
    """
    사용자에게 보여줄 다운로드 파일명.
    경로 구분자·제어문자를 제거하므로 헤더 인젝션이나 경로 조작에 쓰일 수 없다.
    """
    base = _UNSAFE_FILENAME_RE.sub(" ", str(topic or "")).strip()
    base = re.sub(r"\s{2,}", " ", base)[:60].strip(" .")
    if not base:
        base = "사업화 검토"
    stamp = time.strftime("%Y%m%d_%H%M", generated_at or time.localtime())
    return f"{base}_검토보고서_{stamp}.docx"


def resolve(download_dir: str, stored_name: str) -> Path | None:
    """내부 파일명을 검증하고 실제 경로를 반환한다(없거나 형식 위반이면 None)."""
    if not stored_name or not STORED_NAME_RE.match(stored_name):
        return None
    directory = Path(download_dir).resolve()
    target = (directory / stored_name).resolve()
    # 심볼릭 링크 등으로 디렉터리를 벗어나는 경우를 차단한다.
    if target.parent != directory or not target.is_file():
        return None
    return target


def sweep(download_dir: str, ttl_seconds: int) -> int:
    """TTL이 지난 생성 파일을 삭제하고 삭제 건수를 반환한다."""
    directory = Path(download_dir)
    if not directory.is_dir():
        return 0
    cutoff = time.time() - max(60, int(ttl_seconds))
    removed = 0
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return 0
    for entry in entries:
        try:
            if not entry.is_file() or not STORED_NAME_RE.match(entry.name):
                continue
            if entry.stat().st_mtime < cutoff:
                os.unlink(entry.path)
                removed += 1
        except OSError:
            continue
    if removed:
        logger.info("만료 보고서 %d건 정리", removed)
    return removed
