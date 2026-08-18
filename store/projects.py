"""專案註冊表：這台機器上有哪些專案。

刻意只管「有哪些專案」這一件事，不碰檔案系統。驗證目標 repo 安不安全是
engine/workspace.validate_project_repo 的事，建立 .ai-workflow-proj/ 是
engine/project.scaffold 的事 —— 註冊表只負責記住結果。

分開的理由很實際：註冊一個專案是「驗證 → 初始化 → 記錄」三個步驟，
前兩步都可能失敗，而失敗時絕不能留下一筆指向壞掉路徑的紀錄。
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from store.db import open_connection

SCHEMA = Path(__file__).resolve().parent / "schema_registry.sql"

# 專案 id 會出現在網址與 worktree 的路徑裡，所以字元集必須同時對 URL 與
# 檔案系統安全。註冊時就轉好，之後全系統都可以直接信任它。
_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")
MAX_ID_LEN = 40


@dataclass(frozen=True)
class Project:
    id: str
    path: Path
    name: str
    added_at: float
    last_used_at: float | None = None


def slugify(text: str) -> str:
    return _SLUG_UNSAFE.sub("-", str(text).lower()).strip("-")[:MAX_ID_LEN] or "project"


def make_id(path: Path, taken: set[str]) -> str:
    """由資料夾名稱產生可讀的 id，撞名時補上路徑雜湊。

    用路徑的雜湊而不是流水號：同一個專案重新註冊時會拿到同一個 id，
    worktree 目錄與網址就不會每次都換一個。
    """
    base = slugify(Path(path).name)
    if base not in taken:
        return base

    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()
    for length in range(6, len(digest) + 1):
        candidate = f"{base}-{digest[:length]}"
        if candidate not in taken:
            return candidate
    raise RuntimeError(f"無法為 {path} 產生不重複的專案 id")


class ProjectRegistry:
    """中央資料庫裡的專案清單。每個執行緒各自持有連線。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.conn.executescript(SCHEMA.read_text("utf-8"))

    @property
    def conn(self) -> sqlite3.Connection:
        existing = getattr(self._local, "conn", None)
        if existing is None:
            existing = open_connection(self.path)
            self._local.conn = existing
        return existing

    def close(self) -> None:
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            existing.close()
            self._local.conn = None

    # ------------------------------------------------------------ 讀

    def _row(self, row: sqlite3.Row) -> Project:
        return Project(
            id=row["id"],
            path=Path(row["path"]),
            name=row["name"],
            added_at=row["added_at"],
            last_used_at=row["last_used_at"],
        )

    def list(self) -> list[Project]:
        """最近用過的排前面 —— 從沒跑過的專案排在後面比按字母排實用。"""
        rows = self.conn.execute(
            "SELECT * FROM projects ORDER BY COALESCE(last_used_at, added_at) DESC"
        ).fetchall()
        return [self._row(r) for r in rows]

    def get(self, project_id: str) -> Project | None:
        row = self.conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        return self._row(row) if row else None

    def get_by_path(self, path: Path) -> Project | None:
        row = self.conn.execute(
            "SELECT * FROM projects WHERE path = ?", (str(Path(path).resolve()),)
        ).fetchone()
        return self._row(row) if row else None

    # ------------------------------------------------------------ 寫

    def add(self, path: Path, name: str = "") -> Project:
        """記錄一個已經驗證過、也已經初始化過的專案。

        重複註冊同一個路徑會直接回傳既有的那一筆，不視為錯誤 —— 呼叫端
        通常只是想確保它在清單裡。
        """
        resolved = Path(path).resolve()
        existing = self.get_by_path(resolved)
        if existing:
            return existing

        taken = {p.id for p in self.list()}
        project = Project(
            id=make_id(resolved, taken),
            path=resolved,
            name=name or resolved.name,
            added_at=time.time(),
        )
        self.conn.execute(
            "INSERT INTO projects (id, path, name, added_at) VALUES (?, ?, ?, ?)",
            (project.id, str(project.path), project.name, project.added_at),
        )
        return project

    def rename(self, project_id: str, name: str) -> bool:
        cur = self.conn.execute(
            "UPDATE projects SET name = ? WHERE id = ?", (name, project_id)
        )
        return cur.rowcount > 0

    def touch(self, project_id: str) -> None:
        self.conn.execute(
            "UPDATE projects SET last_used_at = ? WHERE id = ?",
            (time.time(), project_id),
        )

    def remove(self, project_id: str) -> bool:
        """只從清單移除。專案資料夾裡的東西一概不動 —— 那是使用者的檔案，
        而且 .ai-workflow-proj/ 裡有已經 commit 進 git 的工作流定義。"""
        cur = self.conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        return cur.rowcount > 0
