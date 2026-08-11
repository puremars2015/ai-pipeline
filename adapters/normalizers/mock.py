"""mock agent 的事件翻譯 —— 它本來就吐正規化形狀，這裡只負責驗證與補值。

刻意不做成「引擎特例」：mock 走的是和真實 CLI 完全相同的
subprocess → 逐行讀 → normalize 路徑，所以引擎測試涵蓋的是真正的執行路徑。
"""

from __future__ import annotations

from typing import Any

from engine.events import ALL_KINDS, STDOUT, ev


def normalize(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        return [ev(STDOUT, raw)]
    if not isinstance(raw, dict):
        return []

    kind = raw.get("kind")
    if kind not in ALL_KINDS:
        return [ev(STDOUT, f"[mock:未知 kind {kind!r}]")]

    return [
        {
            "kind": kind,
            "text": str(raw.get("text") or ""),
            "data": dict(raw.get("data") or {}),
        }
    ]
