"""
무한 확장 트리 데이터 모델 (SQLite)
==================================

설계 요점
---------
* **인접 리스트 + 물질화 경로(materialized path) 혼합 구조**
  - ``parent_id`` : 부모-자식 관계(재귀적 구조, 깊이 제한 없음)
  - ``path``      : ``/1/7/23/`` 형태의 조상 경로 문자열
  경로 컬럼이 있기 때문에 하위 전체(subtree) 조회·삭제를 재귀 없이
  단일 인덱스 스캔(``path LIKE '/1/7/%'``)으로 처리한다.
  깊이가 수십 단계로 늘어나도 쿼리 비용이 선형 이상으로 증가하지 않는다.

* **조상 조회도 추가 쿼리 없이 처리**
  ``path``에 조상 id가 모두 들어있어, 경로를 파싱한 뒤 ``IN (...)`` 한 번으로
  조상 전체를 가져온다. (재귀 CTE 대비 계획 수립 비용이 없음)

* **세션 격리**
  프로젝트는 브라우저 세션이 보유한 난수 ``owner_key``에 묶인다.
  모든 노드 접근은 ``project_id``로 한 번 더 검증하므로, 다른 사용자의
  노드 id를 알아도 조회/수정/삭제가 불가능하다(IDOR 차단).

* **커넥션 관리**
  요청(앱 컨텍스트)당 1개의 커넥션을 ``flask.g``에 보관하고 teardown에서
  반드시 닫는다. 스레드 간 커넥션 공유가 없으므로 누수·경쟁 조건이 없다.
"""
from __future__ import annotations

import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

from flask import current_app, g

__all__ = [
    "NodeLimitError",
    "TreeRepository",
    "close_db",
    "get_db",
    "init_app",
    "init_db",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_key   TEXT    NOT NULL UNIQUE,
    topic       TEXT    NOT NULL,
    created_at  REAL    NOT NULL,
    updated_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    parent_id   INTEGER REFERENCES nodes(id) ON DELETE CASCADE,
    depth       INTEGER NOT NULL DEFAULT 0,
    path        TEXT    NOT NULL DEFAULT '/',
    title       TEXT    NOT NULL,
    content     TEXT    NOT NULL DEFAULT '',
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  REAL    NOT NULL
);

-- 형제 노드 정렬 조회 및 자식 조회 최적화
CREATE INDEX IF NOT EXISTS idx_nodes_parent
    ON nodes (project_id, parent_id, sort_order, id);
-- 하위 전체(subtree) 접두사 검색 최적화
CREATE INDEX IF NOT EXISTS idx_nodes_path
    ON nodes (project_id, path);
-- 프로젝트 단위 전체 로딩 최적화
CREATE INDEX IF NOT EXISTS idx_nodes_project_depth
    ON nodes (project_id, depth, sort_order);
"""


class NodeLimitError(RuntimeError):
    """노드 수/깊이 한도를 초과했을 때 발생."""


# ───────────────────────── 커넥션 관리 ─────────────────────────
def get_db() -> sqlite3.Connection:
    """요청 범위 커넥션을 반환한다(없으면 생성)."""
    conn = getattr(g, "_idea_db", None)
    if conn is None:
        db_path = Path(current_app.config["DATABASE_PATH"])
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            db_path,
            timeout=15.0,
            isolation_level=None,          # 명시적 트랜잭션 제어
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")       # 읽기/쓰기 동시성
            cur.execute("PRAGMA synchronous=NORMAL")     # 성능/안정성 균형
            cur.execute("PRAGMA foreign_keys=ON")        # cascade 보장
            cur.execute("PRAGMA busy_timeout=15000")
            cur.execute("PRAGMA temp_store=MEMORY")
            cur.execute("PRAGMA cache_size=-8000")       # 약 8MB 페이지 캐시
        finally:
            cur.close()
        g._idea_db = conn
    return conn


def close_db(_exc: BaseException | None = None) -> None:
    """요청 종료 시 커넥션을 확실히 닫아 누수를 막는다."""
    conn = g.pop("_idea_db", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def init_db(app) -> None:
    db_path = Path(app.config["DATABASE_PATH"])
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15.0)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def init_app(app) -> None:
    app.teardown_appcontext(close_db)
    init_db(app)


# ───────────────────────── 리포지토리 ─────────────────────────
class TreeRepository:
    """트리 CRUD. 모든 메서드는 세션 소유 프로젝트 범위 안에서만 동작한다."""

    __slots__ = ("_conn", "_max_nodes", "_max_depth")

    def __init__(self, conn: sqlite3.Connection | None = None) -> None:
        self._conn = conn or get_db()
        self._max_nodes = int(current_app.config["MAX_NODES_PER_PROJECT"])
        self._max_depth = int(current_app.config["MAX_TREE_DEPTH"])

    # ── 프로젝트 ──────────────────────────────────────────────
    @staticmethod
    def new_owner_key() -> str:
        """예측 불가능한 세션 소유키."""
        return secrets.token_urlsafe(32)

    def get_project_by_owner(self, owner_key: str) -> sqlite3.Row | None:
        if not owner_key:
            return None
        return self._conn.execute(
            "SELECT id, owner_key, topic, created_at, updated_at"
            "  FROM projects WHERE owner_key = ?",
            (owner_key,),
        ).fetchone()

    def reset_project(self, owner_key: str, topic: str) -> int:
        """기존 트리를 모두 지우고 새 주제로 프로젝트를 생성/초기화한다."""
        now = time.time()
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT id FROM projects WHERE owner_key = ?", (owner_key,)
            ).fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO projects (owner_key, topic, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?)",
                    (owner_key, topic, now, now),
                )
                project_id = int(cur.lastrowid)
            else:
                project_id = int(row["id"])
                conn.execute("DELETE FROM nodes WHERE project_id = ?", (project_id,))
                conn.execute(
                    "UPDATE projects SET topic = ?, updated_at = ? WHERE id = ?",
                    (topic, now, project_id),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return project_id

    def delete_project(self, project_id: int) -> None:
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM nodes WHERE project_id = ?", (project_id,))
            conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def touch_project(self, project_id: int) -> None:
        self._conn.execute(
            "UPDATE projects SET updated_at = ? WHERE id = ?", (time.time(), project_id)
        )

    # ── 조회 ──────────────────────────────────────────────────
    def count_nodes(self, project_id: int) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM nodes WHERE project_id = ?", (project_id,)
        ).fetchone()
        return int(row["c"]) if row else 0

    def get_node(self, project_id: int, node_id: int) -> sqlite3.Row | None:
        """project_id를 함께 검증하여 타 사용자 노드 접근을 차단한다."""
        return self._conn.execute(
            "SELECT id, project_id, parent_id, depth, path, title, content, sort_order"
            "  FROM nodes WHERE id = ? AND project_id = ?",
            (node_id, project_id),
        ).fetchone()

    def list_all(self, project_id: int) -> list[sqlite3.Row]:
        """프로젝트 전체 노드를 계층 조립에 적합한 순서로 한 번에 읽는다."""
        return self._conn.execute(
            "SELECT id, parent_id, depth, title, content, sort_order"
            "  FROM nodes WHERE project_id = ?"
            "  ORDER BY depth, parent_id, sort_order, id",
            (project_id,),
        ).fetchall()

    def list_children(self, project_id: int, parent_id: int | None) -> list[sqlite3.Row]:
        if parent_id is None:
            return self._conn.execute(
                "SELECT id, parent_id, depth, title, content, sort_order"
                "  FROM nodes WHERE project_id = ? AND parent_id IS NULL"
                "  ORDER BY sort_order, id",
                (project_id,),
            ).fetchall()
        return self._conn.execute(
            "SELECT id, parent_id, depth, title, content, sort_order"
            "  FROM nodes WHERE project_id = ? AND parent_id = ?"
            "  ORDER BY sort_order, id",
            (project_id, parent_id),
        ).fetchall()

    def list_ancestors(self, project_id: int, node: sqlite3.Row) -> list[sqlite3.Row]:
        """경로 문자열을 파싱해 조상 전체를 단일 쿼리로 가져온다(루트→부모 순)."""
        ids = self._parse_path_ids(node["path"])
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"SELECT id, parent_id, depth, title, content FROM nodes"
            f" WHERE project_id = ? AND id IN ({placeholders})",
            (project_id, *ids),
        ).fetchall()
        order = {nid: i for i, nid in enumerate(ids)}
        return sorted(rows, key=lambda r: order.get(int(r["id"]), 0))

    def list_subtree(self, project_id: int, node: sqlite3.Row) -> list[sqlite3.Row]:
        """자신을 포함한 하위 전체를 경로 접두사 검색으로 가져온다."""
        prefix = self._subtree_prefix(node)
        return self._conn.execute(
            "SELECT id, parent_id, depth, title, content, sort_order"
            "  FROM nodes"
            "  WHERE project_id = ? AND (id = ? OR path LIKE ? ESCAPE '\\')"
            "  ORDER BY depth, parent_id, sort_order, id",
            (project_id, int(node["id"]), prefix + "%"),
        ).fetchall()

    def count_subtree(self, project_id: int, node: sqlite3.Row) -> int:
        prefix = self._subtree_prefix(node)
        row = self._conn.execute(
            "SELECT COUNT(*) AS c FROM nodes"
            " WHERE project_id = ? AND (id = ? OR path LIKE ? ESCAPE '\\')",
            (project_id, int(node["id"]), prefix + "%"),
        ).fetchone()
        return int(row["c"]) if row else 0

    # ── 변경 ──────────────────────────────────────────────────
    def add_children(
        self,
        project_id: int,
        parent: sqlite3.Row | None,
        items: Sequence[tuple[str, str]],
    ) -> list[dict[str, Any]]:
        """
        자식 노드를 일괄 추가한다. 하나의 트랜잭션으로 처리되어
        중간 실패 시 부분 생성이 남지 않는다.
        items: [(title, content), ...]
        """
        if not items:
            return []

        parent_id = int(parent["id"]) if parent is not None else None
        parent_path = parent["path"] if parent is not None else "/"
        depth = (int(parent["depth"]) + 1) if parent is not None else 0

        if depth > self._max_depth:
            raise NodeLimitError(f"트리 최대 깊이({self._max_depth}단계)를 초과했습니다.")

        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM nodes WHERE project_id = ?", (project_id,)
            ).fetchone()["c"]
            if int(total) + len(items) > self._max_nodes:
                raise NodeLimitError(
                    f"프로젝트당 최대 노드 수({self._max_nodes}개)를 초과합니다. "
                    "불필요한 가지를 삭제한 뒤 다시 시도해 주세요."
                )

            if parent_id is None:
                row = conn.execute(
                    "SELECT COALESCE(MAX(sort_order), -1) AS m FROM nodes"
                    " WHERE project_id = ? AND parent_id IS NULL",
                    (project_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COALESCE(MAX(sort_order), -1) AS m FROM nodes"
                    " WHERE project_id = ? AND parent_id = ?",
                    (project_id, parent_id),
                ).fetchone()
            next_order = int(row["m"]) + 1

            now = time.time()
            created: list[dict[str, Any]] = []
            for offset, (title, content) in enumerate(items):
                cur = conn.execute(
                    "INSERT INTO nodes"
                    " (project_id, parent_id, depth, path, title, content, sort_order, created_at)"
                    " VALUES (?, ?, ?, '', ?, ?, ?, ?)",
                    (project_id, parent_id, depth, title, content, next_order + offset, now),
                )
                node_id = int(cur.lastrowid)
                node_path = f"{parent_path}{node_id}/"
                conn.execute("UPDATE nodes SET path = ? WHERE id = ?", (node_path, node_id))
                created.append(
                    {
                        "id": node_id,
                        "parent_id": parent_id,
                        "depth": depth,
                        "title": title,
                        "content": content,
                        "sort_order": next_order + offset,
                        "child_count": 0,
                    }
                )
            conn.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?", (now, project_id)
            )
            conn.execute("COMMIT")
            return created
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def update_node(self, project_id: int, node_id: int, title: str, content: str) -> bool:
        cur = self._conn.execute(
            "UPDATE nodes SET title = ?, content = ? WHERE id = ? AND project_id = ?",
            (title, content, node_id, project_id),
        )
        if cur.rowcount:
            self.touch_project(project_id)
            return True
        return False

    def delete_subtree(self, project_id: int, node: sqlite3.Row) -> int:
        """
        노드와 모든 하위 노드를 한 번의 DELETE로 제거(cascade).
        경로 인덱스를 사용하므로 깊은 트리에서도 빠르다.

        ※ 삭제 건수는 DELETE 전에 미리 센다.
          외래키 CASCADE가 문장 실행 중에 하위 행을 먼저 지우기 때문에
          ``cursor.rowcount``에는 직접 삭제된 1건만 집계된다(하위는 누락).
        """
        prefix = self._subtree_prefix(node)
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM nodes"
                " WHERE project_id = ? AND (id = ? OR path LIKE ? ESCAPE '\\')",
                (project_id, int(node["id"]), prefix + "%"),
            ).fetchone()
            removed = int(row["c"]) if row else 0
            conn.execute(
                "DELETE FROM nodes"
                " WHERE project_id = ? AND (id = ? OR path LIKE ? ESCAPE '\\')",
                (project_id, int(node["id"]), prefix + "%"),
            )
            conn.execute(
                "UPDATE projects SET updated_at = ? WHERE id = ?",
                (time.time(), project_id),
            )
            conn.execute("COMMIT")
            return removed
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ── 헬퍼 ──────────────────────────────────────────────────
    @staticmethod
    def _parse_path_ids(path: str | None) -> list[int]:
        """'/1/7/23/' → [1, 7] (자신 id 제외)."""
        if not path:
            return []
        parts = [p for p in str(path).split("/") if p]
        ids: list[int] = []
        for p in parts[:-1]:  # 마지막은 자기 자신
            try:
                ids.append(int(p))
            except ValueError:
                continue
        return ids

    @staticmethod
    def _subtree_prefix(node: sqlite3.Row) -> str:
        """
        LIKE 접두사를 안전하게 만든다. 경로는 숫자와 '/'로만 구성되지만
        방어적으로 와일드카드 문자를 이스케이프한다.
        """
        raw = node["path"] or f"/{int(node['id'])}/"
        return raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    # ── 렌더링용 직렬화 ───────────────────────────────────────
    @staticmethod
    def serialize(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
        """프론트엔드 트리 조립용 평면 배열 (자식 수 포함)."""
        items = [
            {
                "id": int(r["id"]),
                "parent_id": int(r["parent_id"]) if r["parent_id"] is not None else None,
                "depth": int(r["depth"]),
                "title": r["title"],
                "content": r["content"],
                "sort_order": int(r["sort_order"]),
                "child_count": 0,
            }
            for r in rows
        ]
        index = {it["id"]: it for it in items}
        for it in items:
            parent = index.get(it["parent_id"]) if it["parent_id"] is not None else None
            if parent is not None:
                parent["child_count"] += 1
        return items
