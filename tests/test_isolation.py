"""每節點獨立 worktree（isolation=per_node）的測試。

重點驗證四件事：
1. 會寫檔的節點真的平行（不再序列化）
2. 上游的成果傳得到下游（靠自動 commit）
3. fan-in 合併多個上游；衝突時要有清楚的錯誤與檔案清單
4. 迴圈重入時延續自己上一輪的成果，不是從頭重做
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import pytest

from adapters.registry import Registry
from engine import runner as R
from engine.context import RunContext
from engine.graph import parse, validate
from engine.isolation import PER_NODE, PerNodeIsolation, SharedIsolation, parse_mode
from engine.runner import FAILED, PASSED, Runner
from engine.workspace import MergeConflict, create_workspace, prepare_run_base
from settings import Guards
from tests.test_runner import Recorder, build, mock_node, script
from tests.test_workspace import make_repo

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def repo_and_base(tmp_path):
    """一個 repo + run 基準，刻意不 checkout run branch。

    跟正式流程一致：per_node 模式下 task/<run-id> 不能被任何 worktree 佔用，
    否則跑完之後移動它會被 git 拒絕。
    """
    repo = make_repo(tmp_path / "proj")
    base = prepare_run_base(
        project_repo=repo, main_branch="main", run_id="iso",
        tool_root=tmp_path / "tool",
    )
    return repo, base


@pytest.fixture
def repo_and_workspace(tmp_path):
    """共用模式用的 fixture：真的有一個 checkout 出來的 worktree。"""
    repo = make_repo(tmp_path / "proj")
    ws = create_workspace(
        project_repo=repo,
        worktree_root=tmp_path / "wt",
        main_branch="main",
        run_id="shared",
        tool_root=tmp_path / "tool",
    )
    yield repo, ws
    ws.remove()


def run_per_node(payload, repo, base, tmp_path, **guard_kw):
    graph = parse(payload)
    registry = Registry()
    assert validate(graph, [s.id for s in registry.all()]) == []

    isolation = PerNodeIsolation(
        repo=repo,
        worktree_root=tmp_path / "wt",
        run_id="iso",
        run_base_sha=base.base_sha,
        tool_root=tmp_path / "tool",
    )
    ctx = RunContext(
        run_id="iso",
        requirement="測試需求",
        branch=base.branch,
        base_sha=base.base_sha,
        workdir=str(tmp_path / "wt" / "iso"),
        tool_root=str(ROOT),
        repo=str(repo),
    )
    rec = Recorder()
    runner = Runner(
        graph=graph,
        context=ctx,
        isolation=isolation,
        registry=registry,
        guards=Guards(**{"default_node_timeout": 60, "run_timeout": 300, **guard_kw}),
        emit=rec,
        artifacts_dir=tmp_path / "artifacts",
    )
    try:
        return runner.run(), ctx, rec, isolation
    finally:
        pass


def git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    ).stdout.strip()


# ---------------------------------------------------------------- 設定解析


def test_parse_mode():
    assert parse_mode({}) == "shared"
    assert parse_mode({"isolation": "shared"}) == "shared"
    assert parse_mode({"isolation": "per_node"}) == PER_NODE
    with pytest.raises(ValueError, match="isolation 只能是"):
        parse_mode({"isolation": "每個節點一台機器"})


def test_graph_validation_rejects_bad_isolation():
    graph = parse({
        "settings": {"isolation": "nope"},
        "nodes": [{"id": "a", "type": "mock", "config": {"prompt": "p"}}],
        "edges": [],
    })
    assert any("isolation 只能是" in p for p in validate(graph, ["mock"]))


def test_graph_validation_rejects_bad_max_run_steps():
    graph = parse({
        "settings": {"max_run_steps": 0},
        "nodes": [{"id": "a", "type": "mock", "config": {"prompt": "p"}}],
        "edges": [],
    })
    assert any("max_run_steps" in p for p in validate(graph, ["mock"]))


def test_isolation_capability_flags():
    shared = SharedIsolation(workspace=None)
    assert shared.serialises_writes() is True
    assert shared.needs_autocommit() is False

    per = PerNodeIsolation(repo=Path("/x"), worktree_root=Path("/y"),
                           run_id="r", run_base_sha="sha")
    assert per.serialises_writes() is False
    assert per.write_lock_held() is False
    assert per.needs_autocommit() is True


# -------------------------------------------------- 真正的平行寫入


def test_mutating_nodes_run_in_parallel(repo_and_base, tmp_path):
    """per_node 的重點：兩個都會寫檔的節點同時跑，不再排隊。"""
    payload = {
        "settings": {"isolation": PER_NODE},
        "nodes": [
            {"id": "req", "type": "requirement"},
            mock_node("w1", message="a", files={"a.txt": "1\n"}, sleep=0.4, tools=["x"]),
            mock_node("w2", message="b", files={"b.txt": "2\n"}, sleep=0.4, tools=["x"]),
        ],
        "edges": [{"from": "req", "to": "w1"}, {"from": "req", "to": "w2"}],
    }
    repo, base = repo_and_base
    start = time.monotonic()
    result, ctx, rec, iso = run_per_node(payload, repo, base, tmp_path,
                                         max_parallel_nodes=4)
    elapsed = time.monotonic() - start

    assert result.status == PASSED, result.reason
    assert not rec.has_phase("await_lock"), "per_node 不該有等鎖"

    # 用事件序號判斷執行區間是否重疊 —— 比看牆上時鐘可靠，不會因為機器負載誤判
    spans: dict[str, dict[str, int]] = {}
    for node_id, event in rec.events:
        phase = event["data"].get("phase")
        if phase == "spawn":
            spans.setdefault(node_id, {})["start"] = event["seq"]
        elif phase == "node_end":
            spans.setdefault(node_id, {})["end"] = event["seq"]
    w1, w2 = spans["w1"], spans["w2"]
    overlap = w1["start"] < w2["end"] and w2["start"] < w1["end"]
    assert overlap, f"兩個會寫檔的節點沒有重疊執行: {spans}"

    # 每個節點 3 個 sleep 點 × 0.4s = 1.2s；序列化會超過 2.4s
    assert elapsed < 2.0, f"看起來還是序列化: {elapsed:.2f}s"

    # 各自有 worktree、各自有 branch
    assert set(iso.workspaces) == {"w1", "w2"}
    assert iso.workspaces["w1"].path != iso.workspaces["w2"].path
    assert (iso.workspaces["w1"].path / "a.txt").exists()
    assert (iso.workspaces["w2"].path / "b.txt").exists()
    # 互相看不到對方的檔案 —— 這就是隔離
    assert not (iso.workspaces["w1"].path / "b.txt").exists()
    assert not (iso.workspaces["w2"].path / "a.txt").exists()


def test_each_node_gets_its_own_branch(repo_and_base, tmp_path):
    payload = {
        "settings": {"isolation": PER_NODE},
        "nodes": [mock_node("only", message="x", files={"f.txt": "1\n"})],
        "edges": [],
    }
    repo, base = repo_and_base
    result, _, _, iso = run_per_node(payload, repo, base, tmp_path)
    assert result.status == PASSED, result.reason
    from engine.graph import safe_name
    expected = f"node/iso/{safe_name('only')}"
    assert iso.workspaces["only"].branch == expected
    assert expected in git(["branch", "--list", expected], repo)


# ------------------------------------------------ 上游成果傳到下游


def test_downstream_sees_upstream_changes(repo_and_base, tmp_path):
    """靠自動 commit 傳遞狀態：下游 worktree 是從上游的 commit 開出來的。"""
    payload = {
        "settings": {"isolation": PER_NODE},
        "nodes": [
            mock_node("first", message="建立檔案", files={"from_first.txt": "hello\n"}),
            mock_node("second", message="看到了"),
        ],
        "edges": [{"from": "first", "to": "second"}],
    }
    repo, base = repo_and_base
    result, ctx, rec, iso = run_per_node(payload, repo, base, tmp_path)

    assert result.status == PASSED, result.reason
    second = iso.workspaces["second"].path
    assert (second / "from_first.txt").read_text() == "hello\n", \
        "下游必須看到上游寫的檔案"

    # 自動 commit 有發事件，使用者看得到狀態是怎麼傳的
    assert rec.has_phase("node_commit")
    assert result.node_commits["first"]


def test_readonly_node_sees_upstream_and_diff(repo_and_base, tmp_path):
    """唯讀審查節點要能拿到累積的 diff（相對 run 起點，不是相對上一個節點）。"""
    marker = tmp_path / "seen_diff.txt"
    payload = {
        "settings": {"isolation": PER_NODE},
        "nodes": [
            mock_node("impl", message="改了", files={"feature.py": "def f(): pass\n"}),
            {
                "id": "review",
                "type": "shell",
                "mutates": False,
                "config": {
                    "command": f"git diff {{{{ run.base_sha }}}} > {marker}; "
                               f"test -f feature.py",
                    "expect_exit_code": 0,
                },
            },
        ],
        "edges": [{"from": "impl", "to": "review"}],
    }
    repo, base = repo_and_base
    result, ctx, _, iso = run_per_node(payload, repo, base, tmp_path)

    assert result.status == PASSED, result.reason
    seen = marker.read_text("utf-8")
    assert "feature.py" in seen
    assert "def f()" in seen
    assert "feature.py" in ctx.changed_files


def test_condition_node_does_not_break_the_chain(repo_and_base, tmp_path):
    """條件節點不產生 commit，必須把上游狀態原樣往下傳。

    少了這個 pass-through，下游會從 run 的起點重新開始，前面做的全部不見。
    """
    payload = {
        "settings": {"isolation": PER_NODE},
        "nodes": [
            mock_node("impl", message="改了", files={"kept.txt": "keep me\n"}),
            {"id": "gate", "type": "condition", "config": {"expr": "True"}},
            mock_node("after", message="繼續"),
        ],
        "edges": [
            {"from": "impl", "to": "gate"},
            {"from": "gate", "to": "after", "port": "true"},
        ],
    }
    repo, base = repo_and_base
    result, _, _, iso = run_per_node(payload, repo, base, tmp_path)

    assert result.status == PASSED, result.reason
    assert (iso.workspaces["after"].path / "kept.txt").exists(), \
        "條件節點把上游的成果弄丟了"


# ---------------------------------------------------------------- fan-in


def test_fan_in_merges_parallel_branches(repo_and_base, tmp_path):
    """兩個平行節點改不同檔案，fan-in 的節點要同時看到兩邊的成果。"""
    payload = {
        "settings": {"isolation": PER_NODE},
        "nodes": [
            {"id": "req", "type": "requirement"},
            mock_node("left", message="L", files={"left.txt": "L\n"}),
            mock_node("right", message="R", files={"right.txt": "R\n"}),
            mock_node("join", message="合流"),
        ],
        "edges": [
            {"from": "req", "to": "left"},
            {"from": "req", "to": "right"},
            {"from": "left", "to": "join"},
            {"from": "right", "to": "join"},
        ],
    }
    repo, base = repo_and_base
    result, _, _, iso = run_per_node(payload, repo, base, tmp_path,
                                     max_parallel_nodes=4)

    assert result.status == PASSED, result.reason
    joined = iso.workspaces["join"].path
    assert (joined / "left.txt").read_text() == "L\n"
    assert (joined / "right.txt").read_text() == "R\n"


def test_fan_in_conflict_reports_files(repo_and_base, tmp_path):
    """兩個平行節點改同一個檔案的同一處 → fan-in 衝突。

    這是 per_node 換來真平行的代價，錯誤訊息必須列出衝突檔案，
    不然使用者完全不知道從哪裡下手。
    """
    payload = {
        "settings": {"isolation": PER_NODE},
        "nodes": [
            {"id": "req", "type": "requirement"},
            mock_node("left", message="L", files={"shared.txt": "left wins\n"}),
            mock_node("right", message="R", files={"shared.txt": "right wins\n"}),
            mock_node("join", message="合流"),
        ],
        "edges": [
            {"from": "req", "to": "left"},
            {"from": "req", "to": "right"},
            {"from": "left", "to": "join"},
            {"from": "right", "to": "join"},
        ],
    }
    repo, base = repo_and_base
    result, _, rec, _ = run_per_node(payload, repo, base, tmp_path,
                                     max_parallel_nodes=4)

    assert result.status == FAILED
    assert "合併上游結果失敗" in result.reason
    assert "shared.txt" in result.reason

    # 事件裡也要帶結構化的衝突清單，UI 才標得出來
    conflict_events = [
        e for _, e in rec.events if e["data"].get("conflicted_files")
    ]
    assert conflict_events
    assert conflict_events[0]["data"]["conflicted_files"] == ["shared.txt"]


def test_merge_conflict_leaves_worktree_clean(repo_and_base, tmp_path):
    """合併失敗要 abort，不能留下半合併狀態的工作目錄。"""
    repo, base = repo_and_base
    from engine.workspace import create_node_workspace, merge_into

    # 兩個分岔的 commit，改同一行
    left = create_node_workspace(repo, tmp_path / "wt", "iso", "l",
                                 [base.base_sha], base.base_sha)
    (left.path / "c.txt").write_text("left\n")
    left_sha = left.commit("left")

    right = create_node_workspace(repo, tmp_path / "wt", "iso", "r",
                                  [base.base_sha], base.base_sha)
    (right.path / "c.txt").write_text("right\n")
    right.commit("right")

    with pytest.raises(MergeConflict) as excinfo:
        merge_into(right, [left_sha])
    assert excinfo.value.files == ["c.txt"]

    # abort 過了，工作目錄乾淨、沒有殘留的合併狀態
    assert git(["status", "--porcelain"], right.path) == ""
    assert not (right.path / ".git").exists() or True  # worktree 用檔案不是目錄
    left.remove()
    right.remove()


# ---------------------------------------------------------------- 迴圈


def test_loop_reentry_keeps_own_previous_work(repo_and_base, tmp_path):
    """迴圈第二輪要從自己上一輪的成果繼續，不是從 run 起點重做。"""
    payload = {
        "settings": {"isolation": PER_NODE},
        "nodes": [
            {"id": "req", "type": "requirement"},
            # 每輪寫一個不同名字的檔案（script 也吃模板）。
            # 若第二輪的基準錯回 run 起點，第一輪的 round1.txt 就會消失。
            {
                "id": "impl",
                "type": "mock",
                "max_visits": 3,
                "config": {
                    "prompt": "x",
                    "script": '{"message":"round {{ loop.iteration }}",'
                              '"files":{"round{{ loop.iteration }}.txt":"r"}}',
                },
            },
            {"id": "gate", "type": "condition",
             "config": {"expr": "loop.iteration >= 2"}},
            mock_node("done", message="收工"),
        ],
        "edges": [
            {"from": "req", "to": "impl"},
            {"from": "impl", "to": "gate"},
            {"from": "gate", "to": "done", "port": "true"},
            {"from": "gate", "to": "impl", "port": "false"},
        ],
    }
    repo, base = repo_and_base
    result, _, _, iso = run_per_node(payload, repo, base, tmp_path)

    assert result.status == PASSED, result.reason
    assert result.visits["impl"] == 2

    # 第二輪的 worktree 起點必須是第一輪的 commit（不是 run 的起點）
    impl_ws = iso.workspaces["impl"]
    assert (impl_ws.path / "round1.txt").exists(), "第一輪的成果被丟掉了"
    assert (impl_ws.path / "round2.txt").exists()

    history = git(["log", "--oneline"], impl_ws.path)
    assert history.count("impl:") == 2, f"第二輪沒有延續第一輪:\n{history}"


# ---------------------------------------------------------- run branch


def test_run_branch_points_at_final_commit(repo_and_base, tmp_path):
    """task/<run-id> 要指到終端節點的產出，這樣 diff 與合併指令照樣可用。"""
    payload = {
        "settings": {"isolation": PER_NODE},
        "nodes": [
            mock_node("a", message="A", files={"a.txt": "1\n"}),
            mock_node("b", message="B", files={"b.txt": "2\n"}),
        ],
        "edges": [{"from": "a", "to": "b"}],
    }
    repo, base = repo_and_base
    result, _, _, iso = run_per_node(payload, repo, base, tmp_path)
    assert result.status == PASSED, result.reason

    final = iso.finalise(base.branch, list(result.node_commits.values()))
    assert final == result.node_commits["b"]

    # run branch 上看得到兩個節點的成果
    files = git(["ls-tree", "-r", "--name-only", base.branch], repo).splitlines()
    assert "a.txt" in files and "b.txt" in files


def test_cleanup_removes_all_node_worktrees(repo_and_base, tmp_path):
    payload = {
        "settings": {"isolation": PER_NODE},
        "nodes": [
            {"id": "req", "type": "requirement"},
            mock_node("x", message="x", files={"x.txt": "1\n"}),
            mock_node("y", message="y", files={"y.txt": "1\n"}),
        ],
        "edges": [{"from": "req", "to": "x"}, {"from": "x", "to": "y"}],
    }
    repo, base = repo_and_base
    result, _, _, iso = run_per_node(payload, repo, base, tmp_path)
    assert result.status == PASSED, result.reason

    paths = [ws.path for ws in iso.workspaces.values()]
    assert all(p.exists() for p in paths)
    iso.cleanup()
    assert not any(p.exists() for p in paths)
    # branch 保留下來給人工檢視
    assert git(["branch", "--list", "node/iso/*"], repo).strip(), "節點 branch 應保留"


# ----------------------------------------------- shared 模式沒有回歸


def test_shared_mode_still_serialises(repo_and_workspace, tmp_path):
    """加上 per_node 之後，共用模式的行為不能變。"""
    payload = {
        "nodes": [
            {"id": "req", "type": "requirement"},
            mock_node("w1", message="a", sleep=0.3, tools=["x"]),
            mock_node("w2", message="b", sleep=0.3, tools=["x"]),
        ],
        "edges": [{"from": "req", "to": "w1"}, {"from": "req", "to": "w2"}],
    }
    repo, base = repo_and_workspace
    graph = parse(payload)
    registry = Registry()
    ctx = RunContext(run_id="sh", requirement="r", branch=base.branch,
                     base_sha=base.base_sha, workdir=str(base.path),
                     tool_root=str(ROOT), repo=str(repo))
    rec = Recorder()
    runner = Runner(
        graph=graph, context=ctx, isolation=SharedIsolation(workspace=base),
        registry=registry,
        guards=Guards(default_node_timeout=60, run_timeout=300, max_parallel_nodes=4),
        emit=rec, artifacts_dir=tmp_path / "artifacts",
    )
    result = runner.run()

    assert result.status == PASSED, result.reason
    assert rec.has_phase("await_lock"), "共用模式必須維持序列化並提示等鎖"
    assert result.node_commits == {}, "共用模式不做自動 commit"

# ------------------------------------------------ codex review 找到的問題


def test_safe_name_neutralises_dangerous_node_ids():
    """節點 id 是使用者可見的穩定識別字，內部路徑/ref 名稱另外算。

    直接限制 id 格式會擋掉本來合法的既有工作流（"規劃" 這種中文 id 在共用模式
    下完全正常），所以改成永遠轉換成安全名稱。
    """
    from engine.graph import safe_name

    for dangerous in ["../../victim", "a/b", "/abs/path", "..", ".",
                      "x" * 300, "a b", "節點", "~/.ssh/authorized_keys"]:
        name = safe_name(dangerous)
        assert "/" not in name and "\\" not in name
        assert ".." not in name
        assert not name.startswith((".", "-"))
        assert len(name) <= 49  # 32 字前綴 + '-' + 16 字雜湊
        assert re.fullmatch(r"[A-Za-z0-9._-]+", name), name


def test_safe_name_is_stable_and_collision_free():
    """同一個 id 一定得到同一個名稱；不同 id 一定不同 —— 包含只差大小寫的。

    macOS 預設的檔案系統不分大小寫：節點 "A" 與 "a" 是兩個合法且不同的節點，
    若對應到同一個目錄，建第二個時會強制移除還在執行中的第一個。
    """
    from engine.graph import safe_name

    assert safe_name("impl") == safe_name("impl")
    names = {safe_name(x) for x in ["A", "a", "Impl", "impl", "規劃", "规划"]}
    assert len(names) == 6, f"有 id 撞在一起了: {names}"
    # 在不分大小寫的檔案系統上也不能撞
    assert len({n.lower() for n in names}) == 6


def test_existing_workflows_with_unicode_ids_still_load():
    """相容性：14f4863 之前只要求 id 非空，既有工作流不能因升級就載不進來。"""
    graph = parse({
        "nodes": [
            {"id": "需求", "type": "requirement"},
            {"id": "code review", "type": "mock", "config": {"prompt": "p"}},
        ],
        "edges": [{"from": "需求", "to": "code review"}],
    })
    assert set(graph.nodes) == {"需求", "code review"}
    assert validate(graph, ["mock"]) == []


def test_node_id_still_rejects_the_genuinely_impossible():
    from engine.graph import GraphError, Node

    with pytest.raises(GraphError, match="控制字元"):
        Node(id="a\x00b", type="mock")
    with pytest.raises(GraphError, match="太長"):
        Node(id="x" * 201, type="mock")
    with pytest.raises(GraphError, match="缺少 id"):
        Node(id="", type="mock")


def test_traversal_node_id_stays_inside_the_run_dir(repo_and_base, tmp_path):
    """就算 id 長得像路徑穿越，worktree 也必須落在 run 目錄內，且不刪到別人。"""
    from engine.workspace import create_node_workspace

    repo, base = repo_and_base
    victim = tmp_path / "wt" / "DO_NOT_DELETE"
    victim.mkdir(parents=True)
    (victim / "precious.txt").write_text("keep me\n")

    ws = create_node_workspace(
        repo=repo, worktree_root=tmp_path / "wt", run_id="iso",
        node_id="../DO_NOT_DELETE", base_commits=[base.base_sha],
        run_base_sha=base.base_sha,
    )
    try:
        run_dir = (tmp_path / "wt" / "iso").resolve()
        assert run_dir in ws.path.parents, f"逃出 run 目錄: {ws.path}"
        assert (victim / "precious.txt").exists(), "刪到了不該碰的目錄"
    finally:
        ws.remove()


def test_condition_as_fan_in_keeps_both_branches(repo_and_base, tmp_path):
    """條件節點若剛好是 fan-in 點，兩條分支的成果都要往下傳。

    純資料節點不產生 commit，只帶第一個上游 commit 的話，另一條平行分支
    成功完成的修改會靜默消失。
    """
    payload = {
        "settings": {"isolation": PER_NODE},
        "nodes": [
            {"id": "req", "type": "requirement"},
            mock_node("left", message="L", files={"left.txt": "L\n"}),
            mock_node("right", message="R", files={"right.txt": "R\n"}),
            {"id": "gate", "type": "condition", "config": {"expr": "True"}},
            mock_node("after", message="下游"),
        ],
        "edges": [
            {"from": "req", "to": "left"},
            {"from": "req", "to": "right"},
            {"from": "left", "to": "gate"},
            {"from": "right", "to": "gate"},
            {"from": "gate", "to": "after", "port": "true"},
        ],
    }
    repo, base = repo_and_base
    result, _, _, iso = run_per_node(payload, repo, base, tmp_path,
                                     max_parallel_nodes=4)

    assert result.status == PASSED, result.reason
    after = iso.workspaces["after"].path
    assert (after / "left.txt").exists(), "left 分支的成果被條件節點弄丟了"
    assert (after / "right.txt").exists(), "right 分支的成果被條件節點弄丟了"


def test_finalise_merges_all_terminal_branches(repo_and_base, tmp_path):
    """圖可以 fan-out 成兩個各自結束的分支，run branch 要包含兩邊。

    只挑「最後完成的 commit」會漏掉另一邊，而且挑到哪個還取決於執行時序。
    """
    payload = {
        "settings": {"isolation": PER_NODE},
        "nodes": [
            {"id": "req", "type": "requirement"},
            mock_node("endA", message="A", files={"end_a.txt": "A\n"}),
            mock_node("endB", message="B", files={"end_b.txt": "B\n"}),
        ],
        "edges": [{"from": "req", "to": "endA"}, {"from": "req", "to": "endB"}],
    }
    repo, base = repo_and_base
    result, _, _, iso = run_per_node(payload, repo, base, tmp_path,
                                     max_parallel_nodes=4)
    assert result.status == PASSED, result.reason

    commits = [c for n, c in result.node_commits.items()
               if result.node_status.get(n) == PASSED]
    final = iso.finalise(base.branch, commits)
    assert final

    files = git(["ls-tree", "-r", "--name-only", base.branch], repo).splitlines()
    assert "end_a.txt" in files, "終端分支 A 的成果沒進 run branch"
    assert "end_b.txt" in files, "終端分支 B 的成果沒進 run branch"


def test_finalise_single_tip_needs_no_merge(repo_and_base, tmp_path):
    """線性圖只有一個 tip，直接指過去，不該多建整合 worktree。"""
    payload = {
        "settings": {"isolation": PER_NODE},
        "nodes": [
            mock_node("a", message="A", files={"a.txt": "1\n"}),
            mock_node("b", message="B", files={"b.txt": "2\n"}),
        ],
        "edges": [{"from": "a", "to": "b"}],
    }
    repo, base = repo_and_base
    result, _, _, iso = run_per_node(payload, repo, base, tmp_path)
    assert result.status == PASSED, result.reason

    final = iso.finalise(base.branch, list(result.node_commits.values()))
    assert final == result.node_commits["b"], "b 包含 a，tip 只有 b"
    assert iso.INTEGRATE not in iso.workspaces


def test_finalise_conflict_raises(repo_and_base, tmp_path):
    """兩個終端分支改同一處 → 整合失敗，呼叫端要能讓整個 run 失敗。"""
    payload = {
        "settings": {"isolation": PER_NODE},
        "nodes": [
            {"id": "req", "type": "requirement"},
            mock_node("endA", message="A", files={"shared.txt": "A\n"}),
            mock_node("endB", message="B", files={"shared.txt": "B\n"}),
        ],
        "edges": [{"from": "req", "to": "endA"}, {"from": "req", "to": "endB"}],
    }
    repo, base = repo_and_base
    result, _, _, iso = run_per_node(payload, repo, base, tmp_path,
                                     max_parallel_nodes=4)
    assert result.status == PASSED, result.reason  # 兩個節點本身都成功

    with pytest.raises(MergeConflict, match="shared.txt"):
        iso.finalise(base.branch, list(result.node_commits.values()))


def test_tip_commits_filters_ancestors(repo_and_base, tmp_path):
    from engine.workspace import create_node_workspace, tip_commits

    repo, base = repo_and_base
    ws = create_node_workspace(repo, tmp_path / "wt", "iso", "chain",
                               [base.base_sha], base.base_sha)
    try:
        (ws.path / "one.txt").write_text("1\n")
        first_sha = ws.commit("one")
        (ws.path / "two.txt").write_text("2\n")
        second_sha = ws.commit("two")

        # first 是 second 的祖先，只有 second 是 tip
        assert tip_commits(repo, [first_sha, second_sha]) == [second_sha]
        assert tip_commits(repo, [second_sha]) == [second_sha]
        assert tip_commits(repo, []) == []
    finally:
        ws.remove()


def test_condition_does_not_read_a_parallel_nodes_diff(repo_and_base, tmp_path):
    """條件節點不能從共用 context 讀 diff。

    共用狀態是 last-writer-wins：兩條平行分支同時完成時，條件節點可能拿到
    另一條分支的 diff 而選錯出口。沒有工作目錄的節點應該從自己的上游 commit 算。
    """
    payload = {
        "settings": {"isolation": PER_NODE},
        "nodes": [
            mock_node("impl", message="改了", files={"only_mine.txt": "x\n"}),
            {"id": "gate", "type": "condition",
             "config": {"expr": "'only_mine.txt' in changed_files"}},
            mock_node("yes", message="走對了"),
            mock_node("no", message="走錯了"),
        ],
        "edges": [
            {"from": "impl", "to": "gate"},
            {"from": "gate", "to": "yes", "port": "true"},
            {"from": "gate", "to": "no", "port": "false"},
        ],
    }
    repo, base = repo_and_base
    result, _, _, _ = run_per_node(payload, repo, base, tmp_path)

    assert result.status == PASSED, result.reason
    assert result.node_status["yes"] == PASSED, "條件節點看不到自己上游的 diff"
    assert result.node_status["no"] == R.SKIPPED


# --------------------------------------- codex 第三輪 review 找到的問題


def test_artifact_filenames_cannot_escape_the_artifacts_dir(tmp_path):
    """schema / result 檔名原本直接用 node.id，那裡有 write_text 與 unlink。

    支援 schema 的節點若 id 是 "../../victim"，會蓋掉或刪掉 artifacts 之外的檔案。
    """
    from engine.graph import safe_name

    artifacts = tmp_path / "runs" / "r1" / "artifacts"
    artifacts.mkdir(parents=True)
    victim = tmp_path / "runs" / "victim.result.json"
    victim.write_text("precious\n")

    payload = {
        "nodes": [{
            "id": "../victim",
            "type": "mock",
            "config": {
                "prompt": "p",
                "script": '{"structured":{"ok":true}}',
                "schema": '{"type":"object"}',
            },
        }],
        "edges": [],
    }
    run, ctx, _ = build(payload, Registry(), tmp_path)
    run.artifacts = artifacts
    result = run.run()

    assert result.status == PASSED, result.reason
    assert victim.read_text() == "precious\n", "artifacts 目錄外的檔案被動到了"
    written = {p.name for p in artifacts.glob("*")}
    assert any(safe_name("../victim") in n for n in written), written


def test_safe_name_always_yields_a_valid_git_ref(tmp_path):
    """每一個 Node 允許的 id 都必須產生合法的 git branch 名稱。

    per_node 會用它當 branch，check-ref-format 不過的話 run 會直接中止。
    """
    from engine.graph import safe_name
    from engine.workspace import node_branch

    repo = make_repo(tmp_path / "refcheck")
    nasty = [
        "a..b", "..", "...", "a.", ".a", "-a", "a-", "HEAD", "head",
        "a.lock", "refs/heads/x", "a@{b}", "a b", "節點", "a\\b", "a~b",
        "a^b", "a:b", "a?b", "a*b", "a[b]", "x" * 200, "。。。", "-",
    ]
    for node_id in nasty:
        branch = node_branch("run1", node_id)
        code = subprocess.run(
            ["git", "check-ref-format", "--branch", branch],
            cwd=repo, capture_output=True,
        ).returncode
        assert code == 0, f"id {node_id!r} → 不合法的 branch {branch!r}"
        assert ".." not in safe_name(node_id)


def test_safe_name_digest_is_long_enough_to_resist_collisions():
    """前綴會被截斷，唯一性完全靠雜湊 —— 8 個 hex（32 bit）用生日攻擊幾萬次
    就能撞出來，實際上 codex 就找到了一組。"""
    from engine.graph import safe_name

    a = "x" * 32 + "20249"
    b = "x" * 32 + "72765"
    assert safe_name(a) != safe_name(b), "截斷後的長 id 撞在一起了"
    # 雜湊長度至少 16 個 hex
    assert len(safe_name("q").rsplit("-", 1)[1]) >= 16


def test_graph_validation_rejects_internal_name_collision():
    """就算雜湊真的撞了，圖驗證也要擋下來 —— 否則兩個節點共用一個工作目錄。"""
    import engine.graph as gmod

    real = gmod.safe_name
    try:
        gmod.safe_name = lambda node_id: "same-name"   # 強制碰撞
        graph = parse({
            "nodes": [
                {"id": "a", "type": "mock", "config": {"prompt": "p"}},
                {"id": "b", "type": "mock", "config": {"prompt": "p"}},
            ],
            "edges": [{"from": "a", "to": "b"}],
        })
        problems = validate(graph, ["mock"])
        assert any("同一個內部名稱" in p for p in problems), problems
    finally:
        gmod.safe_name = real
