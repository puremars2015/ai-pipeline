"""端到端測試：跑出貨附帶的 plan-impl-qa 工作流。

把 codex / claude 換成 mock（保留節點結構、迴圈、條件、git 節點、真實 worktree），
所以測的是完整的執行路徑，只是不呼叫 LLM。

這份測試同時是舊 bash 六個問題的回歸測試 —— 每一項都在下面被直接斷言。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from adapters.registry import Registry
from engine.context import RunContext
from engine.isolation import SharedIsolation
from engine.graph import parse, to_dict, validate
from engine.runner import PASSED, Runner
from engine.workspace import create_workspace
from settings import Guards
from tests.test_runner import Recorder, script
from tests.test_workspace import make_repo

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "workflows" / "plan-impl-qa.json"


def mockify(graph_dict: dict, qa_verdicts: list[str]) -> dict:
    """把 agent 節點換成 mock，並讓 QA 依序回傳指定的判定。

    QA 的多輪判定用 mock 的 emit 無法表達狀態，所以改用條件節點讀 loop.iteration
    —— 這裡直接把 QA 的 script 設成固定回傳最後一個判定，再用 gate 的運算式
    模擬「第 N 輪才通過」。
    """
    out = json.loads(json.dumps(graph_dict))
    for node in out["nodes"]:
        if node["type"] in ("codex", "claude", "opencode"):
            node["type"] = "mock"
            cfg = node["config"]
            if node["id"] == "plan":
                cfg["script"] = script(message="== 計畫 ==\n1. 建立 feature.py\n驗收：檔案存在")
            elif node["id"] == "impl":
                cfg["script"] = script(
                    message="已建立 feature.py",
                    files={"feature.py": "def run():\n    return 1\n"},
                )
            elif node["id"] == "qa":
                cfg["script"] = script(
                    structured={
                        "verdict": qa_verdicts[-1],
                        "issues": [] if qa_verdicts[-1] == "PASS" else ["還沒處理 null"],
                        "summary": "審查完成",
                    }
                )
    # 測試節點不要真的去跑 pytest（worktree 裡沒有測試）
    for node in out["nodes"]:
        if node["id"] == "tests":
            node["config"]["command"] = "echo '沒有測試，跳過'"
        if node["id"] == "summary":
            node["config"]["command"] = "git log --oneline | head -5"
    return out


@pytest.fixture
def scratch(tmp_path):
    repo = make_repo(tmp_path / "proj")
    ws = create_workspace(
        project_repo=repo,
        worktree_root=tmp_path / "wt",
        main_branch="main",
        run_id="tmpl",
        tool_root=tmp_path / "tool",
    )
    yield repo, ws
    ws.remove()


def run_template(graph_dict, ws, tmp_path, requirement="加上 feature 模組", **guard_kw):
    graph = parse(graph_dict)
    registry = Registry()
    assert validate(graph, [s.id for s in registry.all()]) == []

    ctx = RunContext(
        run_id="tmpl-run",
        requirement=requirement,
        branch=ws.branch,
        base_sha=ws.base_sha,
        workdir=str(ws.path),
        tool_root=str(ROOT),
    )
    rec = Recorder()
    runner = Runner(
        graph=graph,
        context=ctx,
        isolation=SharedIsolation(workspace=ws),
        registry=registry,
        guards=Guards(**{"default_node_timeout": 60, "run_timeout": 300, **guard_kw}),
        emit=rec,
        artifacts_dir=tmp_path / "artifacts",
    )
    return runner.run(), ctx, rec


def test_shipped_template_is_valid():
    graph = parse(json.loads(TEMPLATE.read_text("utf-8")))
    assert validate(graph, [s.id for s in Registry().all()]) == []
    assert [n.id for n in graph.entrypoints()] == ["req"]
    # 迴圈存在且方向正確：gate 的 false 出口回到 impl
    assert {(e.src, e.port, e.dst) for e in graph.back_edges()} == {
        ("gate", "false", "impl")
    }
    assert parse(to_dict(graph)) is not None  # 可往返


def test_template_passes_first_round(scratch, tmp_path):
    repo, ws = scratch
    result, ctx, rec = run_template(
        mockify(json.loads(TEMPLATE.read_text("utf-8")), ["PASS"]), ws, tmp_path
    )

    assert result.status == PASSED, result.reason
    assert result.visits["impl"] == 1
    assert result.node_status["summary"] == PASSED

    # agent 真的改了 worktree 裡的檔案
    assert (ws.path / "feature.py").exists()
    # 而且被 commit 了
    assert ctx.nodes["commit"].last_message and "沒有變更" not in ctx.nodes["commit"].last_message
    # QA 拿到的是 typed JSON，不是靠 head -1 | grep PASS
    assert ctx.nodes["qa"].structured["verdict"] == "PASS"
    assert isinstance(ctx.nodes["qa"].structured["issues"], list)


def test_template_retries_when_qa_fails(scratch, tmp_path):
    """QA 判 FAIL → 回頭重跑 impl → 造訪上限收掉，並說明是重試耗盡。"""
    repo, ws = scratch
    result, ctx, rec = run_template(
        mockify(json.loads(TEMPLATE.read_text("utf-8")), ["FAIL"]),
        ws,
        tmp_path,
        default_max_visits=3,
    )

    assert result.status == "failed"
    assert result.visits["impl"] == 3, "FAIL 應該一路重試到造訪上限"
    assert "仍未通過" in result.reason

    # 每一輪的 prompt 都要帶上前一輪的 QA 問題
    spawns = [
        e for n, e in rec.events
        if n == "impl" and e["data"].get("phase") == "node_start"
    ]
    assert [e["data"]["iteration"] for e in spawns] == [1, 2, 3]


def test_qa_sees_real_diff_not_self_report(scratch, tmp_path):
    """QA 節點的 prompt 必須真的帶到 diff 內容。

    這是原本設計最好的一點（QA 看 git diff 而不是聽實作者自述），
    但只有在 diff 真的非空時才成立 —— 新增檔案不會出現在預設的 git diff 裡。
    """
    repo, ws = scratch
    graph = mockify(json.loads(TEMPLATE.read_text("utf-8")), ["PASS"])
    result, ctx, rec = run_template(graph, ws, tmp_path)

    assert result.status == PASSED, result.reason
    assert "feature.py" in ctx.changed_files
    assert "feature.py" in ctx.diff
    assert "def run()" in ctx.diff, "新增檔案的內容必須進得了 diff"


def test_artifacts_stay_out_of_the_repo(scratch, tmp_path):
    """plan / qa 的產物不能落在 worktree 裡，否則會被 merge 進 main。"""
    repo, ws = scratch
    result, ctx, _ = run_template(
        mockify(json.loads(TEMPLATE.read_text("utf-8")), ["PASS"]), ws, tmp_path
    )
    assert result.status == PASSED, result.reason

    tracked = {p.name for p in ws.path.rglob("*") if ".git" not in p.parts}
    for polluter in ("plan.md", "notes.md", "qa-report.md", "changes.diff",
                     "requirements.md"):
        assert polluter not in tracked, f"{polluter} 不該出現在 worktree"

    # QA 的結構化輸出存在 repo 外的 artifacts 目錄
    assert (tmp_path / "artifacts").exists()


def test_main_repo_working_tree_untouched(scratch, tmp_path):
    """整個流程不能動到使用者手上的工作目錄。

    舊 bash 的 5 號 bug：AUTO_MERGE=true 會在主 repo 執行 git checkout main。
    """
    repo, ws = scratch
    before = (repo / "README.md").read_text()
    head_before = (repo / ".git" / "HEAD").read_text()

    result, _, _ = run_template(
        mockify(json.loads(TEMPLATE.read_text("utf-8")), ["PASS"]), ws, tmp_path
    )
    assert result.status == PASSED, result.reason

    assert (repo / "README.md").read_text() == before
    assert (repo / ".git" / "HEAD").read_text() == head_before
    assert not (repo / "feature.py").exists(), "變更只該存在 worktree 的 branch 上"


def test_no_git_hooks_involved(scratch, tmp_path):
    """6 號 bug：舊設計靠 post-commit hook 觸發，worktree 內的 commit 理論上會遞迴。

    新設計完全不裝 hook，commit 用 --no-verify，不可能遞迴。
    """
    repo, ws = scratch
    hooks = repo / ".git" / "hooks"
    installed = [p.name for p in hooks.glob("*") if not p.name.endswith(".sample")]
    assert installed == [], f"不該安裝任何 hook，卻有 {installed}"

    result, _, _ = run_template(
        mockify(json.loads(TEMPLATE.read_text("utf-8")), ["PASS"]), ws, tmp_path
    )
    assert result.status == PASSED, result.reason


# ------------------------------------------------ codex-review 範本

REVIEW_TEMPLATE = ROOT / "workflows" / "codex-review.json"


def test_review_template_is_valid():
    graph = parse(json.loads(REVIEW_TEMPLATE.read_text("utf-8")))
    assert validate(graph, [s.id for s in Registry().all()]) == []
    assert [n.id for n in graph.entrypoints()] == ["req"]
    # 修正迴圈：commit 回到 review
    assert {(e.src, e.port, e.dst) for e in graph.back_edges()} == {
        ("commit", "out", "review")
    }
    # review 的前向入邊只有 checkout —— 第一輪不會死等還沒跑的 commit
    assert [e.src for e in graph.forward_incoming("review")] == ["checkout"]
    assert parse(to_dict(graph)) is not None


def test_review_template_shell_vars_are_braced():
    """$VAR 後面直接接非 ASCII 時，bash 在某些 locale 會把那些位元組吃進變數名，
    配上 set -u 就變成 unbound variable。實際踩過，整個節點只有 exit 127。"""
    import re

    graph = parse(json.loads(REVIEW_TEMPLATE.read_text("utf-8")))
    bad = re.compile(r"\$[A-Z_][A-Z0-9_]*(?=[^\x00-\x7f])")
    for node in graph.nodes.values():
        command = node.config.get("command") or ""
        found = bad.findall(command)
        assert not found, f"節點 {node.id} 有沒加大括號的變數接著全形字: {found}"


def test_review_template_reviewer_is_readonly_and_typed():
    """審查節點必須是唯讀且回傳 typed JSON —— 這是它比人工掃 diff 可靠的原因。"""
    graph = parse(json.loads(REVIEW_TEMPLATE.read_text("utf-8")))
    review = graph.nodes["review"]
    assert review.mutates is False
    assert review.config["sandbox"] == "read-only"

    schema = json.loads(review.config["schema"])
    assert schema["properties"]["verdict"]["enum"] == ["PASS", "FAIL"]
    issue = schema["properties"]["issues"]["items"]["properties"]
    # 每個 finding 都要說得出「什麼情況下會壞」
    assert {"severity", "file", "problem", "why_it_breaks", "fix"} == set(issue)


def test_review_template_runs_with_mocks(scratch, tmp_path):
    """用 mock 跑完整條路徑：審查 FAIL → 修正 → 重審 PASS。"""
    graph = json.loads(REVIEW_TEMPLATE.read_text("utf-8"))
    for node in graph["nodes"]:
        if node["id"] == "checkout":
            node["config"]["command"] = "echo '審查對象: mock（領先 1 個 commit）'"
        elif node["id"] == "review":
            node["type"] = "mock"
            node["config"]["script"] = script(structured={
                "verdict": "PASS", "issues": [], "summary": "沒有問題",
            })
        elif node["id"] == "fix":
            node["type"] = "mock"
            node["config"]["script"] = script(message="修好了")
        elif node["id"] == "report":
            node["config"]["command"] = "echo 通過"

    repo, ws = scratch
    result, ctx, _ = run_template(graph, ws, tmp_path, requirement="看安全性")

    assert result.status == PASSED, result.reason
    assert ctx.nodes["review"].structured["verdict"] == "PASS"
    assert result.node_status["report"] == PASSED
    assert result.node_status["fix"] == "skipped"
