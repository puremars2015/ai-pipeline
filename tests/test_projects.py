"""專案模型：資料夾版面、註冊表、設定合併。

這一層是「工具服務多個專案」的地基。重點在三件事：
初始化必須冪等（同事已經設定好的專案不能被預設值蓋掉）、
專案 id 必須對網址與檔案系統都安全、設定的疊合順序必須可預測。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

import settings as tool_settings
from engine import project as proj
from engine.workspace import detect_main_branch
from store.projects import ProjectRegistry, make_id, slugify
from tests.test_workspace import make_repo

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def tool(tmp_path):
    """一份最小的工具層設定。"""
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "main_branch": "main",
                "worktree_root": str(tmp_path / "wt"),
                "runs_dir": str(tmp_path / "runs"),
                "database": str(tmp_path / "central.sqlite"),
                "guards": {"max_run_steps": 60},
            }
        ),
        "utf-8",
    )
    return tool_settings.load(config)


# ------------------------------------------------------------ 資料夾版面


def test_scaffold_creates_expected_layout(tmp_path):
    repo = make_repo(tmp_path / "proj")
    paths = proj.scaffold(repo, name="示範專案", main_branch="main")

    assert paths.config_file.exists()
    assert paths.workflows_dir.is_dir()
    assert paths.local_dir.is_dir()
    assert paths.gitignore.exists()
    assert proj.is_initialised(repo)


def test_gitignore_excludes_local_but_not_definitions(tmp_path):
    """工作流要進 git，執行紀錄不能進 —— 這是整個設計的分界線。

    直接問 git，不比對 .gitignore 的文字：真正要保證的是「git 會怎麼判斷」，
    而規則的寫法之後可能會變。
    """
    repo = make_repo(tmp_path / "proj")
    paths = proj.scaffold(repo, name="p", main_branch="main")
    (paths.workflows_dir / "demo.json").write_text("{}", "utf-8")
    (paths.runs_dir / "run-1").mkdir(parents=True)
    (paths.runs_dir / "run-1" / "plan.md").write_text("x", "utf-8")
    paths.local_config_file.write_text("{}", "utf-8")

    def ignored(path: Path) -> bool:
        return subprocess.run(
            ["git", "check-ignore", "-q", str(path)], cwd=str(repo)
        ).returncode == 0

    assert not ignored(paths.config_file)
    assert not ignored(paths.workflows_dir / "demo.json")
    assert ignored(paths.runs_dir / "run-1" / "plan.md")
    assert ignored(paths.database)
    assert ignored(paths.local_config_file)


def test_scaffold_is_idempotent_and_never_clobbers(tmp_path):
    """註冊同事已經設定好的專案時會再跑一次 scaffold，那時不能蓋掉他們的設定。"""
    repo = make_repo(tmp_path / "proj")
    paths = proj.scaffold(repo, name="原本的名字", main_branch="develop")
    paths.config_file.write_text(
        yaml.safe_dump({"name": "同事調過的", "main_branch": "release"}), "utf-8"
    )

    proj.scaffold(repo, name="新名字", main_branch="main")

    kept = yaml.safe_load(paths.config_file.read_text("utf-8"))
    assert kept["name"] == "同事調過的"
    assert kept["main_branch"] == "release"


def test_name_with_yaml_special_chars_survives(tmp_path):
    """名稱裡有冒號、引號的話，寫壞 yaml 會讓專案整個讀不出來。"""
    repo = make_repo(tmp_path / "proj")
    paths = proj.scaffold(repo, name='後端: "核心" 服務', main_branch="main")

    assert yaml.safe_load(paths.config_file.read_text("utf-8"))["name"] == '後端: "核心" 服務'


# ------------------------------------------------------------ 設定合併


def test_uninitialised_project_falls_back_to_tool_defaults(tool, tmp_path):
    """還沒初始化不是錯誤，只是「沒有覆蓋」。"""
    repo = make_repo(tmp_path / "proj")
    resolved = proj.for_project(tool, "proj", repo)

    assert resolved.main_branch == "main"
    assert resolved.guards.max_run_steps == 60
    assert resolved.isolation == "shared"


def test_project_yaml_overrides_tool_settings(tool, tmp_path):
    repo = make_repo(tmp_path / "proj")
    paths = proj.scaffold(repo, name="p", main_branch="main")
    paths.config_file.write_text(
        yaml.safe_dump(
            {"name": "後端", "main_branch": "develop", "isolation": "per_node"}
        ),
        "utf-8",
    )

    resolved = proj.for_project(tool, "proj", repo)
    assert resolved.name == "後端"
    assert resolved.main_branch == "develop"
    assert resolved.isolation == "per_node"


def test_local_yaml_wins_over_project_yaml(tool, tmp_path):
    """local.yaml 是「這台機器」的最後一層覆蓋，必須贏過進 git 的那一份。"""
    repo = make_repo(tmp_path / "proj")
    paths = proj.scaffold(repo, name="p", main_branch="main")
    paths.config_file.write_text(yaml.safe_dump({"main_branch": "develop"}), "utf-8")
    paths.local_config_file.write_text(yaml.safe_dump({"main_branch": "我的分支"}), "utf-8")

    assert proj.for_project(tool, "proj", repo).main_branch == "我的分支"


def test_guards_merge_instead_of_replace(tool, tmp_path):
    """專案只覆蓋一個 guard，其餘必須保留工具層的值，而不是回到 dataclass 預設。"""
    repo = make_repo(tmp_path / "proj")
    paths = proj.scaffold(repo, name="p", main_branch="main")
    paths.config_file.write_text(yaml.safe_dump({"guards": {"run_timeout": 99}}), "utf-8")

    guards = proj.for_project(tool, "proj", repo).guards
    assert guards.run_timeout == 99
    assert guards.max_run_steps == 60


def test_unknown_guard_key_is_rejected(tool, tmp_path):
    """guards 全是安全上限。打錯字卻沒人講的話，使用者會以為自己調高了上限。"""
    repo = make_repo(tmp_path / "proj")
    paths = proj.scaffold(repo, name="p", main_branch="main")
    paths.config_file.write_text(
        yaml.safe_dump({"guards": {"max_run_stepz": 5}}), "utf-8"
    )

    with pytest.raises(proj.ProjectError, match="無法識別"):
        proj.for_project(tool, "proj", repo)


def test_bad_isolation_is_rejected(tool, tmp_path):
    repo = make_repo(tmp_path / "proj")
    paths = proj.scaffold(repo, name="p", main_branch="main")
    paths.config_file.write_text(yaml.safe_dump({"isolation": "隨便"}), "utf-8")

    with pytest.raises(proj.ProjectError, match="isolation"):
        proj.for_project(tool, "proj", repo)


def test_broken_yaml_is_reported_with_path(tool, tmp_path):
    repo = make_repo(tmp_path / "proj")
    paths = proj.scaffold(repo, name="p", main_branch="main")
    paths.config_file.write_text("name: [未關閉\n", "utf-8")

    with pytest.raises(proj.ProjectError, match="project.yaml"):
        proj.for_project(tool, "proj", repo)


def test_worktree_root_is_namespaced_per_project(tool, tmp_path):
    """多專案之後不分層的話，worktree 目錄會變成一坨看不出屬於誰的 run id。"""
    repo = make_repo(tmp_path / "proj")
    resolved = proj.for_project(tool, "backend", repo)
    assert resolved.worktree_root == tool.worktree_root / "backend"


def test_local_yaml_can_relocate_worktree_root(tool, tmp_path):
    repo = make_repo(tmp_path / "proj")
    paths = proj.scaffold(repo, name="p", main_branch="main")
    paths.local_config_file.write_text(
        yaml.safe_dump({"worktree_root": str(tmp_path / "elsewhere")}), "utf-8"
    )

    assert proj.for_project(tool, "p", repo).worktree_root == tmp_path / "elsewhere"


def test_records_live_under_local_dir(tool, tmp_path):
    """紀錄與產物的位置是架構的一部分，不開放覆蓋。"""
    repo = make_repo(tmp_path / "proj")
    resolved = proj.for_project(tool, "p", repo)

    assert resolved.database == repo / proj.DIR_NAME / "local" / "ai-workflow.sqlite"
    assert resolved.runs_dir == repo / proj.DIR_NAME / "local" / "runs"


# ------------------------------------------------------------ 專案 id


def test_slug_is_url_and_filesystem_safe():
    """id 會出現在網址與 worktree 路徑裡。"""
    assert slugify("My Project!") == "my-project"
    assert slugify("後端服務") == "project"
    assert "/" not in slugify("a/b")


def test_id_collision_falls_back_to_path_hash():
    a = make_id(Path("/tmp/one/api"), taken=set())
    b = make_id(Path("/tmp/two/api"), taken={a})

    assert a == "api"
    assert b.startswith("api-") and b != a


def test_same_path_always_gets_same_id():
    """重新註冊同一個專案要拿回同一個 id，不然 worktree 目錄與網址會漂移。"""
    taken = {"api"}
    assert make_id(Path("/tmp/x/api"), taken) == make_id(Path("/tmp/x/api"), taken)


# ------------------------------------------------------------ 註冊表


@pytest.fixture
def reg(tmp_path):
    return ProjectRegistry(tmp_path / "central.sqlite")


def test_add_and_get(reg, tmp_path):
    repo = make_repo(tmp_path / "proj")
    entry = reg.add(repo, name="示範")

    assert reg.get(entry.id).path == repo.resolve()
    assert reg.get_by_path(repo).name == "示範"


def test_registering_same_path_twice_returns_existing(reg, tmp_path):
    """同一個專案被註冊兩次會變成兩份互相看不見的歷史。"""
    repo = make_repo(tmp_path / "proj")
    first = reg.add(repo, name="a")
    second = reg.add(repo, name="b")

    assert first.id == second.id
    assert len(reg.list()) == 1


def test_two_projects_with_same_folder_name_coexist(reg, tmp_path):
    one = make_repo(tmp_path / "team-a" / "api")
    two = make_repo(tmp_path / "team-b" / "api")

    ids = {reg.add(one).id, reg.add(two).id}
    assert len(ids) == 2


def test_list_puts_recently_used_first(reg, tmp_path):
    older = reg.add(make_repo(tmp_path / "older"))
    newer = reg.add(make_repo(tmp_path / "newer"))
    reg.touch(older.id)

    assert [p.id for p in reg.list()][0] == older.id
    assert newer.id in {p.id for p in reg.list()}


def test_remove_only_forgets(reg, tmp_path):
    """移除專案不能動到使用者的檔案 —— 那裡有已經 commit 進 git 的工作流。"""
    repo = make_repo(tmp_path / "proj")
    proj.scaffold(repo, name="p", main_branch="main")
    entry = reg.add(repo)

    assert reg.remove(entry.id) is True
    assert reg.get(entry.id) is None
    assert (repo / proj.DIR_NAME / "project.yaml").exists()
    assert reg.remove(entry.id) is False


# ------------------------------------------------------------ 主線分支偵測


def test_detects_current_branch(tmp_path):
    repo = make_repo(tmp_path / "proj")
    assert detect_main_branch(repo, fallback="fallback") == "main"


def test_falls_back_when_repo_has_no_branch(tmp_path):
    """沒有 commit 的 repo 問不出分支，要退回預設而不是回空字串。"""
    repo = make_repo(tmp_path / "empty", commit=False)
    assert detect_main_branch(repo, fallback="trunk") in {"main", "trunk"}
