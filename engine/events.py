"""正規化事件模型。

不同 CLI 的原始事件長得完全不一樣（實測結果，非推測）：

  codex     {"type":"item.completed","item":{"type":"agent_message","text":...}}
  claude    {"type":"assistant","message":{"content":[{"type":"tool_use",...}]}}
  opencode  {"type":"tool_use","part":{"tool":"write","state":{...}}}

引擎與 UI 只認識這裡定義的一組 kind，各 adapter 的 normalizer 負責翻譯。
新增一個 CLI 只要寫一支 normalizer，引擎與前端都不用改。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable

# ---- 事件種類 -------------------------------------------------------------
STDOUT = "stdout"  # 無法解析的原始輸出行（診斷訊息、純文字模式）
REASONING = "reasoning"  # 思考過程摘要
MESSAGE = "message"  # agent 對人說的話
TOOL_CALL = "tool_call"  # 呼叫工具（讀檔、跑指令…）
TOOL_RESULT = "tool_result"  # 工具回傳
FILE_EDIT = "file_edit"  # 改了檔案
USAGE = "usage"  # token / 費用
STATUS = "status"  # 生命週期（session 建立、turn 開始…）
ERROR = "error"  # 失敗

ALL_KINDS = frozenset(
    {STDOUT, REASONING, MESSAGE, TOOL_CALL, TOOL_RESULT, FILE_EDIT, USAGE, STATUS, ERROR}
)


@dataclass
class NodeEvent:
    """單一節點執行過程中的一則事件。"""

    kind: str
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    node_id: str = ""
    run_id: str = ""
    seq: int = 0
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if self.kind not in ALL_KINDS:
            raise ValueError(f"未知的事件 kind: {self.kind}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "node_id": self.node_id,
            "seq": self.seq,
            "ts": self.ts,
            "kind": self.kind,
            "text": self.text,
            "data": self.data,
        }


def ev(kind: str, text: str = "", **data: Any) -> dict[str, Any]:
    """normalizer 用的簡便建構式。回傳 dict，由 runner 補上 run_id/seq/ts。"""
    return {"kind": kind, "text": text, "data": data}


def passthrough(line: str) -> list[dict[str, Any]]:
    """沒有 normalizer 時的預設行為：整行當 stdout。"""
    return [ev(STDOUT, line)]


def collect_text(events: Iterable[dict[str, Any]]) -> str:
    """把一串事件裡的 message 文字接起來，當作節點的 last_message。"""
    return "\n".join(e["text"] for e in events if e["kind"] == MESSAGE and e["text"])
