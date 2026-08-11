"""Git worktree 生命週期，以及「絕不在錯誤的 repo 上動手」的防線。

背景：這台機器上 /Users/<user>/.git 存在，整個家目錄是一個零 commit 的 git repo。
若 project_repo 指到家目錄（或任何 toplevel 解析成家目錄的子路徑），
`git worktree add` 會試圖把整個家目錄具體化成一份 worktree。
validate_project_repo() 就是為了讓那件事不可能發生。
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class WorkspaceError(Exception):
    """worktree / repo 操作失敗，或目標 repo 不安全。"""


class MergeConflict(WorkspaceError):
    """合併多個上游節點的結果時發生衝突。

    這是每節點獨立 worktree 模式下的真實風險：兩個平行節點改到同一個地方，
    在 fan-in 的節點上才會撞到。錯誤訊息要列出衝突檔案，不然使用者無從下手。
    """

    def __init__(self, message: str, files: list[str]) -> None:
        super().__init__(message)
        self.files = files


def _git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise WorkspaceError(
            f"git {' '.join(args)} 失敗 (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc


def git_toplevel(path: Path) -> Path | None:
    """回傳 path 所屬 git repo 的根目錄；不在 repo 內則回 None。"""
    if not path.exists():
        return None
    proc = _git(["rev-parse", "--show-toplevel"], cwd=path, check=False)
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip()).resolve()


def validate_project_repo(project_repo: Path, tool_root: Path | None = None) -> Path:
    """確認目標 repo 可以安全地開 worktree。回傳解析後的 repo 根目錄。

    擋下：不存在、不是 git repo、根目錄是家目錄、根目錄是本工具自己、零 commit。
    """
    tool_root = (tool_root or ROOT).resolve()
    home = Path.home().resolve()

    if not project_repo.exists():
        raise WorkspaceError(
            f"目標 repo 不存在: {project_repo}\n"
            f"請在 config.local.yaml 設定 project_repo 指到實際的專案路徑。"
        )

    toplevel = git_toplevel(project_repo)
    if toplevel is None:
        raise WorkspaceError(f"目標路徑不是 git repo: {project_repo}")

    if toplevel == home:
        raise WorkspaceError(
            f"拒絕操作：{project_repo} 的 git 根目錄解析成家目錄 ({home})。\n"
            f"這通常是因為 {home}/.git 意外存在，導致整個家目錄變成一個 repo。\n"
            f"在這種狀態下開 worktree 會試圖複製整個家目錄。\n"
            f"請先讓目標專案成為獨立的 git repo，或處理掉 {home}/.git。"
        )

    if toplevel == tool_root:
        raise WorkspaceError(
            f"拒絕操作：目標 repo 就是本工具自己 ({tool_root})。\n"
            f"請把 project_repo 指到你要讓 agent 動手的專案。"
        )

    if _git(["rev-parse", "--verify", "HEAD"], cwd=toplevel, check=False).returncode != 0:
        raise WorkspaceError(
            f"目標 repo 還沒有任何 commit: {toplevel}\n"
            f"worktree 需要一個起始 commit，請先在該 repo 做第一次 commit。"
        )

    return toplevel


def resolve_start_point(repo: Path, main_branch: str) -> str:
    """決定 worktree 的起始點。

    優先 origin/<main_branch>（先 fetch），因為本地 branch 可能落後很多；
    沒有 remote 就退回本地 branch。這修掉舊 bash 「fetch 了卻用本地 branch」的問題。
    """
    has_remote = bool(_git(["remote"], cwd=repo, check=False).stdout.strip())
    if has_remote:
        _git(["fetch", "origin", main_branch], cwd=repo, check=False)
        remote_ref = f"origin/{main_branch}"
        if _git(["rev-parse", "--verify", remote_ref], cwd=repo, check=False).returncode == 0:
            return remote_ref

    if _git(["rev-parse", "--verify", main_branch], cwd=repo, check=False).returncode == 0:
        return main_branch

    raise WorkspaceError(
        f"找不到起始分支 {main_branch}（也沒有 origin/{main_branch}）於 {repo}"
    )


@dataclass
class Workspace:
    """一個 worktree。

    共用模式下整個 run 只有一個；每節點模式下每個節點各有一個。

    base_sha 一律是「run 的起始 commit」，diff 都相對於它算 —— 這樣下游的審查
    節點看到的是本次 run 累積下來的完整變更，而不是只有上一個節點做的那一小段。
    start_point 才是這個 worktree 實際被建立的位置。
    """

    repo: Path
    path: Path
    branch: str
    base_sha: str
    start_point: str
    node_id: str = ""

    @property
    def workdir(self) -> Path:
        return self.path

    def _mark_untracked(self) -> None:
        """把未追蹤的新檔案標成 intent-to-add。

        沒有這一步，`git diff` 完全看不到新增的檔案 —— agent 最常做的事就是
        新增檔案，而 QA 節點是靠 diff 來審查的。少了這步，QA 會對著一份空 diff
        說「沒問題」。
        """
        _git(["add", "-A", "-N"], cwd=self.path, check=False)

    def diff(self) -> str:
        """相對於 run 起始 commit 的完整 diff（含已 commit 與尚未 commit 的變更）。

        刻意回傳字串而不寫進工作目錄 —— 舊 bash 把 changes.diff commit 進 repo，
        導致下一輪的 diff 包含上一輪的 diff，內容平方成長。
        """
        self._mark_untracked()
        return _git(["diff", self.base_sha], cwd=self.path).stdout

    def changed_files(self) -> list[str]:
        self._mark_untracked()
        out = _git(["diff", "--name-only", self.base_sha], cwd=self.path).stdout
        return [line for line in out.splitlines() if line]

    def commit(self, message: str) -> str | None:
        """暫存所有變更並 commit。沒有變更就回 None，不當成錯誤。

        舊 bash 在這裡直接 `git commit`，沒變更時回非零，配上 `set -e`
        會把整條 pipeline 炸掉 —— 而「agent 認為不需要改」是很常見的情況。
        """
        _git(["add", "-A"], cwd=self.path)
        if _git(["diff", "--cached", "--quiet"], cwd=self.path, check=False).returncode == 0:
            return None
        _git(["commit", "-m", message, "--no-verify"], cwd=self.path)
        return _git(["rev-parse", "HEAD"], cwd=self.path).stdout.strip()

    def head(self) -> str:
        return _git(["rev-parse", "HEAD"], cwd=self.path).stdout.strip()

    def remove(self) -> None:
        """拆掉 worktree。branch 保留，讓使用者還能檢視/合併。"""
        _git(["worktree", "remove", "--force", str(self.path)], cwd=self.repo, check=False)
        if self.path.exists():
            shutil.rmtree(self.path, ignore_errors=True)
        _git(["worktree", "prune"], cwd=self.repo, check=False)


def merge_into(workspace: Workspace, commits: list[str]) -> None:
    """把額外的 commit 合併進 worktree。衝突時 abort 並丟出 MergeConflict。

    合併失敗一定要把工作目錄還原乾淨（--abort），否則接下來的節點會在一個
    半合併狀態的目錄上動手，錯得更難查。
    """
    for commit in commits:
        # 已經是祖先就不用合（fast-forward 或 up-to-date）
        if _git(["merge-base", "--is-ancestor", commit, "HEAD"],
                cwd=workspace.path, check=False).returncode == 0:
            continue

        proc = _git(
            ["merge", "--no-edit", "-m", f"merge {commit[:12]}", commit],
            cwd=workspace.path,
            check=False,
        )
        if proc.returncode == 0:
            continue

        conflicted = [
            line
            for line in _git(
                ["diff", "--name-only", "--diff-filter=U"],
                cwd=workspace.path, check=False,
            ).stdout.splitlines()
            if line
        ]
        _git(["merge", "--abort"], cwd=workspace.path, check=False)
        raise MergeConflict(
            f"合併上游結果時發生衝突（{commit[:12]}）於 {len(conflicted)} 個檔案:\n"
            + "\n".join(f"  - {f}" for f in conflicted)
            + "\n兩個平行節點改到了同一個地方。請調整工作流讓它們不要碰同一批檔案，"
            "或把其中一段改成序列執行。",
            conflicted,
        )


@dataclass(frozen=True)
class RunBase:
    """一個 run 的基準，但沒有 checkout 出來的工作目錄。

    per_node 模式用這個而不是 create_workspace：run 層級的 branch 必須保持
    「沒有被任何 worktree 佔用」，否則跑完之後 `git branch -f` 會被 git 拒絕
    （cannot force update the branch … used by worktree at …）。
    順帶也少一份完整 checkout 的磁碟開銷。
    """

    repo: Path
    branch: str
    base_sha: str
    start_point: str


def prepare_run_base(
    project_repo: Path,
    main_branch: str,
    run_id: str,
    tool_root: Path | None = None,
) -> RunBase:
    """驗證目標 repo、決定起點、建立（未 checkout 的）run branch。"""
    repo = validate_project_repo(project_repo, tool_root=tool_root)
    start_point = resolve_start_point(repo, main_branch)
    base_sha = _git(["rev-parse", start_point], cwd=repo).stdout.strip()

    branch = f"task/{run_id}"
    if _git(["rev-parse", "--verify", branch], cwd=repo, check=False).returncode == 0:
        raise WorkspaceError(f"分支已存在: {branch}")
    _git(["branch", branch, base_sha], cwd=repo)

    return RunBase(repo=repo, branch=branch, base_sha=base_sha, start_point=start_point)


def node_branch(run_id: str, node_id: str) -> str:
    """節點層級的 branch 名稱。

    用 safe_name 而不是原始 id：id 可以是中文、可以含空白、可以只差大小寫，
    都不能直接當 ref 名稱（詳見 engine/graph.py 的 safe_name）。

    也刻意不放在 task/<run-id>/ 底下 —— git 的 ref 存成檔案，
    refs/heads/task/<run-id> 一存在就不可能再有 refs/heads/task/<run-id>/<name>。
    """
    from engine.graph import safe_name

    return f"node/{run_id}/{safe_name(node_id)}"


def create_node_workspace(
    repo: Path,
    worktree_root: Path,
    run_id: str,
    node_id: str,
    base_commits: list[str],
    run_base_sha: str,
    tool_root: Path | None = None,
) -> Workspace:
    """為單一節點建立獨立 worktree，從上游節點的產出 commit 開始。

    base_commits 是這個節點該看到的所有上游狀態（含它自己上一輪的產出，
    迴圈重入時才不會把前一輪的成果丟掉）。第一個當起點，其餘合併進來。

    每次造訪都用 -B 重設 branch 並重建目錄 —— 狀態全在 commit 裡，重建是安全的，
    而且比去推理「這個目錄現在是什麼狀態」可靠得多。
    """
    if not base_commits:
        raise WorkspaceError(f"節點 {node_id} 沒有可用的起始 commit")

    # 不能用 task/<run-id>/<node-id>：git 的 ref 是檔案系統上的檔案，
    # refs/heads/task/<run-id> 存在時就不可能再建 refs/heads/task/<run-id>/<node-id>
    # （會是 "cannot lock ref: … exists; cannot create …"）。所以節點的 branch
    # 換一個獨立的前綴，跟 run 層級的 task/<run-id> 不相干。
    from engine.graph import safe_name

    branch = node_branch(run_id, node_id)

    # 路徑必須確認落在 run 目錄之內才敢刪。safe_name 已經保證不含路徑分隔符，
    # 這裡是第二道防線 —— 底下有 rmtree，不能只靠上游的轉換函式正確。
    run_dir = (worktree_root / run_id).resolve()
    path = (run_dir / safe_name(node_id)).resolve()
    if path == run_dir or run_dir not in path.parents:
        raise WorkspaceError(
            f"節點 id 會讓工作目錄逃出 {run_dir}: {node_id!r} → {path}"
        )
    if _git(["check-ref-format", "--branch", branch], cwd=repo,
            check=False).returncode != 0:
        raise WorkspaceError(f"節點 id 組不出合法的 git branch 名稱: {node_id!r}")

    # 重建：先拆掉舊的（同一節點的前一輪），再從新的起點開
    _git(["worktree", "remove", "--force", str(path)], cwd=repo, check=False)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    _git(["worktree", "prune"], cwd=repo, check=False)

    path.parent.mkdir(parents=True, exist_ok=True)
    _git(["worktree", "add", "-B", branch, str(path), base_commits[0]], cwd=repo)

    workspace = Workspace(
        repo=repo,
        path=path,
        branch=branch,
        base_sha=run_base_sha,
        start_point=base_commits[0],
        node_id=node_id,
    )
    merge_into(workspace, base_commits[1:])
    return workspace


def is_ancestor(repo: Path, maybe_ancestor: str, descendant: str) -> bool:
    return (
        _git(["merge-base", "--is-ancestor", maybe_ancestor, descendant],
             cwd=repo, check=False).returncode == 0
    )


def tip_commits(repo: Path, commits: list[str]) -> list[str]:
    """從一堆 commit 裡挑出「沒有被其他人包含」的那些。

    per_node 模式收尾時要決定 run 的最終狀態。圖可以 fan-out 成兩個各自結束的
    分支，兩邊的 commit 互不包含 —— 只挑「最後完成的那個」會靜默漏掉另一邊。
    先算出所有 tip，再全部合併起來才是完整的結果。
    """
    unique: list[str] = []
    for commit in commits:
        if commit and commit not in unique:
            unique.append(commit)
    return [
        commit
        for commit in unique
        if not any(
            other != commit and is_ancestor(repo, commit, other) for other in unique
        )
    ]


def point_branch_at(repo: Path, branch: str, commit: str) -> None:
    """把 branch 指到某個 commit（不切換工作目錄）。

    用來讓 task/<run-id> 指向終端節點的產出，這樣「變更」分頁與合併指令
    在每節點模式下也照樣可用。
    """
    _git(["branch", "-f", branch, commit], cwd=repo)


def create_workspace(
    project_repo: Path,
    worktree_root: Path,
    main_branch: str,
    run_id: str,
    tool_root: Path | None = None,
) -> Workspace:
    """為一個 run 建立乾淨的 worktree + task branch。"""
    repo = validate_project_repo(project_repo, tool_root=tool_root)
    start_point = resolve_start_point(repo, main_branch)

    branch = f"task/{run_id}"
    if _git(["rev-parse", "--verify", branch], cwd=repo, check=False).returncode == 0:
        raise WorkspaceError(f"分支已存在: {branch}")

    worktree_root.mkdir(parents=True, exist_ok=True)
    path = (worktree_root / run_id).resolve()
    if path.exists():
        raise WorkspaceError(f"worktree 目錄已存在: {path}")

    _git(["worktree", "add", "-b", branch, str(path), start_point], cwd=repo)
    base_sha = _git(["rev-parse", "HEAD"], cwd=path).stdout.strip()

    return Workspace(
        repo=repo,
        path=path,
        branch=branch,
        base_sha=base_sha,
        start_point=start_point,
    )
