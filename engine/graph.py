"""工作流圖：正規格式的解析與驗證。

刻意不直接吃 Drawflow 的 JSON。Drawflow 的格式很囉唆（connections 裡的
`output` 欄位其實指的是對方的 input 名稱），而且把引擎綁死在某個前端 library 上。
改成前端負責 Drawflow ↔ 正規格式的轉換，引擎只認正規格式：

    {
      "id": "plan-impl-qa",
      "name": "規劃 → 實作 → QA",
      "nodes": [
        {"id": "req",  "type": "requirement", "label": "需求"},
        {"id": "plan", "type": "codex", "label": "規劃",
         "config": {"prompt": "...", "sandbox": "read-only"}, "mutates": false},
        {"id": "gate", "type": "condition",
         "config": {"expr": "nodes.qa.structured.verdict == 'PASS'"}}
      ],
      "edges": [
        {"from": "req",  "to": "plan"},
        {"from": "gate", "to": "impl", "port": "false"}
      ],
      "settings": {"max_run_steps": 60}
    }

之後想換成 LiteGraph（ComfyUI 用的那個）只要改前端的轉換層。

環是允許的 —— QA 沒過打回去修正本質上就是一個環。安全性靠造訪次數上限，
不是靠禁止環。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# 節點 id 是使用者可見的穩定識別字，不直接拿去當檔名或 ref 名稱 ——
# 那是 safe_name() 的事。這裡只擋真的不能接受的東西：空的、含控制字元、過長。
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
MAX_NODE_ID_LEN = 200

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_DOT_RUN = re.compile(r"\.{2,}")


def safe_name(node_id: str) -> str:
    """把節點 id 轉成可以安全當目錄名與 git ref 用的名稱。

    為什麼要轉，而不是直接限制 id 的格式：

    - id 會被組成 worktree 路徑，而那個路徑底下有 rmtree。"../../victim"
      這種 id 會刪到 worktree_root 之外的東西。
    - macOS 的檔案系統預設不分大小寫。節點 "A" 與 "a" 是兩個合法且不同的節點，
      但會對應到同一個目錄 —— 建第二個的時候會強制移除第一個（那時它還在跑），
      隔離直接失效。
    - 直接限制 id 只能用 ASCII 會讓既有的工作流無法載入（先前只要求非空，
      "規劃" 這種中文 id 在共用模式下完全正常）。

    所以外部 id 保持自由，內部名稱一律是「可讀前綴 + id 的雜湊」。雜湊取自
    未經轉換的原始 id，所以大小寫或 Unicode 正規化不同的 id 不會撞在一起。

    細節都是必要的：

    - 前綴會被截斷（32 字元），所以兩個長 id 可能共用同一個前綴，唯一性完全
      靠雜湊 —— 8 個十六進位字元只有 32 bit，用生日攻擊幾萬次就能撞出來，
      所以取 16 個字元（64 bit）。graph 驗證另外還會檢查整張圖沒有碰撞。
    - 前綴裡的連續句點要收掉：`git check-ref-format` 會拒絕含 `..` 的 ref
      （`a..b` 這種 id 會讓 per_node 的 run 在建 worktree 時直接中止）。
    - 結尾永遠是 `-<hex>`，所以不可能等於 `HEAD`，也不可能以 `.lock` 結尾。
    """
    digest = hashlib.sha1(node_id.encode("utf-8")).hexdigest()[:16]
    prefix = _UNSAFE.sub("-", node_id)[:32]
    prefix = _DOT_RUN.sub(".", prefix).strip("-.")
    return f"{prefix}-{digest}" if prefix else f"node-{digest}"

# 內建節點型別（其餘的 type 必須對應一個 adapter id）
REQUIREMENT = "requirement"
CONDITION = "condition"
GIT = "git"
BUILTIN_TYPES = frozenset({REQUIREMENT, CONDITION, GIT})

DEFAULT_PORT = "out"
CONDITION_PORTS = ("true", "false")


class GraphError(Exception):
    """圖的結構或設定有問題。UI 存檔前就該把這些擋下來。"""


@dataclass
class Node:
    id: str
    type: str
    label: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    # None = 沿用 adapter 宣告的 mutates
    mutates: bool | None = None
    max_visits: int | None = None
    timeout_sec: int | None = None
    join: str = "all"  # all | any
    # 節點失敗時：fail = 中止整個 run；continue = 繼續，讓下游條件節點判斷。
    # 跑測試的節點應該用 continue —— 測試失敗是要送給條件節點的訊號，不是意外。
    on_error: str = "fail"
    pos: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise GraphError("節點缺少 id")
        if _CONTROL_CHARS.search(self.id):
            raise GraphError(f"節點 id 不能含控制字元: {self.id!r}")
        if len(self.id) > MAX_NODE_ID_LEN:
            raise GraphError(
                f"節點 id 太長（上限 {MAX_NODE_ID_LEN} 字元）: {self.id[:40]}…"
            )
        if self.join not in ("all", "any"):
            raise GraphError(f"節點 {self.id}: join 只能是 all 或 any")
        if self.on_error not in ("fail", "continue"):
            raise GraphError(f"節點 {self.id}: on_error 只能是 fail 或 continue")
        if not self.label:
            self.label = self.id

    @property
    def is_builtin(self) -> bool:
        return self.type in BUILTIN_TYPES


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    port: str = DEFAULT_PORT


@dataclass
class Graph:
    nodes: dict[str, Node]
    edges: list[Edge]
    id: str = ""
    name: str = ""
    settings: dict[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------- 拓樸查詢

    def outgoing(self, node_id: str, port: str | None = None) -> list[Edge]:
        return [
            e
            for e in self.edges
            if e.src == node_id and (port is None or e.port == port)
        ]

    def incoming(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.dst == node_id]

    def entrypoints(self) -> list[Node]:
        return [n for n in self.nodes.values() if not self.incoming(n.id)]

    def ports_of(self, node: Node) -> tuple[str, ...]:
        return CONDITION_PORTS if node.type == CONDITION else (DEFAULT_PORT,)

    # ---------------------------------------------------------- 環的處理

    def back_edges(self) -> set[Edge]:
        """找出構成環的邊（DFS 遇到還在遞迴堆疊上的節點）。

        為什麼需要區分：`all` join 的節點若把回頭邊也算進「要等的上游」，
        第一輪就會死等 —— 實作節點在等還沒跑過的 QA。
        回頭邊不列入 all join 的等待條件，只負責觸發重新進入。
        """
        found: set[Edge] = set()
        state: dict[str, int] = {}  # 0=未訪 1=在堆疊上 2=完成

        def visit(node_id: str) -> None:
            state[node_id] = 1
            for edge in self.outgoing(node_id):
                s = state.get(edge.dst, 0)
                if s == 1:
                    found.add(edge)
                elif s == 0:
                    visit(edge.dst)
            state[node_id] = 2

        # 從 entrypoint 開始，再補掃孤立的環
        for node in self.entrypoints():
            if state.get(node.id, 0) == 0:
                visit(node.id)
        for node_id in self.nodes:
            if state.get(node_id, 0) == 0:
                visit(node_id)

        return found

    def forward_incoming(self, node_id: str, back: set[Edge] | None = None) -> list[Edge]:
        back = back if back is not None else self.back_edges()
        return [e for e in self.incoming(node_id) if e not in back]

    def reachable(self) -> set[str]:
        seen: set[str] = set()
        stack = [n.id for n in self.entrypoints()]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(e.dst for e in self.outgoing(current))
        return seen


# -------------------------------------------------------------------- 解析

_NODE_KEYS = {
    "id", "type", "label", "config", "mutates", "max_visits",
    "timeout_sec", "join", "on_error", "pos",
}
_EDGE_KEYS = {"from", "to", "port"}


def parse(payload: dict[str, Any]) -> Graph:
    """把正規格式的 dict 轉成 Graph。只做結構解析，語意檢查在 validate()。"""
    if not isinstance(payload, dict):
        raise GraphError("工作流必須是一個 JSON 物件")

    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise GraphError("工作流至少要有一個節點")

    nodes: dict[str, Node] = {}
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            raise GraphError(f"節點必須是物件，收到 {raw!r}")
        unknown = set(raw) - _NODE_KEYS
        if unknown:
            raise GraphError(f"節點 {raw.get('id')}: 無法識別的欄位 {sorted(unknown)}")
        if not raw.get("type"):
            raise GraphError(f"節點 {raw.get('id')} 缺少 type")
        node = Node(**raw)
        if node.id in nodes:
            raise GraphError(f"節點 id 重複: {node.id}")
        nodes[node.id] = node

    edges: list[Edge] = []
    seen_edges: set[tuple[str, str, str]] = set()
    for raw in payload.get("edges") or []:
        if not isinstance(raw, dict):
            raise GraphError(f"邊必須是物件，收到 {raw!r}")
        unknown = set(raw) - _EDGE_KEYS
        if unknown:
            raise GraphError(f"邊有無法識別的欄位 {sorted(unknown)}")
        src, dst = raw.get("from"), raw.get("to")
        if not src or not dst:
            raise GraphError(f"邊缺少 from 或 to: {raw!r}")
        edge = Edge(src=src, dst=dst, port=raw.get("port") or DEFAULT_PORT)
        key = (edge.src, edge.dst, edge.port)
        if key in seen_edges:
            raise GraphError(f"重複的邊: {edge.src} -[{edge.port}]-> {edge.dst}")
        seen_edges.add(key)
        edges.append(edge)

    return Graph(
        nodes=nodes,
        edges=edges,
        id=payload.get("id") or "",
        name=payload.get("name") or "",
        settings=payload.get("settings") or {},
    )


def to_dict(graph: Graph) -> dict[str, Any]:
    """存回正規格式（存 db / 給前端）。"""
    return {
        "id": graph.id,
        "name": graph.name,
        "settings": graph.settings,
        "nodes": [
            {
                "id": n.id,
                "type": n.type,
                "label": n.label,
                "config": n.config,
                **({"mutates": n.mutates} if n.mutates is not None else {}),
                **({"max_visits": n.max_visits} if n.max_visits is not None else {}),
                **({"timeout_sec": n.timeout_sec} if n.timeout_sec is not None else {}),
                **({"join": n.join} if n.join != "all" else {}),
                **({"on_error": n.on_error} if n.on_error != "fail" else {}),
                **({"pos": n.pos} if n.pos else {}),
            }
            for n in graph.nodes.values()
        ],
        "edges": [
            {"from": e.src, "to": e.dst, **({"port": e.port} if e.port != DEFAULT_PORT else {})}
            for e in graph.edges
        ],
    }


# -------------------------------------------------------------------- 驗證

# 各節點型別的必填 config 欄位
REQUIRED_CONFIG: dict[str, tuple[str, ...]] = {
    CONDITION: ("expr",),
    "shell": ("command",),
}


def validate(graph: Graph, known_adapters: Iterable[str]) -> list[str]:
    """回傳問題清單（空 list = 沒問題）。

    刻意回傳清單而不是丟第一個錯 —— UI 要一次把所有問題標示出來，
    而不是讓使用者一個一個修。
    """
    problems: list[str] = []
    adapters = set(known_adapters)

    # 邊指向的節點必須存在
    for edge in graph.edges:
        if edge.src not in graph.nodes:
            problems.append(f"邊的來源節點不存在: {edge.src}")
        if edge.dst not in graph.nodes:
            problems.append(f"邊的目標節點不存在: {edge.dst}")
    if problems:
        return problems  # 節點都對不上，後面的檢查沒意義

    if not graph.entrypoints():
        problems.append(
            "沒有起點：每個節點都有入邊，整張圖是一個閉環，執行無從開始。"
            "請讓至少一個節點（通常是需求節點）沒有入邊。"
        )

    reachable = graph.reachable()
    for node in graph.nodes.values():
        if node.id not in reachable:
            problems.append(f"孤島節點 {node.label}（{node.id}）：從起點走不到，永遠不會執行")

    for node in graph.nodes.values():
        # 型別必須認識
        if not node.is_builtin and node.type not in adapters:
            problems.append(
                f"節點 {node.label}：未知的型別 {node.type}"
                f"（可用: {sorted(BUILTIN_TYPES | adapters)}）"
            )
            continue

        # 必填 config
        for key in REQUIRED_CONFIG.get(node.type, ()):
            if not str(node.config.get(key) or "").strip():
                problems.append(f"節點 {node.label}：缺少必填設定 {key}")

        # agent 節點沒有 prompt 等於沒指示
        if not node.is_builtin and node.type != "shell":
            if not str(node.config.get("prompt") or "").strip():
                problems.append(f"節點 {node.label}：agent 節點需要 prompt")

        # 條件節點的出邊 port 必須合法，且至少要有一條
        if node.type == CONDITION:
            out = graph.outgoing(node.id)
            if not out:
                problems.append(f"條件節點 {node.label}：沒有任何出邊，判斷結果無處可去")
            for edge in out:
                if edge.port not in CONDITION_PORTS:
                    problems.append(
                        f"條件節點 {node.label} 的出邊 port 必須是 true 或 false，"
                        f"收到 {edge.port}"
                    )
        else:
            for edge in graph.outgoing(node.id):
                if edge.port != DEFAULT_PORT:
                    problems.append(
                        f"節點 {node.label} 不是條件節點，出邊 port 只能是 {DEFAULT_PORT}"
                    )

        if node.max_visits is not None and node.max_visits < 1:
            problems.append(f"節點 {node.label}：max_visits 必須 >= 1")

    # 內部名稱（worktree 目錄 / branch）必須全圖唯一。safe_name 的前綴會截斷、
    # 雜湊也只取一段，理論上仍可能碰撞；撞了就是兩個節點共用同一個工作目錄，
    # 後建的會強制移除還在執行中的那個。這裡直接擋下來。
    by_safe: dict[str, list[str]] = {}
    for node_id in graph.nodes:
        by_safe.setdefault(safe_name(node_id), []).append(node_id)
    for safe, ids in by_safe.items():
        if len(ids) > 1:
            problems.append(
                f"這些節點 id 會對應到同一個內部名稱 {safe}: {sorted(ids)}。"
                "請把其中一個改名。"
            )

    problems.extend(_validate_settings(graph))
    return problems


def _validate_settings(graph: Graph) -> list[str]:
    from engine.isolation import MODES  # 避免模組互相 import

    problems: list[str] = []
    isolation = graph.settings.get("isolation")
    if isolation is not None and isolation not in MODES:
        problems.append(
            f"settings.isolation 只能是 {' / '.join(MODES)}，收到 {isolation!r}"
        )

    steps = graph.settings.get("max_run_steps")
    if steps is not None and (not isinstance(steps, int) or steps < 1):
        problems.append("settings.max_run_steps 必須是 >= 1 的整數")

    return problems
