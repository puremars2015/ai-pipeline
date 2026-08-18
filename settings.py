"""設定載入：config.yaml 為範本，config.local.yaml 疊在上面覆寫。"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent


class ConfigError(Exception):
    """設定本身有問題，啟動時就該擋下來。"""


@dataclass(frozen=True)
class Guards:
    max_parallel_nodes: int = 4
    default_max_visits: int = 3
    max_run_steps: int = 60
    default_node_timeout: int = 1800
    run_timeout: int = 7200
    kill_grace_seconds: int = 10


@dataclass(frozen=True)
class Settings:
    project_repo: Path
    main_branch: str
    worktree_root: Path
    runs_dir: Path
    database: Path
    guards: Guards
    cleanup_worktree_on_success: bool


def deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def resolve_path(raw: str, base: Path | None = None) -> Path:
    """展開 ~ 與環境變數，相對路徑以 base（預設為本工具根目錄）為基準。

    專案層的設定要以「該專案的資料夾」為基準，不是本工具的根目錄 ——
    所以 base 是參數，不是寫死的 ROOT。
    """
    expanded = os.path.expandvars(os.path.expanduser(str(raw)))
    path = Path(expanded)
    if not path.is_absolute():
        path = (base or ROOT) / path
    return path.resolve()


def build_guards(raw: dict[str, Any], base: Guards | None = None) -> Guards:
    """把一層 guards 覆蓋疊到既有的 Guards 上。

    無法識別的欄位一律報錯而不是靜默忽略 —— guards 全是安全上限，
    打錯字卻沒人講的話，使用者會以為自己調高了上限，實際上沒有。
    """
    unknown = set(raw) - set(Guards.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"guards 有無法識別的欄位: {sorted(unknown)}")
    merged = {**(dataclasses.asdict(base) if base else {}), **raw}
    return Guards(**merged)


def load(config_path: Path | None = None) -> Settings:
    base_path = config_path or (ROOT / "config.yaml")
    if not base_path.exists():
        raise ConfigError(f"找不到設定檔: {base_path}")

    data: dict[str, Any] = yaml.safe_load(base_path.read_text("utf-8")) or {}

    local_path = base_path.with_name("config.local.yaml")
    if local_path.exists():
        local = yaml.safe_load(local_path.read_text("utf-8")) or {}
        data = deep_merge(data, local)

    return Settings(
        project_repo=resolve_path(data.get("project_repo", "")),
        main_branch=data.get("main_branch", "main"),
        worktree_root=resolve_path(data.get("worktree_root", "~/ai-pipeline-worktrees")),
        runs_dir=resolve_path(data.get("runs_dir", "./runs")),
        database=resolve_path(data.get("database", "./ai-pipeline.sqlite")),
        guards=build_guards(data.get("guards") or {}),
        cleanup_worktree_on_success=bool(data.get("cleanup_worktree_on_success", False)),
    )
