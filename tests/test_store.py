"""sqlite 存取層測試，含跨執行緒寫入。"""

from __future__ import annotations

import threading

import pytest

from store.db import Store, new_id


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "test.sqlite")


GRAPH = {
    "name": "測試流程",
    "nodes": [{"id": "a", "type": "mock", "config": {"prompt": "p"}}],
    "edges": [],
}


def test_new_id_is_sortable_and_unique():
    ids = [new_id("run") for _ in range(50)]
    assert len(set(ids)) == 50
    assert all(i.startswith("run-") for i in ids)


def test_workflow_crud(store):
    wf_id = store.save_workflow(GRAPH)
    assert store.get_workflow(wf_id)["name"] == "測試流程"
    assert [w["id"] for w in store.list_workflows()] == [wf_id]

    store.save_workflow({**GRAPH, "id": wf_id, "name": "改名了"})
    assert store.get_workflow(wf_id)["name"] == "改名了"
    assert len(store.list_workflows()) == 1, "同 id 應該是更新不是新增"

    assert store.delete_workflow(wf_id)
    assert store.get_workflow(wf_id) is None
    assert not store.delete_workflow(wf_id)


def test_run_lifecycle(store):
    run_id = store.create_run(GRAPH, "加上月對月比較")
    run = store.get_run(run_id)
    assert run["status"] == "queued"
    assert run["requirement"] == "加上月對月比較"
    assert run["graph"]["name"] == "測試流程"

    store.start_run(run_id, "task/x", "abc123", "/tmp/wt")
    assert store.get_run(run_id)["status"] == "running"
    assert store.unfinished_runs() == [run_id]

    store.finish_run(run_id, "passed", "", 3)
    run = store.get_run(run_id)
    assert run["status"] == "passed"
    assert run["steps"] == 3
    assert run["finished_at"] is not None
    assert store.unfinished_runs() == []


def test_requirement_is_immutable_snapshot(store):
    """舊 bash 的 4 號 bug：需求在背景執行期間被改掉會讀到錯的內容。

    需求在 create_run 時就寫進 run record，之後不提供任何修改途徑。
    """
    run_id = store.create_run(GRAPH, "原始需求")
    store.start_run(run_id, "task/x", "sha", "/wt")
    store.finish_run(run_id, "passed", "", 1)
    assert store.get_run(run_id)["requirement"] == "原始需求"


def test_run_snapshots_graph(store):
    """run 存的是執行當時的圖快照，之後編輯工作流不影響歷史。"""
    wf_id = store.save_workflow(GRAPH)
    run_id = store.create_run(store.get_workflow(wf_id), "需求", workflow_id=wf_id)

    store.save_workflow({**GRAPH, "id": wf_id, "name": "後來改了",
                         "nodes": [{"id": "z", "type": "mock", "config": {"prompt": "p"}}]})

    assert store.get_run(run_id)["graph"]["nodes"][0]["id"] == "a"
    assert store.get_workflow(wf_id)["nodes"][0]["id"] == "z"


def test_node_run_upsert(store):
    run_id = store.create_run(GRAPH, "需求")
    store.save_node_run(run_id, "a", status="running", label="節點 A", visits=1)
    store.save_node_run(
        run_id, "a", status="passed", label="節點 A", visits=2,
        last_message="做完了", structured={"verdict": "PASS"},
        session_id="s1", exit_code=0, files=["x.py"],
        usage={"input_tokens": 10},
    )
    nodes = store.get_node_runs(run_id)
    assert len(nodes) == 1
    node = nodes[0]
    assert node["status"] == "passed"
    assert node["visits"] == 2
    assert node["structured"] == {"verdict": "PASS"}
    assert node["files"] == ["x.py"]
    assert node["usage"] == {"input_tokens": 10}


def test_node_run_null_structured(store):
    run_id = store.create_run(GRAPH, "需求")
    store.save_node_run(run_id, "a", status="passed")
    assert store.get_node_runs(run_id)[0]["structured"] is None


def test_events_append_and_replay(store):
    run_id = store.create_run(GRAPH, "需求")
    events = [
        {"seq": i, "node_id": "a", "ts": 1.0 * i, "kind": "stdout",
         "text": f"line {i}", "data": {"i": i}}
        for i in range(1, 11)
    ]
    store.append_events(run_id, events)

    assert store.event_count(run_id) == 10
    assert len(store.get_events(run_id)) == 10
    # 重連補送：只拿 seq 之後的
    later = store.get_events(run_id, after_seq=7)
    assert [e["seq"] for e in later] == [8, 9, 10]
    assert later[0]["data"] == {"i": 8}


def test_events_duplicate_seq_ignored(store):
    """重連時可能重送同一批事件，不能因為主鍵衝突就整批失敗。"""
    run_id = store.create_run(GRAPH, "需求")
    batch = [{"seq": 1, "kind": "stdout", "text": "a", "data": {}}]
    store.append_events(run_id, batch)
    store.append_events(run_id, batch)
    assert store.event_count(run_id) == 1


def test_events_data_with_non_serialisable_values(store):
    """事件 data 可能夾帶 Path 之類的東西，不能讓整個 run 掛在序列化上。"""
    from pathlib import Path

    run_id = store.create_run(GRAPH, "需求")
    store.append_events(
        run_id,
        [{"seq": 1, "kind": "status", "text": "x", "data": {"p": Path("/tmp/x")}}],
    )
    assert store.get_events(run_id)[0]["data"]["p"] == "/tmp/x"


def test_empty_event_batch_is_noop(store):
    run_id = store.create_run(GRAPH, "需求")
    store.append_events(run_id, [])
    assert store.event_count(run_id) == 0


def test_concurrent_writes_from_threads(store):
    """引擎在背景執行緒寫、Flask 在 request 執行緒讀，連線不能跨執行緒共用。"""
    run_id = store.create_run(GRAPH, "需求")
    errors: list[Exception] = []

    def writer(offset: int) -> None:
        try:
            store.append_events(
                run_id,
                [{"seq": offset * 100 + i, "kind": "stdout", "text": "x", "data": {}}
                 for i in range(50)],
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert store.event_count(run_id) == 200


def test_missing_run_returns_none(store):
    assert store.get_run("nope") is None
