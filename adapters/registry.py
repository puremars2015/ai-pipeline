"""載入 adapters/*.yaml，並偵測對應的 CLI 是否真的裝了。"""

from __future__ import annotations

import importlib
import os
import shutil
from pathlib import Path
from typing import Callable

import yaml

from adapters.base import AdapterError, AdapterField, AdapterSpec

ADAPTER_DIR = Path(__file__).resolve().parent

# 每個 normalizer 模組要提供 normalize(raw: dict | str) -> list[dict]
Normalizer = Callable[[object], list[dict]]

_SPEC_KEYS = {
    "id",
    "label",
    "binary",
    "binary_candidates",
    "argv",
    "prompt_delivery",
    "cwd",
    "events",
    "normalizer",
    "mutates",
    "kind",
    "fields",
    "resume",
    "env",
    "supports_schema",
    "description",
}


def _parse(data: dict, source: Path) -> AdapterSpec:
    unknown = set(data) - _SPEC_KEYS
    if unknown:
        raise AdapterError(f"{source.name}: 無法識別的欄位 {sorted(unknown)}")

    for required in ("id", "binary"):
        if not data.get(required):
            raise AdapterError(f"{source.name}: 缺少必要欄位 {required}")

    fields = [AdapterField(**f) for f in (data.get("fields") or [])]
    payload = {k: v for k, v in data.items() if k != "fields"}
    payload.setdefault("label", payload["id"])
    return AdapterSpec(fields=fields, **payload)


class Registry:
    def __init__(self, adapter_dir: Path | None = None) -> None:
        self.dir = adapter_dir or ADAPTER_DIR
        self._specs: dict[str, AdapterSpec] = {}
        self._normalizers: dict[str, Normalizer] = {}
        self.load()

    def load(self) -> None:
        self._specs.clear()
        for path in sorted(self.dir.glob("*.yaml")):
            data = yaml.safe_load(path.read_text("utf-8")) or {}
            spec = _parse(data, path)
            if spec.id in self._specs:
                raise AdapterError(f"adapter id 重複: {spec.id}")
            self._specs[spec.id] = spec

    def __contains__(self, adapter_id: str) -> bool:
        return adapter_id in self._specs

    def get(self, adapter_id: str) -> AdapterSpec:
        if adapter_id not in self._specs:
            raise AdapterError(
                f"未知的 adapter: {adapter_id}（可用: {sorted(self._specs)}）"
            )
        return self._specs[adapter_id]

    def all(self) -> list[AdapterSpec]:
        return list(self._specs.values())

    def normalizer(self, spec: AdapterSpec) -> Normalizer | None:
        """取得 spec 對應的事件翻譯函式；沒有就回 None（呼叫端退回純文字）。

        normalizer 模組可以二選一：

        - `normalize(raw)` —— 無狀態，整個行程共用一份（會被快取）
        - `make_normalizer()` —— 回傳一個**每次執行都是新的**可呼叫物件，
          給需要跨事件記狀態的 adapter 用（例如 pi 要把 tool_execution_start
          的參數記下來，等對應的 end 成功了才回報檔案改動）

        有 make_normalizer 就絕不快取 —— 狀態跨節點共用會讓並行執行互相污染。
        """
        if not spec.normalizer:
            return None

        module = importlib.import_module(f"adapters.normalizers.{spec.normalizer}")

        factory = getattr(module, "make_normalizer", None)
        if factory is not None:
            return factory()

        if spec.normalizer in self._normalizers:
            return self._normalizers[spec.normalizer]
        func = getattr(module, "normalize", None)
        if func is None:
            raise AdapterError(
                f"adapters/normalizers/{spec.normalizer}.py 需要 normalize() "
                f"或 make_normalizer()"
            )
        self._normalizers[spec.normalizer] = func
        return func

    def resolve_binary(self, spec: AdapterSpec) -> str | None:
        """找出實際要執行的檔案。PATH 優先，其次是 adapter 宣告的候選絕對路徑。

        候選路徑是為了 pi 這種情況：它裝在 ~/.hermes/node/bin，而那個目錄不在
        使用者的 PATH 上。動使用者的 shell 設定不是我們該做的事。
        """
        found = shutil.which(spec.binary)
        if found:
            return found
        for candidate in spec.binary_candidates:
            path = Path(candidate).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        return None

    def is_installed(self, spec: AdapterSpec) -> bool:
        return self.resolve_binary(spec) is not None

    def availability(self) -> dict[str, bool]:
        return {spec.id: self.is_installed(spec) for spec in self.all()}


_default: Registry | None = None


def default() -> Registry:
    global _default
    if _default is None:
        _default = Registry()
    return _default
