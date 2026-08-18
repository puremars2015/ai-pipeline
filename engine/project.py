"""專案資料夾（<專案>/.ai-workflow-proj/）的版面、初始化與設定合併。

為什麼工作流住在目標專案裡，而不是工具的資料庫裡
------------------------------------------------
工作流是「這個專案要怎麼被 agent 處理」的知識，跟 .github/workflows 一樣屬於
專案本身。放在工具的 sqlite 裡的話：換一台機器要重建、同事看不到、改了什麼
沒有紀錄、也沒辦法 review。放進專案就全部解決，代價只是要處理檔案 IO。

哪些進 git、哪些不進
--------------------
進 git：project.yaml、workflows/*.json —— 這些是要跟人共用的定義。
不進 git：local/ 與 local.yaml —— 執行紀錄、產物、sqlite、機器相關的路徑。

.gitignore 本身有進 git，所以每個 worktree 裡也帶著同一條規則。就算 agent
在 worktree 裡誤寫了 local/，也不會變成 diff 的一部分。

worktree 刻意不在這裡
---------------------
per_node 模式的磁碟用量是「節點數 × repo 大小」，而且在 repo 自己的工作目錄
底下開 worktree 會讓 git status、編輯器索引、以及 git 自己的路徑推理都變得很怪。
worktree 一律留在 <worktree_root>/<專案 id>/ 之下。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

import settings as tool_settings
from engine.isolation import MODES, SHARED
from settings import ConfigError, Guards, Settings

DIR_NAME = ".ai-workflow-proj"

# 只擋 local 的東西。project.yaml 與 workflows/ 是刻意要進 git 的。
GITIGNORE = """\
# 由 AI Workflow Builder 建立。
# 執行紀錄、產物與這台機器的設定不進 git；工作流定義（workflows/）要進。
local/
local.yaml
"""

PROJECT_YAML = """\
# AI Workflow Builder — 專案設定
#
# 這個檔案會進 git，是整個 team 共用的。只跟你這台機器有關的覆蓋
# （例如 worktree 要放哪）請寫在同目錄的 local.yaml，那個不進 git。
#
# 沒寫的項目一律沿用工具層的預設（工具目錄的 config.yaml / config.local.yaml）。

# 在專案選單上顯示的名稱
name: {name}

# worktree 的基準分支：每次 run 從 origin/<這個> 開新 branch
main_branch: {main_branch}

# 這個專案新建工作流時的預設隔離模式（工作流自己的 settings.isolation 會再覆蓋）
#   shared   = 整個 run 一個 worktree，會寫檔的節點排隊執行
#   per_node = 每個節點自己的 worktree，可真平行，但 fan-in 時要合併、可能衝突
isolation: {isolation}

# 執行防護的覆蓋。留空就用工具層的預設。
# 可用欄位：max_parallel_nodes / default_max_visits / max_run_steps /
#           default_node_timeout / run_timeout / kill_grace_seconds
guards: {{}}
"""


class ProjectError(Exception):
    """專案資料夾不存在、格式壞掉，或無法建立。"""


@dataclass(frozen=True)
class ProjectPaths:
    """一個專案底下所有由本工具管理的路徑。

    只是把路徑集中起來，不保證這些檔案存在（scaffold 才負責建立）。
    """

    repo: Path

    @property
    def root(self) -> Path:
        return self.repo / DIR_NAME

    @property
    def config_file(self) -> Path:
        return self.root / "project.yaml"

    @property
    def local_config_file(self) -> Path:
        return self.root / "local.yaml"

    @property
    def gitignore(self) -> Path:
        return self.root / ".gitignore"

    @property
    def workflows_dir(self) -> Path:
        return self.root / "workflows"

    @property
    def local_dir(self) -> Path:
        return self.root / "local"

    @property
    def database(self) -> Path:
        return self.local_dir / "ai-workflow.sqlite"

    @property
    def runs_dir(self) -> Path:
        return self.local_dir / "runs"


def paths_for(repo: Path) -> ProjectPaths:
    return ProjectPaths(repo=Path(repo).resolve())


def is_initialised(repo: Path) -> bool:
    return paths_for(repo).config_file.exists()


def scaffold(repo: Path, *, name: str, main_branch: str) -> ProjectPaths:
    """在專案裡建立 .ai-workflow-proj/。已經有的東西一律不覆蓋。

    冪等是硬性要求：註冊一個同事已經設定好的專案時，會走到這裡，
    而那時 project.yaml 裡是他們調過的設定，絕不能被預設值蓋掉。
    """
    paths = paths_for(repo)
    if not paths.repo.is_dir():
        raise ProjectError(f"專案路徑不存在: {paths.repo}")

    for directory in (paths.root, paths.workflows_dir, paths.local_dir):
        directory.mkdir(parents=True, exist_ok=True)

    if not paths.gitignore.exists():
        paths.gitignore.write_text(GITIGNORE, "utf-8")

    if not paths.config_file.exists():
        # json.dumps 產生的雙引號字串剛好也是合法的 YAML scalar，
        # 名稱裡有冒號或引號時不會把 yaml 弄壞。
        paths.config_file.write_text(
            PROJECT_YAML.format(
                name=json.dumps(name, ensure_ascii=False),
                main_branch=json.dumps(main_branch, ensure_ascii=False),
                isolation=json.dumps(SHARED),
            ),
            "utf-8",
        )

    return paths


def read_config(repo: Path) -> dict[str, Any]:
    """讀出 project.yaml + local.yaml 疊合後的原始設定。

    尚未初始化的專案回空 dict —— 呼叫端會退回工具層的預設值，
    而不是整個爆掉。「還沒設定」跟「設定壞掉」是兩件事。
    """
    paths = paths_for(repo)
    data: dict[str, Any] = {}

    for path in (paths.config_file, paths.local_config_file):
        if not path.exists():
            continue
        try:
            loaded = yaml.safe_load(path.read_text("utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ProjectError(f"{path} 不是合法的 YAML: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ProjectError(f"{path} 的內容必須是一組設定，讀到 {type(loaded).__name__}")
        data = tool_settings.deep_merge(data, loaded)

    return data


@dataclass(frozen=True)
class ProjectSettings:
    """一個專案的生效設定：工具層 → project.yaml → local.yaml 疊完的結果。

    引擎只認這個，不再認全域的單一 project_repo。
    """

    id: str
    name: str
    repo: Path
    main_branch: str
    isolation: str
    guards: Guards
    worktree_root: Path
    database: Path
    runs_dir: Path
    workflows_dir: Path
    cleanup_worktree_on_success: bool


def for_project(
    tool: Settings, project_id: str, repo: Path, name: str = ""
) -> ProjectSettings:
    """把工具層設定與專案自己的設定疊起來。

    可被專案覆蓋的只有「跟這個專案怎麼跑有關」的項目。database 與 runs_dir
    刻意不開放覆蓋 —— 它們的位置是這個架構的一部分（一定在 local/ 底下），
    讓它們可設定只會製造出「紀錄跑到別的地方去了」這種難查的狀況。
    """
    repo = Path(repo).resolve()
    paths = paths_for(repo)
    raw = read_config(repo)

    isolation = raw.get("isolation") or SHARED
    if isolation not in MODES:
        raise ProjectError(
            f"{paths.config_file} 的 isolation 只能是 {' / '.join(MODES)}，"
            f"讀到 {isolation!r}"
        )

    try:
        guards = tool_settings.build_guards(raw.get("guards") or {}, base=tool.guards)
    except ConfigError as exc:
        raise ProjectError(f"{paths.config_file}: {exc}") from exc

    # worktree_root 若被專案的 local.yaml 指定，就照它說的用（那本來就是
    # 針對這個專案寫的）；沒指定才在工具層的根目錄底下依專案分層 ——
    # 多專案之後不分層的話，那個目錄會變成一坨看不出屬於誰的 run id。
    raw_worktree = raw.get("worktree_root")
    worktree_root = (
        tool_settings.resolve_path(raw_worktree, base=repo)
        if raw_worktree
        else tool.worktree_root / project_id
    )

    return ProjectSettings(
        id=project_id,
        name=str(raw.get("name") or name or repo.name),
        repo=repo,
        main_branch=str(raw.get("main_branch") or tool.main_branch),
        isolation=isolation,
        guards=guards,
        worktree_root=worktree_root,
        database=paths.database,
        runs_dir=paths.runs_dir,
        workflows_dir=paths.workflows_dir,
        cleanup_worktree_on_success=bool(
            raw.get("cleanup_worktree_on_success", tool.cleanup_worktree_on_success)
        ),
    )
