"""一個專案的執行紀錄存取層。刻意用原生 sqlite3，不引入 ORM —— 資料表只有三張。

每個專案各有一個資料庫（<專案>/.ai-workflow-proj/local/ai-workflow.sqlite），
由 store/stores.py 的 StoreRegistry 管理實例。

執行緒安全：每個執行緒各自持有自己的連線（sqlite 連線不能跨執行緒共用）。
引擎在背景執行緒跑、Flask 在 request 執行緒回應，兩邊都會寫，所以開 WAL 模式
讓讀寫不互相擋。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA = Path(__file__).resolve().parent / "schema_project.sql"


def new_id(prefix: str) -> str:
    """時間排序在前的短 id，方便肉眼比對與當 branch 名稱。"""
    return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def open_connection(path: Path) -> sqlite3.Connection:
    """開一條 sqlite 連線。所有資料庫（中央註冊表、各專案的紀錄）都走這裡。

    WAL 讓讀寫不互相擋 —— 引擎在背景執行緒寫、Flask 在 request 執行緒讀。
    """
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._connect() as conn:
            conn.executescript(SCHEMA.read_text("utf-8"))

    # ------------------------------------------------------------ 連線

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.path)

    @property
    def conn(self) -> sqlite3.Connection:
        existing = getattr(self._local, "conn", None)
        if existing is None:
            existing = self._connect()
            self._local.conn = existing
        return existing

    def close(self) -> None:
        existing = getattr(self._local, "conn", None)
        if existing is not None:
            existing.close()
            self._local.conn = None

    # ----------------------------------------------------------- runs

    def create_run(
        self,
        graph: dict[str, Any],
        requirement: str,
        workflow_id: str | None = None,
        project_id: str = "",
        project_path: str = "",
    ) -> str:
        run_id = new_id("run")
        self.conn.execute(
            """
            INSERT INTO runs (id, workflow_id, workflow_name, project_id,
                              project_path, graph, requirement, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)
            """,
            (run_id, workflow_id, graph.get("name") or "", project_id, project_path,
             json.dumps(graph, ensure_ascii=False), requirement, time.time()),
        )
        return run_id

    def start_run(self, run_id: str, branch: str, base_sha: str, worktree: str) -> None:
        self.conn.execute(
            """
            UPDATE runs SET status='running', started_at=?, branch=?, base_sha=?,
                            worktree=? WHERE id=?
            """,
            (time.time(), branch, base_sha, worktree, run_id),
        )

    def finish_run(self, run_id: str, status: str, reason: str, steps: int) -> None:
        self.conn.execute(
            "UPDATE runs SET status=?, reason=?, steps=?, finished_at=? WHERE id=?",
            (status, reason, steps, time.time(), run_id),
        )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        run = dict(row)
        run["graph"] = json.loads(run["graph"])
        run["nodes"] = self.get_node_runs(run_id)
        return run

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        """這個專案的執行紀錄。跨專案的總覽是 stores.merge_runs 的事。"""
        rows = self.conn.execute(
            """
            SELECT id, workflow_id, workflow_name, project_id, project_path,
                   requirement, status, reason, branch, steps,
                   created_at, started_at, finished_at
            FROM runs ORDER BY created_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def unfinished_runs(self) -> list[str]:
        """服務重啟後，狀態卡在 running 的 run —— 那些引擎執行緒已經不存在了。"""
        rows = self.conn.execute(
            "SELECT id FROM runs WHERE status IN ('queued','running')"
        ).fetchall()
        return [r["id"] for r in rows]

    # ------------------------------------------------------ node_runs

    def save_node_run(self, run_id: str, node_id: str, **fields: Any) -> None:
        payload = {
            "label": fields.get("label", ""),
            "node_type": fields.get("node_type", ""),
            "status": fields.get("status", "pending"),
            "visits": int(fields.get("visits", 0)),
            "last_message": fields.get("last_message", "") or "",
            "structured": json.dumps(fields.get("structured"), ensure_ascii=False)
            if fields.get("structured") is not None
            else None,
            "session_id": fields.get("session_id", "") or "",
            "exit_code": fields.get("exit_code"),
            "files": json.dumps(fields.get("files") or [], ensure_ascii=False),
            "usage": json.dumps(fields.get("usage") or {}, ensure_ascii=False),
        }
        self.conn.execute(
            f"""
            INSERT INTO node_runs (run_id, node_id, {", ".join(payload)})
            VALUES (?, ?, {", ".join("?" * len(payload))})
            ON CONFLICT(run_id, node_id) DO UPDATE SET
                {", ".join(f"{k}=excluded.{k}" for k in payload)}
            """,
            (run_id, node_id, *payload.values()),
        )

    def get_node_runs(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM node_runs WHERE run_id=?", (run_id,)
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["structured"] = (
                json.loads(item["structured"]) if item["structured"] else None
            )
            item["files"] = json.loads(item["files"])
            item["usage"] = json.loads(item["usage"])
            out.append(item)
        return out

    # --------------------------------------------------------- events

    def append_events(self, run_id: str, events: Iterable[dict[str, Any]]) -> None:
        rows = [
            (
                run_id,
                e["seq"],
                e.get("node_id", ""),
                e.get("ts", time.time()),
                e["kind"],
                e.get("text", ""),
                json.dumps(e.get("data") or {}, ensure_ascii=False, default=str),
            )
            for e in events
        ]
        if not rows:
            return
        # 同一個 seq 重複寫入時忽略（重連補送時可能發生）
        self.conn.executemany(
            """
            INSERT INTO events (run_id, seq, node_id, ts, kind, text, data)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, seq) DO NOTHING
            """,
            rows,
        )

    def get_events(
        self, run_id: str, after_seq: int = 0, limit: int = 5000
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT seq, node_id, ts, kind, text, data FROM events
            WHERE run_id=? AND seq > ? ORDER BY seq LIMIT ?
            """,
            (run_id, after_seq, limit),
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["data"] = json.loads(item["data"])
            out.append(item)
        return out

    def event_count(self, run_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE run_id=?", (run_id,)
        ).fetchone()
        return int(row["n"])
