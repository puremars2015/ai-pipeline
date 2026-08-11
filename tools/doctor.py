"""環境自我檢查。

用法：
    .venv/bin/python -m tools.doctor

檢查每個 adapter 的 CLI 是否裝了、是否登入、以及設定是否互相矛盾
（例如 ~/.codex/config.toml 指定的模型比安裝的 codex 版本新）。
這類問題如果不先攤出來，會變成工作流跑到一半才失敗，而且錯誤訊息藏在事件流裡。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import settings
from adapters.registry import default as default_registry
from engine.workspace import WorkspaceError, validate_project_repo

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
        if not registry.is_installed(spec):
            mark = WARN if spec.kind == "mock" else BAD
            print(f" {mark} {spec.label:22} 找不到指令: {spec.binary}")
            if spec.kind != "mock":
                problems.append(f"{spec.id}: {spec.binary} 未安裝")
            continue

        code, out = _run([spec.binary, "--version"])
        version = out.splitlines()[0] if out else "?"
        print(f" {OK} {spec.label:22} {version}")

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


def check_repo() -> list[str]:
    print("\n== 目標 repo ==")
    cfg = settings.load()
    try:
        repo = validate_project_repo(cfg.project_repo)
    except WorkspaceError as exc:
        print(f" {BAD} {exc}")
        return [f"目標 repo 不可用: {cfg.project_repo}"]
    print(f" {OK} {repo}  (基準分支 {cfg.main_branch})")
    return []


def main() -> int:
    problems: list[str] = []
    problems += check_adapters()
    problems += check_codex_model()
    problems += check_repo()

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
