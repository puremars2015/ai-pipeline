"""隔離模式：節點在哪個工作目錄上動手。

兩種模式，由工作流的 settings.isolation 決定：

**shared**（預設）
  整個 run 一個 worktree，所有節點接力。會寫檔的節點搶同一把寫入鎖，實際是
  序列化的；只有唯讀節點真的平行。簡單、直觀、diff 一路累積。

**per_node**
  每個節點自己的 worktree + branch，從上游節點的產出 commit 開始。
  沒有共用狀態就不需要鎖 —— 會寫檔的節點也能真的平行跑。代價是：

  - 每個會寫檔的節點跑完必須自動 commit，否則結果無法交給下游
  - fan-in 的節點要合併多個上游的 commit，可能衝突（兩個平行節點改同一處）
  - 磁碟用量是「節點數 × repo 大小」

選哪個是取捨，不是優劣：per_node 換到真平行，代價是要處理合併衝突。
"""

from __future__ import annotations

import shutil
import threading
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import ContextManager, Protocol

from engine.workspace import (
    Workspace,
    create_node_workspace,
    point_branch_at,
)

SHARED = "shared"
PER_NODE = "per_node"
MODES = (SHARED, PER_NODE)


class Isolation(Protocol):
    mode: str

    def acquire(self, node_id: str, base_commits: list[str]) -> Workspace:
        """取得這個節點要用的工作目錄。"""

    def write_lock(self) -> ContextManager:
        """會寫檔的節點要持有的鎖。per_node 模式不需要，回傳空的 context。"""

    def serialises_writes(self) -> bool:
        """會寫檔的節點是否被迫序列化（共用工作目錄時為真）。"""

    def write_lock_held(self) -> bool:
        """目前是否有節點持有寫入鎖 —— 用來提示使用者「正在等鎖」。"""

    def needs_autocommit(self) -> bool:
        """節點跑完是否必須自動 commit 才能把狀態傳給下游。"""

    def run_branch_commit(self) -> str | None:
        """整個 run 對外代表的 commit（給 task/<run-id> 用）。"""

    def cleanup(self) -> None: ...


@dataclass
class SharedIsolation:
    """整個 run 共用一個工作目錄。

    workspace 允許是 None（測試時沒有真的 repo）。即使沒有 worktree，寫入仍然
    要序列化 —— 節點還是共用同一個目錄，同時寫一樣會互相蓋掉。
    """

    workspace: Workspace | None
    mode: str = SHARED
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def acquire(self, node_id: str, base_commits: list[str]) -> Workspace | None:
        return self.workspace

    def write_lock(self) -> ContextManager:
        return self._lock

    def serialises_writes(self) -> bool:
        return True

    def write_lock_held(self) -> bool:
        return self._lock.locked()

    def needs_autocommit(self) -> bool:
        # 共用模式下要不要 commit 由使用者用 git 節點明確決定
        return False

    def run_branch_commit(self) -> str | None:
        return None  # branch 本來就在這個 worktree 上，不用另外指

    def cleanup(self) -> None:
        if self.workspace is not None:
            self.workspace.remove()


@dataclass
class PerNodeIsolation:
    """每個節點自己的 worktree。"""

    repo: Path
    worktree_root: Path
    run_id: str
    run_base_sha: str
    tool_root: Path | None = None
    mode: str = PER_NODE
    # node_id -> 該節點目前的 worktree
    workspaces: dict[str, Workspace] = field(default_factory=dict)
    # 依完成順序記錄的最後一個產出 commit
    last_commit: str | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def acquire(self, node_id: str, base_commits: list[str]) -> Workspace:
        # git worktree add / remove 會動到共用的 .git，不能真的並行呼叫
        with self._lock:
            workspace = create_node_workspace(
                repo=self.repo,
                worktree_root=self.worktree_root,
                run_id=self.run_id,
                node_id=node_id,
                base_commits=base_commits,
                run_base_sha=self.run_base_sha,
                tool_root=self.tool_root,
            )
            self.workspaces[node_id] = workspace
            return workspace

    def write_lock(self) -> ContextManager:
        # 每個節點各有工作目錄，不會互相蓋檔 —— 這就是 per_node 的重點
        return nullcontext()

    def serialises_writes(self) -> bool:
        return False

    def write_lock_held(self) -> bool:
        return False

    def needs_autocommit(self) -> bool:
        return True

    def record_commit(self, commit: str) -> None:
        with self._lock:
            self.last_commit = commit

    def run_branch_commit(self) -> str | None:
        return self.last_commit

    def finalise(self, run_branch: str) -> str | None:
        """讓 task/<run-id> 指向最後一個產出 commit。

        規則刻意選成「最後一個成功完成的節點的產出」—— 定義明確、隨時算得出來。
        想看特定節點的結果，它自己的 branch task/<run-id>/<node-id> 還在。
        """
        if self.last_commit is None:
            return None
        point_branch_at(self.repo, run_branch, self.last_commit)
        return self.last_commit

    def cleanup(self) -> None:
        for workspace in self.workspaces.values():
            workspace.remove()
        run_dir = self.worktree_root / self.run_id
        if run_dir.exists():
            shutil.rmtree(run_dir, ignore_errors=True)


def parse_mode(settings: dict) -> str:
    mode = (settings or {}).get("isolation") or SHARED
    if mode not in MODES:
        raise ValueError(f"isolation 只能是 {' / '.join(MODES)}，收到 {mode!r}")
    return mode
