"""
프로젝트 소스 zip 패키징
=======================

로컬 실행용으로 Flask 폴더 구조를 그대로 담은 zip을 제공한다.
* 화이트리스트 방식이므로 ``.env``, ``.git``, ``instance/``, 생성된 보고서,
  가상환경 같은 민감/불필요 파일이 포함될 여지가 없다.
* 소스가 바뀌지 않으면 만들어 둔 zip 바이트를 재사용한다(최대 1개만 보관하여
  메모리 사용량이 누적되지 않는다).
"""
from __future__ import annotations

import io
import threading
import zipfile
from pathlib import Path

__all__ = ["build_source_zip"]

# 패키지에 포함할 항목 (파일 또는 디렉터리)
INCLUDE_FILES = (
    "app.py",
    "wsgi.py",
    "config.py",
    "requirements.txt",
    "Procfile",
    "runtime.txt",
    "README.md",
    ".env.example",
    ".gitignore",
)
INCLUDE_DIRS = ("models", "routes", "services", "static", "templates")

EXCLUDE_DIR_NAMES = {"__pycache__", ".git", ".venv", "venv", "node_modules", ".pytest_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".pyd", ".log", ".sqlite3", ".db"}

_lock = threading.Lock()
_cache: tuple[int, bytes] | None = None  # (소스 서명, zip 바이트)


def _iter_source_files(base: Path) -> list[Path]:
    files: list[Path] = []
    for name in INCLUDE_FILES:
        path = base / name
        if path.is_file():
            files.append(path)
    for dirname in INCLUDE_DIRS:
        root = base / dirname
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in EXCLUDE_DIR_NAMES for part in path.parts):
                continue
            if path.suffix.lower() in EXCLUDE_SUFFIXES:
                continue
            files.append(path)
    return files


def _signature(files: list[Path]) -> int:
    """파일 목록·크기·수정시각을 합친 간단한 서명(변경 감지용)."""
    total = len(files)
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        total = (total * 1_000_003 + int(stat.st_mtime_ns) + stat.st_size) & 0xFFFFFFFFFFFF
    return total


def build_source_zip(base_dir: str | Path, *, root_name: str = "idea_mece") -> bytes:
    """Flask 구조를 유지한 zip 바이트를 반환한다."""
    global _cache
    base = Path(base_dir).resolve()
    files = _iter_source_files(base)
    signature = _signature(files)

    with _lock:
        if _cache is not None and _cache[0] == signature:
            return _cache[1]

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in files:
            archive.write(path, arcname=f"{root_name}/{path.relative_to(base).as_posix()}")
        # 런타임 디렉터리는 빈 상태로 포함 (구조 유지)
        archive.writestr(f"{root_name}/downloads/.gitkeep", "")
        archive.writestr(f"{root_name}/instance/.gitkeep", "")
    data = buffer.getvalue()
    buffer.close()

    with _lock:
        _cache = (signature, data)  # 항상 1개만 보관 → 메모리 누적 없음
    return data
