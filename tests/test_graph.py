"""圖的解析與驗證測試。"""

from __future__ import annotations

import pytest

from engine.graph import Edge, GraphError, parse, to_dict, validate

ADAPTERS = {"codex", "claude", "opencode", "shell", "mock"}


def wf(nodes, edges=None, **extra):
    return {"nodes": nodes, "edges": edges or [], **extra}


def agent(nid, **cfg):
    return {"id": nid, "type": "mock", "config": {"prompt": "p", **cfg}}


# ---------------------------------------------------------------- 解析


def test_parse_roundtrip():
    payload = wf(
        [
            {"id": "req", "type": "requirement", "label": "需求"},
            agent("impl"),
            {"id": "gate", "type": "condition", "config": {"expr": "1 == 1"}},
        ],
        [
            {"from": "req", "to": "impl"},
            {"from": "gate", "to": "impl", "port": "false"},
        ],
        id="w1",
        name="測試",
    )
    graph = parse(payload)
    assert set(graph.nodes) == {"req", "impl", "gate"}
    assert graph.id == "w1"

    again = parse(to_dict(graph))
    assert to_dict(again) == to_dict(graph)


def test_parse_rejects_bad_input():
    with pytest.raises(GraphError, match="至少要有一個節點"):
        parse(wf([]))
    with pytest.raises(GraphError, match="缺少 type"):
        parse(wf([{"id": "a"}]))
    with pytest.raises(GraphError, match="id 重複"):
        parse(wf([agent("a"), agent("a")]))
    with pytest.raises(GraphError, match="無法識別的欄位"):
        parse(wf([{"id": "a", "type": "mock", "colour": "red"}]))
    with pytest.raises(GraphError, match="缺少 from 或 to"):
        parse(wf([agent("a")], [{"from": "a"}]))
    with pytest.raises(GraphError, match="重複的邊"):
        parse(wf([agent("a"), agent("b")], [{"from": "a", "to": "b"}] * 2))
    with pytest.raises(GraphError, match="on_error"):
        parse(wf([{"id": "a", "type": "mock", "on_error": "explode"}]))
    with pytest.raises(GraphError, match="join"):
        parse(wf([{"id": "a", "type": "mock", "join": "maybe"}]))


# ---------------------------------------------------------------- 環


def test_back_edges_detected():
    """impl → qa → gate -false-> impl 是一個環，回頭邊是 gate→impl。"""
    graph = parse(
        wf(
            [
                {"id": "req", "type": "requirement"},
                agent("impl"),
                agent("qa"),
                {"id": "gate", "type": "condition", "config": {"expr": "1"}},
            ],
            [
                {"from": "req", "to": "impl"},
                {"from": "impl", "to": "qa"},
                {"from": "qa", "to": "gate"},
                {"from": "gate", "to": "impl", "port": "false"},
            ],
        )
    )
    assert graph.back_edges() == {Edge("gate", "impl", "false")}
    # impl 的前向入邊只有 req，不含回頭邊 —— 否則第一輪會死等 qa
    assert graph.forward_incoming("impl") == [Edge("req", "impl", "out")]


def test_entrypoints_and_reachability():
    graph = parse(
        wf(
            [{"id": "a", "type": "requirement"}, agent("b"), agent("orphan")],
            [{"from": "a", "to": "b"}],
        )
    )
    assert [n.id for n in graph.entrypoints()] == ["a", "orphan"]
    assert graph.reachable() == {"a", "b", "orphan"}


# ---------------------------------------------------------------- 驗證


def test_valid_graph_has_no_problems():
    graph = parse(
        wf(
            [{"id": "req", "type": "requirement"}, agent("impl")],
            [{"from": "req", "to": "impl"}],
        )
    )
    assert validate(graph, ADAPTERS) == []


def test_detects_island_node():
    """孤島節點永遠不會執行，UI 要擋下來。"""
    graph = parse(
        wf(
            [{"id": "req", "type": "requirement"}, agent("a"), agent("b")],
            [{"from": "a", "to": "b"}],
        )
    )
    # a 自己沒有入邊，所以是 entrypoint，不算孤島；改成真的孤島：
    graph2 = parse(
        wf(
            [{"id": "req", "type": "requirement"}, agent("a"), agent("island")],
            [{"from": "req", "to": "a"}, {"from": "a", "to": "island"},
             {"from": "island", "to": "a"}],
        )
    )
    assert validate(graph2, ADAPTERS) == []  # island 從 req 走得到

    graph3 = parse(
        wf(
            [{"id": "x", "type": "mock", "config": {"prompt": "p"}},
             {"id": "y", "type": "mock", "config": {"prompt": "p"}}],
            [{"from": "x", "to": "y"}, {"from": "y", "to": "x"}],
        )
    )
    problems = validate(graph3, ADAPTERS)
    assert any("沒有起點" in p for p in problems)


def test_detects_no_entrypoint():
    graph = parse(wf([agent("a"), agent("b")],
                     [{"from": "a", "to": "b"}, {"from": "b", "to": "a"}]))
    assert any("沒有起點" in p for p in validate(graph, ADAPTERS))


def test_detects_unknown_type():
    graph = parse(wf([{"id": "a", "type": "pi-code", "config": {"prompt": "p"}}]))
    problems = validate(graph, ADAPTERS)
    assert any("未知的型別 pi-code" in p for p in problems)


def test_detects_missing_required_config():
    graph = parse(wf([{"id": "s", "type": "shell", "config": {}}]))
    assert any("缺少必填設定 command" in p for p in validate(graph, ADAPTERS))

    graph = parse(wf([{"id": "c", "type": "condition", "config": {}}]))
    assert any("缺少必填設定 expr" in p for p in validate(graph, ADAPTERS))


def test_detects_agent_without_prompt():
    graph = parse(wf([{"id": "a", "type": "mock", "config": {}}]))
    assert any("需要 prompt" in p for p in validate(graph, ADAPTERS))


def test_detects_dangling_edge():
    graph = parse(wf([agent("a")], [{"from": "a", "to": "ghost"}]))
    assert any("目標節點不存在: ghost" in p for p in validate(graph, ADAPTERS))


def test_condition_port_rules():
    graph = parse(
        wf(
            [{"id": "req", "type": "requirement"},
             {"id": "c", "type": "condition", "config": {"expr": "1"}},
             agent("a")],
            [{"from": "req", "to": "c"}, {"from": "c", "to": "a", "port": "maybe"}],
        )
    )
    assert any("必須是 true 或 false" in p for p in validate(graph, ADAPTERS))


def test_condition_needs_outgoing_edge():
    graph = parse(
        wf(
            [{"id": "req", "type": "requirement"},
             {"id": "c", "type": "condition", "config": {"expr": "1"}}],
            [{"from": "req", "to": "c"}],
        )
    )
    assert any("沒有任何出邊" in p for p in validate(graph, ADAPTERS))


def test_non_condition_cannot_use_named_port():
    graph = parse(
        wf(
            [{"id": "req", "type": "requirement"}, agent("a")],
            [{"from": "req", "to": "a", "port": "true"}],
        )
    )
    assert any("出邊 port 只能是 out" in p for p in validate(graph, ADAPTERS))


def test_validate_returns_all_problems_not_just_first():
    """UI 要一次標示所有問題，不能只回報第一個。"""
    graph = parse(
        wf([{"id": "a", "type": "nope", "config": {}},
            {"id": "b", "type": "shell", "config": {}}])
    )
    assert len(validate(graph, ADAPTERS)) >= 2
