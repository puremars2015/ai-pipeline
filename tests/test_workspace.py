"""worktree 生命週期與安全防線的測試。

重點在防線：這台機器的家目錄意外是個 git repo，所以「拒絕在 $HOME 上開 worktree」
不是理論上的邊界情況，是實際會踩到的。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from engine.workspace import (
    WorkspaceError,
    create_workspace,
    resolve_start_point,
    validate_project_repo,
)


def _run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True)


def make_repo(path: Path, commit: bool = True) -> Path:
    """建一個最小的 git repo，附一個 commit。"""
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-b", "main"], path)
    _run(["git", "config", "user.email", "test@example.com"], path)
    _run(["git", "config", "user.name", "Test"], path)
    if commit:
        (path / "README.md").write_text("hello\n")
        _run(["git", "add", "-A"], path)
        _run(["git", "commit", "-m", "init"], path)
    return path


# ---------------------------------------------------------------- 防線


def test_rejects_home_directory(tmp_path, monkeypatch):
    """目標 repo 的 git 根目錄若解析成家目錄，必須拒絕。"""
    fake_home = make_repo(tmp_path / "fakehome")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    with pytest.raises(WorkspaceError, match="家目錄"):
        validate_project_repo(fake_home)


def test_rejects_subdir_of_home_repo(tmp_path, monkeypatch):
    """指到家目錄底下的子資料夾也要擋 —— 這正是 ai-pipeline 原本的處境。

    子資料夾本身不是 repo，但 rev-parse --show-toplevel 會往上找到家目錄。
    """
    fake_home = make_repo(tmp_path / "fakehome")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    subdir = fake_home / "Documents" / "some-project"
    subdir.mkdir(parents=True)

    with pytest.raises(WorkspaceError, match="家目錄"):
        validate_project_repo(subdir)


def test_rejects_tool_root(tmp_path):
    """拒絕把本工具自己的 repo 當成目標。"""
    tool = make_repo(tmp_path / "ai-pipeline")
    with pytest.raises(WorkspaceError, match="本工具自己"):
        validate_project_repo(tool, tool_root=tool)


def test_rejects_non_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorkspaceError, match="不是 git repo"):
        validate_project_repo(plain)


def test_rejects_missing_path(tmp_path):
    with pytest.raises(WorkspaceError, match="不存在"):
        validate_project_repo(tmp_path / "nope")


def test_rejects_repo_without_commits(tmp_path):
    """零 commit 的 repo 開不了 worktree —— 家目錄那個 repo 正是這種狀態。"""
    empty = make_repo(tmp_path / "empty", commit=False)
    with pytest.raises(WorkspaceError, match="還沒有任何 commit"):
        validate_project_repo(empty)


def test_accepts_normal_repo(tmp_path):
    repo = make_repo(tmp_path / "good")
    assert validate_project_repo(repo, tool_root=tmp_path / "elsewhere") == repo


# ---------------------------------------------------------------- 起始點


def test_start_point_falls_back_to_local_when_no_remote(tmp_path):
    repo = make_repo(tmp_path / "local")
    assert resolve_start_point(repo, "main") == "main"


def test_start_point_prefers_remote(tmp_path):
    """有 remote 時要用 origin/main，不能用可能落後的本地 branch。"""
    upstream = make_repo(tmp_path / "upstream")
    clone = tmp_path / "clone"
    _run(["git", "clone", str(upstream), str(clone)], tmp_path)
    _run(["git", "config", "user.email", "test@example.com"], clone)
    _run(["git", "config", "user.name", "Test"], clone)

    assert resolve_start_point(clone, "main") == "origin/main"


def test_start_point_missing_branch(tmp_path):
    repo = make_repo(tmp_path / "repo")
    with pytest.raises(WorkspaceError, match="找不到起始分支"):
        resolve_start_point(repo, "nonexistent")


# ---------------------------------------------------------------- worktree


def test_create_workspace(tmp_path):
    repo = make_repo(tmp_path / "proj")
    ws = create_workspace(
        project_repo=repo,
        worktree_root=tmp_path / "wt",
        main_branch="main",
        run_id="r1",
        tool_root=tmp_path / "tool",
    )
    assert ws.path.exists()
    assert (ws.path / "README.md").exists()
    assert ws.branch == "task/r1"
    # 主 repo 的工作目錄沒被動到
    assert repo.joinpath("README.md").read_text() == "hello\n"
    ws.remove()
    assert not ws.path.exists()


def test_create_workspace_rejects_duplicate_branch(tmp_path):
    repo = make_repo(tmp_path / "proj")
    kwargs = dict(
        project_repo=repo,
        worktree_root=tmp_path / "wt",
        main_branch="main",
        tool_root=tmp_path / "tool",
    )
    ws = create_workspace(run_id="dup", **kwargs)
    try:
        with pytest.raises(WorkspaceError, match="分支已存在"):
            create_workspace(run_id="dup", **kwargs)
    finally:
        ws.remove()


def test_commit_returns_none_when_no_changes(tmp_path):
    """這是舊 bash 的 1 號 bug：沒變更時 git commit 回非零，set -e 炸掉整條流程。"""
    repo = make_repo(tmp_path / "proj")
    ws = create_workspace(
        project_repo=repo,
        worktree_root=tmp_path / "wt",
        main_branch="main",
        run_id="r2",
        tool_root=tmp_path / "tool",
    )
    try:
        assert ws.commit("nothing to see") is None

        (ws.path / "new.txt").write_text("data\n")
        sha = ws.commit("real change")
        assert sha is not None
        assert ws.changed_files() == ["new.txt"]
        assert "new.txt" in ws.diff()
    finally:
        ws.remove()


def test_diff_is_not_written_into_worktree(tmp_path):
    """舊 bash 的 2 號 bug：changes.diff 被 commit 進 repo 造成 diff 遞迴膨脹。"""
    repo = make_repo(tmp_path / "proj")
    ws = create_workspace(
        project_repo=repo,
        worktree_root=tmp_path / "wt",
        main_branch="main",
        run_id="r3",
        tool_root=tmp_path / "tool",
    )
    try:
        (ws.path / "a.txt").write_text("x\n")
        ws.commit("round 1")
        first = ws.diff()

        # 再拿一次 diff，內容不該包含上一次的 diff
        second = ws.diff()
        assert first == second
        assert not (ws.path / "changes.diff").exists()
        assert "diff --git" not in second.replace("diff --git a/a.txt b/a.txt", "", 1)
    finally:
        ws.remove()
