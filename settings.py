"""設定載入：config.yaml 為範本，config.local.yaml 疊在上面覆寫。"""

from __future__ import annotations

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


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _resolve(raw: str) -> Path:
    """展開 ~ 與環境變數，相對路徑以專案根目錄為基準。"""
    expanded = os.path.expandvars(os.path.expanduser(str(raw)))
    path = Path(expanded)
    if not path.is_absolute():
        path = ROOT / path
    return path.resolve()


def load(config_path: Path | None = None) -> Settings:
    base_path = config_path or (ROOT / "config.yaml")
    if not base_path.exists():
        raise ConfigError(f"找不到設定檔: {base_path}")

    data: dict[str, Any] = yaml.safe_load(base_path.read_text("utf-8")) or {}

    local_path = base_path.with_name("config.local.yaml")
    if local_path.exists():
        local = yaml.safe_load(local_path.read_text("utf-8")) or {}
        data = _deep_merge(data, local)

    guard_fields = {f for f in Guards.__dataclass_fields__}
    raw_guards = data.get("guards") or {}
    unknown = set(raw_guards) - guard_fields
    if unknown:
        raise ConfigError(f"guards 有無法識別的欄位: {sorted(unknown)}")

    return Settings(
        project_repo=_resolve(data.get("project_repo", "")),
        main_branch=data.get("main_branch", "main"),
        worktree_root=_resolve(data.get("worktree_root", "~/ai-pipeline-worktrees")),
        runs_dir=_resolve(data.get("runs_dir", "./runs")),
        database=_resolve(data.get("database", "./ai-pipeline.sqlite")),
        guards=Guards(**raw_guards),
        cleanup_worktree_on_success=bool(data.get("cleanup_worktree_on_success", False)),
    )
