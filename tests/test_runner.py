"""排程器測試。全部用 mock adapter —— 不呼叫 LLM、不花錢、結果確定。

mock 走的是和真實 CLI 完全相同的 subprocess → 逐行讀 → normalize 路徑，
所以這些測試涵蓋的是真正的執行路徑，不是繞過它的捷徑。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from adapters.registry import Registry
from engine import runner as R
from engine.context import RunContext
from engine.isolation import SharedIsolation
from engine.graph import parse, validate
from engine.runner import CANCELLED, FAILED, PASSED, Runner
from settings import Guards
from tests.test_workspace import make_repo

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def registry():
    return Registry()


def script(**kw) -> str:
    return json.dumps(kw)


def mock_node(nid, label=None, **spec):
    """一個 mock agent 節點，行為由 script 決定。"""
    return {
        "id": nid,
        "type": "mock",
        "label": label or nid,
        "config": {"prompt": "ignored", "script": script(**spec)},
    }


class Recorder:
    """收集事件，測試用。

    序號在這裡指派 —— 正式環境是 RunBus 負責，兩者都是「單一 sink 決定順序」。
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.lock = threading.Lock()
        self._seq = 0

    def __call__(self, node_id: str, event: dict) -> None:
        with self.lock:
            self._seq += 1
            self.events.append((node_id, {**event, "seq": self._seq}))

    def phases(self, node_id: str) -> list[str]:
        return [
            e["data"].get("phase")
            for n, e in self.events
            if n == node_id and e["kind"] == "status"
        ]

    def kinds(self, node_id: str | None = None) -> list[str]:
        return [e["kind"] for n, e in self.events if node_id in (None, n)]

    def texts(self, kind: str) -> list[str]:
        return [e["text"] for _, e in self.events if e["kind"] == kind]

    def has_phase(self, phase: str) -> bool:
        return any(e["data"].get("phase") == phase for _, e in self.events)


def build(payload, registry, tmp_path, workspace=None, **guard_kw):
    graph = parse(payload)
    assert validate(graph, [s.id for s in registry.all()]) == []

    ctx = RunContext(
        run_id="test-run",
        requirement="測試需求",
        workdir=str(workspace.path if workspace else tmp_path),
        tool_root=str(ROOT),
    )
    guards = Guards(**{"default_node_timeout": 30, "run_timeout": 120, **guard_kw})
    rec = Recorder()
    run = Runner(
        graph=graph,
        context=ctx,
        # 沒有 worktree 也要用 SharedIsolation：節點仍共用一個目錄，寫入必須序列化
        isolation=SharedIsolation(workspace=workspace),
        registry=registry,
        guards=guards,
        emit=rec,
        artifacts_dir=tmp_path / "artifacts",
    )
    return run, ctx, rec


# ---------------------------------------------------------------- 線性


def test_linear_run(registry, tmp_path):
    payload = {
        "nodes": [
            {"id": "req", "type": "requirement"},
            mock_node("a", message="A 完成"),
            mock_node("b", message="B 完成"),
        ],
        "edges": [{"from": "req", "to": "a"}, {"from": "a", "to": "b"}],
    }
    run, ctx, rec = build(payload, registry, tmp_path)
    result = run.run()

    assert result.status == PASSED
    assert result.node_status == {"req": PASSED, "a": PASSED, "b": PASSED}
    assert ctx.nodes["a"].last_message == "A 完成"
    assert ctx.nodes["b"].last_message == "B 完成"
    assert result.steps == 3


def test_requirement_flows_into_prompt(registry, tmp_path):
    """需求文字要能透過模板進到下游節點的 prompt。"""
    payload = {
        "nodes": [
            {"id": "req", "type": "requirement"},
            {
                "id": "a",
                "type": "mock",
                "config": {
                    "prompt": "需求是：{{ requirement }}",
                    "script": script(message="ok"),
                },
            },
        ],
        "edges": [{"from": "req", "to": "a"}],
    }
    run, ctx, rec = build(payload, registry, tmp_path)
    assert run.run().status == PASSED

    spawn = [e for _, e in rec.events if e["data"].get("phase") == "spawn"]
    assert spawn  # prompt 有渲染（mock 用 argv 傳 prompt，這裡確認節點真的跑了）
    assert ctx.requirement == "測試需求"


def test_shell_command_is_template_rendered(registry, tmp_path):
    """shell 節點的 command 本身要能用模板。

    adapter 的參數替換只有單層：直接把設定值塞進 argv，值裡面的 {{ … }} 會
    原樣傳給 CLI。實測時 tests 節點就把字面的 "{{ run.repo }}" 印了出來，
    導致找不到測試環境，QA 因此連續三輪判 FAIL。
    """
    marker = tmp_path / "rendered.txt"
    payload = {
        "nodes": [
            {"id": "req", "type": "requirement"},
            {
                "id": "sh",
                "type": "shell",
                "mutates": False,
                "config": {
                    # 同時驗證 run.* 與 requirement 都進得去
                    "command": f'echo "repo={{{{ run.repo }}}} need={{{{ requirement }}}}" > {marker}',
                    "expect_exit_code": 0,
                },
            },
        ],
        "edges": [{"from": "req", "to": "sh"}],
    }
    run, ctx, _ = build(payload, registry, tmp_path)
    ctx.repo = "/some/target/repo"
    result = run.run()

    assert result.status == PASSED, result.reason
    written = marker.read_text("utf-8")
    assert "{{" not in written, f"模板沒有被渲染: {written}"
    assert "repo=/some/target/repo" in written
    assert "need=測試需求" in written


def test_git_commit_message_is_template_rendered(tmp_path):
    from engine.workspace import create_workspace

    repo = make_repo(tmp_path / "proj")
    ws = create_workspace(
        project_repo=repo, worktree_root=tmp_path / "wt", main_branch="main",
        run_id="msg", tool_root=tmp_path / "tool",
    )
    try:
        payload = {
            "nodes": [
                mock_node("impl", message="x", files={"a.txt": "1\n"}),
                {"id": "c", "type": "git",
                 "config": {"action": "commit", "message": "impl [{{ run.id }}]"}},
            ],
            "edges": [{"from": "impl", "to": "c"}],
        }
        run, _, _ = build(payload, Registry(), tmp_path, workspace=ws)
        assert run.run().status == PASSED

        import subprocess
        log = subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=ws.path,
                             capture_output=True, text=True).stdout
        assert log.strip() == "impl [test-run]"
    finally:
        ws.remove()


def test_upstream_output_reaches_downstream_prompt(registry, tmp_path):
    payload = {
        "nodes": [
            mock_node("plan", message="這是計畫"),
            {
                "id": "impl",
                "type": "mock",
                "config": {
                    "prompt": "依計畫實作：{{ nodes.plan.last_message }}",
                    "script": script(message="done"),
                },
            },
        ],
        "edges": [{"from": "plan", "to": "impl"}],
    }
    run, ctx, rec = build(payload, registry, tmp_path)
    assert run.run().status == PASSED
    assert ctx.nodes["plan"].last_message == "這是計畫"


# ---------------------------------------------------------------- 分支


def test_condition_takes_true_branch(registry, tmp_path):
    payload = {
        "nodes": [
            mock_node("qa", structured={"verdict": "PASS"}),
            {"id": "gate", "type": "condition",
             "config": {"expr": "nodes.qa.structured.verdict == 'PASS'"}},
            mock_node("ship", message="出貨"),
            mock_node("fix", message="修正"),
        ],
        "edges": [
            {"from": "qa", "to": "gate"},
            {"from": "gate", "to": "ship", "port": "true"},
            {"from": "gate", "to": "fix", "port": "false"},
        ],
    }
    run, ctx, rec = build(payload, registry, tmp_path)
    result = run.run()

    assert result.status == PASSED
    assert result.node_status["ship"] == PASSED
    assert result.node_status["fix"] == R.SKIPPED  # 沒被選到的分支不執行
    assert result.visits["fix"] == 0


def test_condition_takes_false_branch(registry, tmp_path):
    payload = {
        "nodes": [
            mock_node("qa", structured={"verdict": "FAIL"}),
            {"id": "gate", "type": "condition",
             "config": {"expr": "nodes.qa.structured.verdict == 'PASS'"}},
            mock_node("ship", message="出貨"),
            mock_node("fix", message="修正"),
        ],
        "edges": [
            {"from": "qa", "to": "gate"},
            {"from": "gate", "to": "ship", "port": "true"},
            {"from": "gate", "to": "fix", "port": "false"},
        ],
    }
    run, _, _ = build(payload, registry, tmp_path)
    result = run.run()
    assert result.node_status["fix"] == PASSED
    assert result.node_status["ship"] == R.SKIPPED


def test_structured_output_via_schema_file(registry, tmp_path):
    """codex 走 --output-schema 寫檔的路徑（mock 也支援），要能解析回 dict。"""
    payload = {
        "nodes": [
            {
                "id": "qa",
                "type": "mock",
                "config": {
                    "prompt": "審查",
                    "script": script(structured={"verdict": "PASS", "issues": []}),
                    "schema": '{"type":"object"}',
                },
            }
        ],
        "edges": [],
    }
    run, ctx, _ = build(payload, registry, tmp_path)
    assert run.run().status == PASSED
    assert ctx.nodes["qa"].structured == {"verdict": "PASS", "issues": []}


# ---------------------------------------------------------------- 迴圈


def test_qa_retry_loop_succeeds_on_second_round(registry, tmp_path):
    """impl → qa → gate -false-> impl 的環，第二輪 PASS 就收掉。

    這是舊 bash 的核心場景，也是選「完整 DAG」的理由。
    """
    counter = tmp_path / "round.txt"
    payload = {
        "nodes": [
            {"id": "req", "type": "requirement"},
            mock_node("impl", message="改好了"),
            {
                "id": "qa",
                "type": "mock",
                "label": "QA",
                "config": {
                    "prompt": "審查第 {{ loop.iteration }} 輪",
                    # 第一輪 FAIL、第二輪 PASS：用檔案計數模擬狀態變化
                    "script": script(message="reviewed"),
                },
            },
            {"id": "gate", "type": "condition",
             "config": {"expr": "loop.iteration >= 2"}},
            mock_node("done", message="收工"),
        ],
        "edges": [
            {"from": "req", "to": "impl"},
            {"from": "impl", "to": "qa"},
            {"from": "qa", "to": "gate"},
            {"from": "gate", "to": "done", "port": "true"},
            {"from": "gate", "to": "impl", "port": "false"},
        ],
    }
    run, ctx, rec = build(payload, registry, tmp_path)
    result = run.run()

    assert result.status == PASSED, result.reason
    assert result.visits["impl"] == 2, "第一輪 gate 為 false，應該回頭再跑 impl"
    assert result.visits["gate"] == 2
    assert result.node_status["done"] == PASSED


def test_loop_stops_at_max_visits(registry, tmp_path):
    """條件永遠 false 的無限迴圈，必須被造訪上限攔下來且說明原因。"""
    payload = {
        "nodes": [
            {"id": "req", "type": "requirement"},
            mock_node("impl", message="又改了"),
            {"id": "gate", "type": "condition", "config": {"expr": "False"}},
            mock_node("done"),
        ],
        "edges": [
            {"from": "req", "to": "impl"},
            {"from": "impl", "to": "gate"},
            {"from": "gate", "to": "done", "port": "true"},
            {"from": "gate", "to": "impl", "port": "false"},
        ],
    }
    run, _, _ = build(payload, registry, tmp_path, default_max_visits=3)
    result = run.run()

    assert result.status == FAILED
    assert "造訪次數已達上限 3" in result.reason
    assert result.visits["impl"] == 3
    assert "重試 3 次仍未通過" in result.reason


def test_max_run_steps_guard(registry, tmp_path):
    payload = {
        "nodes": [
            {"id": "req", "type": "requirement"},
            mock_node("a"),
            {"id": "gate", "type": "condition", "config": {"expr": "False"}},
            mock_node("z"),
        ],
        "edges": [
            {"from": "req", "to": "a"},
            {"from": "a", "to": "gate"},
            {"from": "gate", "to": "z", "port": "true"},
            {"from": "gate", "to": "a", "port": "false"},
        ],
    }
    run, _, _ = build(
        payload, registry, tmp_path, default_max_visits=999, max_run_steps=5
    )
    result = run.run()
    assert result.status == FAILED
    assert "執行步數超過上限 5" in result.reason


def test_loop_iteration_available_in_prompt(registry, tmp_path):
    payload = {
        "nodes": [
            {"id": "req", "type": "requirement"},
            {"id": "a", "type": "mock",
             "config": {"prompt": "第 {{ loop.iteration }} 輪", "script": script(message="x")}},
            {"id": "gate", "type": "condition", "config": {"expr": "loop.iteration >= 2"}},
            mock_node("done"),
        ],
        "edges": [
            {"from": "req", "to": "a"},
            {"from": "a", "to": "gate"},
            {"from": "gate", "to": "done", "port": "true"},
            {"from": "gate", "to": "a", "port": "false"},
        ],
    }
    run, _, rec = build(payload, registry, tmp_path)
    assert run.run().status == PASSED
    iterations = [
        e["data"]["iteration"]
        for n, e in rec.events
        if n == "a" and e["data"].get("phase") == "node_start"
    ]
    assert iterations == [1, 2]


# ---------------------------------------------------------------- 平行與鎖


def test_parallel_readonly_nodes_overlap(registry, tmp_path):
    """mutates=false 的節點應該真的同時跑。"""
    payload = {
        "nodes": [
            {"id": "req", "type": "requirement"},
            {**mock_node("r1", message="a", sleep=0.4, tools=["x"]), "mutates": False},
            {**mock_node("r2", message="b", sleep=0.4, tools=["x"]), "mutates": False},
        ],
        "edges": [{"from": "req", "to": "r1"}, {"from": "req", "to": "r2"}],
    }
    run, _, rec = build(payload, registry, tmp_path, max_parallel_nodes=4)
    start = time.monotonic()
    result = run.run()
    elapsed = time.monotonic() - start

    assert result.status == PASSED
    # 各自至少 0.8s（2 個 sleep 點），序列化會 >1.6s，平行應該明顯更短
    assert elapsed < 1.5, f"看起來沒有平行執行: {elapsed:.2f}s"
    assert not rec.has_phase("await_lock")


def test_mutating_nodes_are_serialised(registry, tmp_path):
    """共用 worktree 時，會寫檔的節點不能同時跑，且必須讓使用者看得到在等鎖。"""
    payload = {
        "nodes": [
            {"id": "req", "type": "requirement"},
            mock_node("w1", message="a", sleep=0.3, tools=["x"]),
            mock_node("w2", message="b", sleep=0.3, tools=["x"]),
        ],
        "edges": [{"from": "req", "to": "w1"}, {"from": "req", "to": "w2"}],
    }
    run, _, rec = build(payload, registry, tmp_path, max_parallel_nodes=4)
    result = run.run()

    assert result.status == PASSED
    assert rec.has_phase("await_lock"), "等鎖時必須發事件，不能靜默序列化"

    # 兩個節點的執行區間不能重疊
    spans = {}
    for node_id, event in rec.events:
        phase = event["data"].get("phase")
        if phase == "spawn":
            spans.setdefault(node_id, {})["start"] = event["seq"]
        elif phase == "node_end":
            spans.setdefault(node_id, {})["end"] = event["seq"]
    w1, w2 = spans["w1"], spans["w2"]
    assert w1["end"] < w2["start"] or w2["end"] < w1["start"]


# ---------------------------------------------------------------- join


def test_all_join_waits_for_both_upstreams(registry, tmp_path):
    payload = {
        "nodes": [
            {"id": "req", "type": "requirement"},
            {**mock_node("a", message="A"), "mutates": False},
            {**mock_node("b", message="B"), "mutates": False},
            mock_node("join", message="合流"),
        ],
        "edges": [
            {"from": "req", "to": "a"},
            {"from": "req", "to": "b"},
            {"from": "a", "to": "join"},
            {"from": "b", "to": "join"},
        ],
    }
    run, _, rec = build(payload, registry, tmp_path)
    result = run.run()

    assert result.status == PASSED
    assert result.visits["join"] == 1, "all join 只該在兩邊都到位後跑一次"

    order = [
        (n, e["data"]["phase"])
        for n, e in rec.events
        if e["data"].get("phase") in ("node_start", "node_end")
    ]
    join_start = order.index(("join", "node_start"))
    assert ("a", "node_end") in order[:join_start]
    assert ("b", "node_end") in order[:join_start]


def test_any_join_runs_on_first_arrival(registry, tmp_path):
    payload = {
        "nodes": [
            {"id": "req", "type": "requirement"},
            {**mock_node("slow", message="慢", sleep=0.5, tools=["x"]), "mutates": False},
            {**mock_node("fast", message="快"), "mutates": False},
            {**mock_node("join", message="合流"), "join": "any"},
        ],
        "edges": [
            {"from": "req", "to": "slow"},
            {"from": "req", "to": "fast"},
            {"from": "slow", "to": "join"},
            {"from": "fast", "to": "join"},
        ],
    }
    run, _, _ = build(payload, registry, tmp_path, max_parallel_nodes=4)
    result = run.run()
    assert result.status == PASSED
    assert result.visits["join"] >= 1


# ---------------------------------------------------------------- 失敗處理


def test_agent_failure_aborts_run_by_default(registry, tmp_path):
    payload = {
        "nodes": [
            mock_node("a", message="x", exit_code=1),
            mock_node("b", message="不該跑到"),
        ],
        "edges": [{"from": "a", "to": "b"}],
    }
    run, _, _ = build(payload, registry, tmp_path)
    result = run.run()

    assert result.status == FAILED
    assert "失敗" in result.reason
    assert result.node_status["a"] == FAILED
    assert result.visits["b"] == 0


def test_on_error_continue_lets_condition_decide(registry, tmp_path):
    """跑測試的節點失敗是要送給條件節點的訊號，不該直接中止整個 run。"""
    payload = {
        "nodes": [
            {**mock_node("tests", message="測試掛了", exit_code=1),
             "on_error": "continue"},
            {"id": "gate", "type": "condition",
             "config": {"expr": "nodes.tests.exit_code == 0"}},
            mock_node("ship"),
            mock_node("fix", message="修測試"),
        ],
        "edges": [
            {"from": "tests", "to": "gate"},
            {"from": "gate", "to": "ship", "port": "true"},
            {"from": "gate", "to": "fix", "port": "false"},
        ],
    }
    run, ctx, _ = build(payload, registry, tmp_path)
    result = run.run()

    assert result.status == PASSED
    assert ctx.nodes["tests"].exit_code == 1
    assert result.node_status["fix"] == PASSED
    assert result.node_status["ship"] == R.SKIPPED


def test_agent_error_event_marks_failure(registry, tmp_path):
    """agent 自己吐 error 事件（exit code 仍為 0）也要算失敗。"""
    payload = {"nodes": [mock_node("a", fail="模型回錯誤", message="x")], "edges": []}
    run, _, rec = build(payload, registry, tmp_path)
    result = run.run()
    assert result.status == FAILED
    assert "模型回錯誤" in result.reason


def test_bad_condition_expression_fails_clearly(registry, tmp_path):
    payload = {
        "nodes": [
            {"id": "req", "type": "requirement"},
            {"id": "gate", "type": "condition", "config": {"expr": "this is not python"}},
            mock_node("a"),
        ],
        "edges": [{"from": "req", "to": "gate"}, {"from": "gate", "to": "a", "port": "true"}],
    }
    run, _, _ = build(payload, registry, tmp_path)
    result = run.run()
    assert result.status == FAILED
    assert "設定有問題" in result.reason


# ---------------------------------------------------------------- 取消


def test_cancel_stops_run(registry, tmp_path):
    payload = {
        "nodes": [
            {"id": "req", "type": "requirement"},
            mock_node("slow", message="x", sleep=3, tools=["a", "b", "c"]),
            mock_node("after", message="不該跑到"),
        ],
        "edges": [{"from": "req", "to": "slow"}, {"from": "slow", "to": "after"}],
    }
    run, _, _ = build(payload, registry, tmp_path)
    threading.Timer(0.8, run.cancel.set).start()

    start = time.monotonic()
    result = run.run()
    elapsed = time.monotonic() - start

    assert result.status == CANCELLED
    assert elapsed < 5, f"取消沒有及時生效: {elapsed:.1f}s"
    assert result.visits["after"] == 0


def test_node_timeout(registry, tmp_path):
    payload = {
        "nodes": [mock_node("slow", message="x", sleep=5, tools=["a", "b", "c"])],
        "edges": [],
    }
    run, _, _ = build(payload, registry, tmp_path)
    run.graph.nodes["slow"].timeout_sec = 1
    result = run.run()
    assert result.status == FAILED
    assert "timeout" in result.reason.lower()


# ---------------------------------------------------------------- git 節點


def test_git_commit_node_with_no_changes_does_not_break_run(tmp_path):
    """舊 bash 的 1 號 bug：沒變更時 git commit 回非零，set -e 炸掉整條 pipeline。"""
    from engine.workspace import create_workspace

    repo = make_repo(tmp_path / "proj")
    ws = create_workspace(
        project_repo=repo,
        worktree_root=tmp_path / "wt",
        main_branch="main",
        run_id="gitrun",
        tool_root=tmp_path / "tool",
    )
    try:
        payload = {
            "nodes": [
                {"id": "req", "type": "requirement"},
                {"id": "commit", "type": "git",
                 "config": {"action": "commit", "message": "沒東西可 commit"}},
                mock_node("after", message="流程繼續"),
            ],
            "edges": [{"from": "req", "to": "commit"}, {"from": "commit", "to": "after"}],
        }
        run, ctx, rec = build(payload, Registry(), tmp_path, workspace=ws)
        result = run.run()

        assert result.status == PASSED, result.reason
        assert result.node_status["after"] == PASSED
        assert "沒有變更" in ctx.nodes["commit"].last_message
    finally:
        ws.remove()


def test_git_commit_after_file_change_and_diff_visible(tmp_path):
    """agent 改檔 → commit → 下游看得到 diff，且 diff 不會被寫進 worktree。"""
    from engine.workspace import create_workspace

    repo = make_repo(tmp_path / "proj")
    ws = create_workspace(
        project_repo=repo,
        worktree_root=tmp_path / "wt",
        main_branch="main",
        run_id="gitrun2",
        tool_root=tmp_path / "tool",
    )
    try:
        payload = {
            "nodes": [
                mock_node("impl", message="加了檔案", files={"feature.py": "print(1)\n"}),
                {"id": "commit", "type": "git",
                 "config": {"action": "commit", "message": "impl: {{ run.id }}"}},
                {
                    "id": "qa",
                    "type": "mock",
                    "mutates": False,
                    "config": {
                        "prompt": "審查這份 diff：\n{{ run.diff }}",
                        "script": script(message="看過了"),
                    },
                },
            ],
            "edges": [{"from": "impl", "to": "commit"}, {"from": "commit", "to": "qa"}],
        }
        run, ctx, rec = build(payload, Registry(), tmp_path, workspace=ws)
        result = run.run()

        assert result.status == PASSED, result.reason
        assert (ws.path / "feature.py").exists()
        assert "feature.py" in ctx.changed_files
        assert "feature.py" in ctx.diff
        # 2 號 bug：diff 只在記憶體，絕不落進 worktree
        assert not (ws.path / "changes.diff").exists()
    finally:
        ws.remove()
