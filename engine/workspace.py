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
    """一個 run 專屬的 worktree。所有節點共用這一份工作目錄。"""

    repo: Path
    path: Path
    branch: str
    base_sha: str
    start_point: str

    @property
    def workdir(self) -> Path:
        return self.path

    def diff(self) -> str:
        """相對於 run 起始 commit 的完整 diff（含尚未 commit 的變更）。

        刻意回傳字串而不寫進工作目錄 —— 舊 bash 把 changes.diff commit 進 repo，
        導致下一輪的 diff 包含上一輪的 diff，內容平方成長。
        """
        return _git(["diff", self.base_sha], cwd=self.path).stdout

    def changed_files(self) -> list[str]:
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
