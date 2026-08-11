"""Adapter 契約：如何呼叫一個 agent CLI。

一個 adapter = adapters/<id>.yaml（怎麼呼叫，同時驅動 UI 的節點設定表單）
             + adapters/normalizers/<id>.py（怎麼解讀它的事件輸出，選配）

要接一個新的 CLI（例如 pi code）不需要改引擎，放這兩個檔案就好。

argv 規則
---------
argv 清單的每一項可以是：

  "exec"                                    裸字串 —— 一定保留
  {flag: ["-m", "{{ model }}"]}             群組 —— 若群組內任一 placeholder
                                            渲染成空字串，整個群組丟棄

這條規則讓「可選參數」變得宣告式：使用者沒填 model，`-m` 就整個不出現，
而不是傳一個空字串進去讓 CLI 報錯。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][\w.]*)\s*\}\}")


class AdapterError(Exception):
    """adapter 定義有問題，或參數組不出來。"""


def _lookup(variables: dict[str, Any], dotted: str) -> Any:
    cur: Any = variables
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            cur = getattr(cur, part, None)
        if cur is None:
            return None
    return cur


def render(template: str, variables: dict[str, Any]) -> tuple[str, bool]:
    """把 {{ var }} 換成值。

    回傳 (結果, 是否有 placeholder 渲染成空)。第二個值讓呼叫端能決定要不要
    丟棄整個 flag 群組。
    """
    saw_empty = False

    def sub(match: re.Match[str]) -> str:
        nonlocal saw_empty
        value = _lookup(variables, match.group(1))
        if value is None or value == "" or value is False:
            saw_empty = True
            return ""
        if value is True:
            return ""
        return str(value)

    return _PLACEHOLDER.sub(sub, template), saw_empty


@dataclass(frozen=True)
class AdapterField:
    """一個節點設定欄位；UI 依此自動生成表單。"""

    name: str
    type: str = "text"  # text | textarea | select | number | bool
    label: str = ""
    default: Any = ""
    options: list[str] = field(default_factory=list)
    help: str = ""

    def __post_init__(self) -> None:
        allowed = {"text", "textarea", "select", "number", "bool"}
        if self.type not in allowed:
            raise AdapterError(f"欄位 {self.name} 的 type 不合法: {self.type}")


@dataclass(frozen=True)
class AdapterSpec:
    id: str
    label: str
    binary: str
    argv: list[Any] = field(default_factory=list)
    # prompt 怎麼交給 CLI：argv（當成參數，用 {{ prompt }} 佔位）或 stdin
    prompt_delivery: str = "argv"
    # 工作目錄模板。claude 沒有 --cd 這種 flag，只認 cwd，所以需要這個欄位。
    cwd: str | None = None
    events: str = "text"  # jsonl | text
    normalizer: str | None = None
    # 是否會寫檔。會寫檔的節點受 worktree 寫入互斥鎖管制，不會真的平行跑。
    mutates: bool = True
    kind: str = "agent"  # agent | shell | mock
    fields: list[AdapterField] = field(default_factory=list)
    resume: dict[str, Any] | None = None
    env: dict[str, str] = field(default_factory=dict)
    # 支援 --output-schema / --json-schema 之類的結構化輸出
    supports_schema: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        if self.prompt_delivery not in {"argv", "stdin"}:
            raise AdapterError(
                f"adapter {self.id}: prompt_delivery 只能是 argv 或 stdin"
            )
        if self.events not in {"jsonl", "text"}:
            raise AdapterError(f"adapter {self.id}: events 只能是 jsonl 或 text")

    def defaults(self) -> dict[str, Any]:
        return {f.name: f.default for f in self.fields}

    def build_argv(self, variables: dict[str, Any]) -> list[str]:
        """依 argv 規格與變數組出實際的指令列。"""
        out: list[str] = [self.binary]

        for entry in self.argv:
            if isinstance(entry, str):
                rendered, _ = render(entry, variables)
                out.append(rendered)
                continue

            if isinstance(entry, dict) and "flag" in entry:
                pieces = entry["flag"]
                if not isinstance(pieces, list):
                    raise AdapterError(f"adapter {self.id}: flag 必須是 list")
                rendered_pieces: list[str] = []
                drop = False
                for piece in pieces:
                    text, saw_empty = render(str(piece), variables)
                    if saw_empty:
                        drop = True
                        break
                    rendered_pieces.append(text)
                if not drop:
                    out.extend(rendered_pieces)
                continue

            raise AdapterError(
                f"adapter {self.id}: argv 項目必須是字串或 {{flag: [...]}}，"
                f"收到 {entry!r}"
            )

        return out

    def build_cwd(self, variables: dict[str, Any]) -> str | None:
        if not self.cwd:
            return None
        rendered, saw_empty = render(self.cwd, variables)
        if saw_empty or not rendered:
            raise AdapterError(f"adapter {self.id}: cwd 模板渲染成空 ({self.cwd})")
        return rendered
