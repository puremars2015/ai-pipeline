"""環境自我檢查。

用法：
    .venv/bin/python -m tools.doctor

檢查每個 adapter 的 CLI 是否裝了、是否登入、以及設定是否互相矛盾
（例如 ~/.codex/config.toml 指定的模型比安裝的 codex 版本新）。
這類問題如果不先攤出來，會變成工作流跑到一半才失敗，而且錯誤訊息藏在事件流裡。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import settings
from adapters.registry import default as default_registry
from engine import project as proj
from engine.workspace import WorkspaceError, validate_project_repo
from store.projects import ProjectRegistry
from store.workflows import WorkflowStore

OK = "\033[32m✓\033[0m"
WARN = "\033[33m!\033[0m"
BAD = "\033[31m✗\033[0m"


def _run(argv: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except FileNotFoundError:
        return 127, "找不到指令"
    except subprocess.TimeoutExpired:
        return 124, f"超過 {timeout}s 沒回應"


def check_adapters() -> list[str]:
    problems: list[str] = []
    registry = default_registry()

    print("== Adapter / CLI ==")
    for spec in registry.all():
        # 用解析後的路徑，不是 yaml 裡的名字 —— 有些 CLI（例如 pi）裝在不在
        # PATH 上的目錄，靠 adapter 的 binary_candidates 才找得到。
        resolved = registry.resolve_binary(spec)
        if resolved is None:
            mark = WARN if spec.kind == "mock" else BAD
            print(f" {mark} {spec.label:22} 找不到指令: {spec.binary}")
            if spec.kind != "mock":
                problems.append(f"{spec.id}: 找不到 {spec.binary}")
            continue

        code, out = _run([resolved, "--version"])
        if code != 0:
            print(f" {WARN} {spec.label:22} 找到了但問不出版本 (exit {code})")
            print(f"     {resolved}")
            if out:
                print(f"     {out.splitlines()[0][:100]}")
            problems.append(f"{spec.id}: {resolved} 無法執行")
            continue

        version = out.splitlines()[0] if out else "?"
        note = "" if resolved == shutil.which(spec.binary) else f"  ({resolved})"
        print(f" {OK} {spec.label:22} {version}{note}")

    return problems


def check_codex_model() -> list[str]:
    """codex 專屬：config.toml 的模型可能比安裝的 CLI 版本新。

    實際踩到過：config 設 model = "gpt-5.6-luna"，但 codex-cli 0.136.0 送出去
    會被 API 以 400 擋掉（"requires a newer version of Codex"），
    而且 codex 會先卡在 models cache 解析錯誤上，看起來像沒反應。
    """
    problems: list[str] = []
    config = Path.home() / ".codex" / "config.toml"
    if not config.exists():
        return problems

    print("\n== codex 模型設定 ==")
    model = None
    for line in config.read_text("utf-8").splitlines():
        if line.strip().startswith("model ") or line.strip().startswith("model="):
            model = line.split("=", 1)[1].strip().strip('"')
            break

    if not model:
        print(f" {OK} config.toml 沒有寫死模型，用 codex 預設")
        return problems

    code, out = _run(
        ["codex", "exec", "--json", "--color", "never", "-s", "read-only",
         "--skip-git-repo-check", "說 ok"],
        timeout=90,
    )
    if "requires a newer version of Codex" in out:
        print(f" {BAD} config.toml 的 model = {model} 需要更新的 codex CLI")
        print(f"     修法：codex update       （或在節點設定裡填一個舊模型覆寫）")
        problems.append(f"codex 模型 {model} 與已安裝的 CLI 版本不相容")
    elif code != 0:
        print(f" {WARN} model = {model}，但試跑失敗 (exit {code})")
        print(f"     {out.splitlines()[-1][:120] if out else ''}")
        problems.append(f"codex 試跑失敗 (exit {code})")
    else:
        print(f" {OK} model = {model} 可用")

    return problems


def check_projects() -> list[str]:
    """逐一檢查已註冊的專案。

    一個專案壞掉（資料夾被搬走、project.yaml 打錯字）不該讓其他專案的
    檢查跟著中斷 —— 這支工具的用處就是一次把所有問題攤出來。
    """
    print("\n== 已註冊專案 ==")
    cfg = settings.load()
    entries = ProjectRegistry(cfg.database).list()
    if not entries:
        print(f" {WARN} 還沒有註冊任何專案。在網頁上新增，或 POST /api/projects。")
        return []

    problems: list[str] = []
    for entry in entries:
        try:
            repo = validate_project_repo(entry.path)
            resolved = proj.for_project(cfg, entry.id, entry.path, entry.name)
        except (WorkspaceError, proj.ProjectError) as exc:
            print(f" {BAD} {entry.id:20} {exc}")
            problems.append(f"專案 {entry.id} 不可用: {exc}")
            continue

        mark = OK if proj.is_initialised(repo) else WARN
        print(f" {mark} {entry.id:20} {repo}  (基準分支 {resolved.main_branch})")
        if not proj.is_initialised(repo):
            print(f"     還沒有 {proj.DIR_NAME}/，重新註冊一次就會建好")
            problems.append(f"專案 {entry.id} 缺少 {proj.DIR_NAME}/")
            continue

        count = len(WorkflowStore(resolved.workflows_dir).list())
        runs = resolved.database.exists()
        print(f"     {count} 個工作流 @ {resolved.workflows_dir}")
        print(f"     執行紀錄 {'有' if runs else '還沒有'} @ {resolved.database}")

    return problems


def main() -> int:
    problems: list[str] = []
    problems += check_adapters()
    problems += check_codex_model()
    problems += check_projects()

    print()
    if problems:
        print(f"{BAD} {len(problems)} 個問題需要處理：")
        for p in problems:
            print(f"   - {p}")
        return 1
    print(f"{OK} 環境檢查全部通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
