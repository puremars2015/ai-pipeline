"""Run context：節點之間傳遞的資料，以及 prompt 模板 / 條件運算式的求值。

Prompt 用 Jinja2（Flask 已內建，不多一個依賴）：

    根據以下計畫實作，這是第 {{ loop.iteration }} 輪。
    {{ nodes.plan.last_message }}
    {% if nodes.qa.structured %}上一輪的問題：{{ nodes.qa.structured.issues }}{% endif %}

條件運算式用 simpleeval（白名單求值，不是 eval）：

    nodes.qa.structured.verdict == 'PASS'
    nodes.tests.exit_code == 0 and loop.iteration < 3
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jinja2.sandbox import SandboxedEnvironment
from simpleeval import EvalWithCompoundTypes, InvalidExpression


class ContextError(Exception):
    """模板或運算式的問題。"""


class Dot(dict):
    """支援 `.` 存取的 dict，缺鍵回傳空的 Dot。

    這樣 `nodes.qa.structured.verdict` 在 qa 還沒跑過時不會爆掉，而是求值成
    一個 falsy 的空容器 —— 迴圈第一輪的條件判斷需要這個行為。
    """

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        value = self.get(name)
        if value is None and name not in self:
            return Dot()
        return wrap(value)

    def __bool__(self) -> bool:
        return len(self) > 0

    def __str__(self) -> str:
        # 空的 Dot 代表「這個值不存在」，在模板裡應該渲染成什麼都沒有，
        # 而不是字面上的 "{}"。
        return "" if not self else super().__repr__()


def wrap(value: Any) -> Any:
    """遞迴把 dict 換成 Dot，讓點存取能一路走下去。"""
    if isinstance(value, Dot):
        return value
    if isinstance(value, dict):
        return Dot({k: wrap(v) for k, v in value.items()})
    if isinstance(value, list):
        return [wrap(v) for v in value]
    return value


@dataclass
class NodeOutput:
    """一個節點跑完之後留給下游的東西。"""

    last_message: str = ""
    structured: Any = None
    session_id: str = ""
    exit_code: int | None = None
    files: list[str] = field(default_factory=list)
    stdout: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    visits: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "last_message": self.last_message,
            "structured": self.structured,
            "session_id": self.session_id,
            "exit_code": self.exit_code,
            "files": self.files,
            "stdout": self.stdout,
            "usage": self.usage,
            "status": self.status,
            "visits": self.visits,
        }


@dataclass
class RunContext:
    run_id: str
    requirement: str = ""
    branch: str = ""
    base_sha: str = ""
    workdir: str = ""
    tool_root: str = ""
    diff: str = ""
    changed_files: list[str] = field(default_factory=list)
    nodes: dict[str, NodeOutput] = field(default_factory=dict)
    # 目前正在執行的節點的造訪次數，供模板的 loop.iteration 使用
    iteration: int = 1

    def output(self, node_id: str) -> NodeOutput:
        return self.nodes.setdefault(node_id, NodeOutput())

    def as_variables(self) -> dict[str, Any]:
        return {
            "requirement": self.requirement,
            "run": wrap(
                {
                    "id": self.run_id,
                    "branch": self.branch,
                    "base_sha": self.base_sha,
                    "diff": self.diff,
                    "changed_files": self.changed_files,
                }
            ),
            "nodes": wrap({k: v.as_dict() for k, v in self.nodes.items()}),
            "loop": wrap({"iteration": self.iteration}),
            "diff": self.diff,
            "changed_files": self.changed_files,
            "workdir": self.workdir,
            "tool_root": self.tool_root,
        }


# ------------------------------------------------------------------ 模板

_env = SandboxedEnvironment(
    # ChainableUndefined 讓 {{ nodes.qa.structured.issues }} 在 qa 未跑時
    # 渲染成空字串而不是丟 UndefinedError
    undefined=__import__("jinja2").ChainableUndefined,
    keep_trailing_newline=True,
    autoescape=False,
)


def render_template(template: str, variables: dict[str, Any]) -> str:
    if not template:
        return ""
    try:
        return _env.from_string(template).render(**variables)
    except Exception as exc:  # jinja 的錯誤型別很雜，統一包起來
        raise ContextError(f"prompt 模板渲染失敗: {exc}") from exc


# ------------------------------------------------------------------ 運算式


def evaluate(expr: str, variables: dict[str, Any]) -> bool:
    """求值條件運算式，回傳布林。

    用 simpleeval 的白名單求值器，不是 Python 的 eval —— 運算式雖然是使用者
    自己寫的，但沒有理由讓它能 import os。
    """
    if not expr or not expr.strip():
        raise ContextError("條件節點的運算式是空的")

    evaluator = EvalWithCompoundTypes(names=variables)
    try:
        result = evaluator.eval(expr)
    except InvalidExpression as exc:
        raise ContextError(f"條件運算式無效: {expr}\n{exc}") from exc
    except Exception as exc:
        raise ContextError(f"條件運算式求值失敗: {expr}\n{exc}") from exc

    return bool(result)
