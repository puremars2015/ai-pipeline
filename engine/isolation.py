"""隔離模式：節點在哪個工作目錄上動手。

兩種模式，由工作流的 settings.isolation 決定：

**shared**（預設）
  整個 run 一個 worktree，所有節點接力。會寫檔的節點搶同一把寫入鎖，實際是
  序列化的；只有唯讀節點真的平行。簡單、直觀、diff 一路累積。
  節點之間不 commit，整個 run 跑完由 service 收尾 commit 到 task/<run-id>。

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

from engine.graph import safe_name
from engine.workspace import (
    Workspace,
    WorkspaceError,
    create_node_workspace,
    point_branch_at,
    tip_commits,
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
        # 共用模式下節點之間靠同一個工作目錄接力，中途要不要 commit 由使用者
        # 用 git 節點決定。整個 run 的成果則由 service 在收尾時 commit 上
        # task/<run-id>，不然 branch 會空著、worktree 一清成果就沒了。
        return False

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
    # 內部名稱 → 節點 id。兩個節點對應到同一個名稱就代表會共用工作目錄，
    # 後建的會刪掉前一個（可能還在執行中），所以在真的動手前先擋下來。
    _claimed: dict[str, str] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # 收尾用的整合 worktree 名稱（合併多個終端分支時才會建）
    INTEGRATE = "__integrate"

    def acquire(self, node_id: str, base_commits: list[str]) -> Workspace:
        # git worktree add / remove 會動到共用的 .git，不能真的並行呼叫
        with self._lock:
            safe = safe_name(node_id)
            owner = self._claimed.setdefault(safe, node_id)
            if owner != node_id:
                raise WorkspaceError(
                    f"節點 {node_id!r} 與 {owner!r} 會對應到同一個工作目錄 "
                    f"{safe} —— 拒絕覆蓋還在使用中的目錄"
                )
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

    def finalise(self, run_branch: str, commits: list[str]) -> str | None:
        """讓 task/<run-id> 代表整個 run 的完整結果。

        不能用「最後完成的那個 commit」—— 圖可以 fan-out 成兩個各自結束的分支，
        兩邊的 commit 互不包含，挑一個就會靜默漏掉另一邊，而且挑到哪個還取決於
        執行時序。做法是先算出所有 tip（沒有被其他人包含的 commit），只有一個就
        直接指過去，多個就在一個獨立的整合 worktree 裡合併起來。

        合併衝突會往外丟 MergeConflict —— 呼叫端應該讓整個 run 失敗，
        而不是安靜地只採用其中一邊。
        """
        tips = tip_commits(self.repo, [c for c in commits if c])
        if not tips:
            return None

        if len(tips) == 1:
            point_branch_at(self.repo, run_branch, tips[0])
            return tips[0]

        integrate = create_node_workspace(
            repo=self.repo,
            worktree_root=self.worktree_root,
            run_id=self.run_id,
            node_id=self.INTEGRATE,
            base_commits=tips,
            run_base_sha=self.run_base_sha,
            tool_root=self.tool_root,
        )
        with self._lock:
            self.workspaces[self.INTEGRATE] = integrate
        merged = integrate.head()
        point_branch_at(self.repo, run_branch, merged)
        return merged

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
