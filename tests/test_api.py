"""Flask API 與 SSE 串流測試。

用真的 mock 工作流跑完整條路徑：POST /api/runs → 背景執行緒 → SSE 事件 →
落進 sqlite → 重連補送。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
import yaml

from tests.test_workspace import make_repo

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def app_ctx(tmp_path):
    """一個指向臨時 repo 的 app，設定檔也是臨時的。"""
    repo = make_repo(tmp_path / "proj")
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "project_repo": str(repo),
                "main_branch": "main",
                "worktree_root": str(tmp_path / "wt"),
                "runs_dir": str(tmp_path / "runs"),
                "database": str(tmp_path / "t.sqlite"),
                "cleanup_worktree_on_success": True,
                "guards": {
                    "default_node_timeout": 60,
                    "run_timeout": 180,
                    "default_max_visits": 3,
                    "max_parallel_nodes": 4,
                },
            }
        ),
        "utf-8",
    )
    from app import create_app

    app = create_app(config)
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client, app, repo


def mock_graph(**script) -> dict:
    return {
        "id": "t-wf",
        "name": "測試流程",
        "nodes": [
            {"id": "req", "type": "requirement"},
            {
                "id": "a",
                "type": "mock",
                "label": "節點 A",
                "config": {
                    "prompt": "需求：{{ requirement }}",
                    "script": json.dumps(script or {"message": "完成"}),
                },
            },
        ],
        "edges": [{"from": "req", "to": "a"}],
    }


def wait_for(client, run_id, timeout=60):
    """等 run 結束，回傳最終狀態。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/runs/{run_id}").get_json()
        if body["status"] not in ("queued", "running"):
            return body
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} 沒有在 {timeout}s 內結束")


# ------------------------------------------------------------ 基本端點


def test_health_reports_repo_and_adapters(app_ctx):
    client, _, repo = app_ctx
    body = client.get("/api/health").get_json()
    assert body["project_repo"]["ok"] is True
    assert body["project_repo"]["path"] == str(repo)
    assert body["adapters_installed"]["mock"] is True


def test_adapters_endpoint_drives_ui_forms(app_ctx):
    client, _, _ = app_ctx
    body = client.get("/api/adapters").get_json()

    by_id = {a["id"]: a for a in body["adapters"]}
    assert {"codex", "claude", "opencode", "shell", "mock"} <= set(by_id)

    codex = by_id["codex"]
    sandbox = next(f for f in codex["fields"] if f["name"] == "sandbox")
    assert sandbox["type"] == "select"
    assert "read-only" in sandbox["options"]
    assert codex["supports_schema"] is True

    builtins = {b["id"]: b for b in body["builtins"]}
    assert {"requirement", "condition", "git"} == set(builtins)
    assert builtins["condition"]["fields"][0]["name"] == "expr"


def test_pages_render(app_ctx):
    client, _, _ = app_ctx
    for path in ("/", "/runs", "/runs/anything"):
        assert client.get(path).status_code == 200


# --------------------------------------------------------------- 工作流


def test_shipped_template_is_seeded(app_ctx):
    client, _, _ = app_ctx
    ids = [w["id"] for w in client.get("/api/workflows").get_json()["workflows"]]
    assert "plan-impl-qa" in ids

    wf = client.get("/api/workflows/plan-impl-qa").get_json()
    assert any(n["id"] == "gate" for n in wf["nodes"])


def test_workflow_save_get_delete(app_ctx):
    client, _, _ = app_ctx
    saved = client.post("/api/workflows", json=mock_graph()).get_json()
    assert saved["problems"] == []

    wf = client.get(f"/api/workflows/{saved['id']}").get_json()
    assert wf["name"] == "測試流程"

    assert client.delete(f"/api/workflows/{saved['id']}").status_code == 200
    assert client.get(f"/api/workflows/{saved['id']}").status_code == 404
    assert client.delete(f"/api/workflows/{saved['id']}").status_code == 404


def test_workflow_save_rejects_malformed(app_ctx):
    client, _, _ = app_ctx
    resp = client.post("/api/workflows", json={"nodes": []})
    assert resp.status_code == 400
    assert "至少要有一個節點" in resp.get_json()["error"]


def test_validate_endpoint_lists_all_problems(app_ctx):
    client, _, _ = app_ctx
    bad = {
        "name": "壞的",
        "nodes": [
            {"id": "a", "type": "does-not-exist", "config": {}},
            {"id": "b", "type": "shell", "config": {}},
        ],
        "edges": [],
    }
    body = client.post("/api/workflows/validate", json=bad).get_json()
    assert body["ok"] is False
    assert len(body["problems"]) >= 2


def test_validate_endpoint_accepts_good_graph(app_ctx):
    client, _, _ = app_ctx
    body = client.post("/api/workflows/validate", json=mock_graph()).get_json()
    assert body == {"ok": True, "problems": []}


# ------------------------------------------------------------------ run


def test_run_end_to_end(app_ctx):
    client, _, repo = app_ctx
    resp = client.post(
        "/api/runs",
        json={"graph": mock_graph(message="做完了", files={"new.py": "x=1\n"}),
              "requirement": "加上新模組"},
    )
    assert resp.status_code == 201
    run_id = resp.get_json()["run_id"]

    final = wait_for(client, run_id)
    assert final["status"] == "passed", final["reason"]
    assert final["requirement"] == "加上新模組"
    assert final["branch"] == f"task/{run_id}"

    nodes = {n["node_id"]: n for n in final["nodes"]}
    assert nodes["a"]["status"] == "passed"
    assert nodes["a"]["last_message"] == "做完了"
    assert nodes["a"]["files"] == ["new.py"]
    assert nodes["a"]["usage"]["input_tokens"] == 10

    # 主 repo 的工作目錄沒被動到
    assert not (repo / "new.py").exists()

    import subprocess

    branches = subprocess.run(
        ["git", "branch", "--list", f"task/{run_id}"],
        cwd=repo, capture_output=True, text=True,
    ).stdout
    assert f"task/{run_id}" in branches, "branch 要留著給人工檢查與合併"


def test_run_rejects_invalid_graph(app_ctx):
    client, _, _ = app_ctx
    resp = client.post(
        "/api/runs",
        json={"graph": {"name": "x",
                        "nodes": [{"id": "a", "type": "ghost", "config": {}}],
                        "edges": []},
              "requirement": "r"},
    )
    assert resp.status_code == 400
    assert "未知的型別" in resp.get_json()["error"]


def test_run_from_workflow_id(app_ctx):
    client, _, _ = app_ctx
    wf_id = client.post("/api/workflows", json=mock_graph()).get_json()["id"]
    resp = client.post("/api/runs", json={"workflow_id": wf_id, "requirement": "r"})
    assert resp.status_code == 201
    assert wait_for(client, resp.get_json()["run_id"])["status"] == "passed"


def test_run_requires_graph_or_workflow(app_ctx):
    client, _, _ = app_ctx
    assert client.post("/api/runs", json={"requirement": "r"}).status_code == 400
    assert client.post(
        "/api/runs", json={"workflow_id": "nope", "requirement": "r"}
    ).status_code == 404


def test_failed_run_records_reason(app_ctx):
    client, _, _ = app_ctx
    run_id = client.post(
        "/api/runs",
        json={"graph": mock_graph(message="x", exit_code=2), "requirement": "r"},
    ).get_json()["run_id"]

    final = wait_for(client, run_id)
    assert final["status"] == "failed"
    assert "exit code 2" in final["reason"]


def test_cancel_run(app_ctx):
    client, _, _ = app_ctx
    run_id = client.post(
        "/api/runs",
        json={"graph": mock_graph(message="x", sleep=3, tools=["a", "b", "c"]),
              "requirement": "r"},
    ).get_json()["run_id"]

    deadline = time.time() + 15
    while time.time() < deadline:
        if client.get(f"/api/runs/{run_id}").get_json()["status"] == "running":
            break
        time.sleep(0.05)

    assert client.post(f"/api/runs/{run_id}/cancel").get_json()["ok"] is True
    assert wait_for(client, run_id)["status"] == "cancelled"
    assert client.post(f"/api/runs/{run_id}/cancel").status_code == 409


def test_cancel_unknown_run(app_ctx):
    client, _, _ = app_ctx
    assert client.post("/api/runs/nope/cancel").status_code == 404


def test_list_runs(app_ctx):
    client, _, _ = app_ctx
    for _ in range(3):
        rid = client.post(
            "/api/runs", json={"graph": mock_graph(), "requirement": "r"}
        ).get_json()["run_id"]
        wait_for(client, rid)

    runs = client.get("/api/runs").get_json()["runs"]
    assert len(runs) == 3
    assert all("active" in r for r in runs)


# ------------------------------------------------------------------ SSE


def read_sse(client, run_id, after=None, max_wait=60):
    """讀完整條 SSE 串流，回傳 (events, done_payload)。"""
    url = f"/api/runs/{run_id}/events" + (f"?after={after}" if after else "")
    resp = client.get(url, headers={"Accept": "text/event-stream"})
    assert resp.headers["Content-Type"].startswith("text/event-stream")
    assert resp.headers["Cache-Control"].startswith("no-cache")
    assert resp.headers["X-Accel-Buffering"] == "no"

    events, done = [], None
    deadline = time.time() + max_wait
    buffer = ""
    for chunk in resp.response:
        if time.time() > deadline:
            break
        buffer += chunk.decode("utf-8")
        while "\n\n" in buffer:
            block, buffer = buffer.split("\n\n", 1)
            if block.startswith(":"):
                continue  # 註解 / keepalive
            fields = {}
            for line in block.splitlines():
                key, _, value = line.partition(": ")
                fields[key] = value
            if fields.get("event") == "done":
                done = json.loads(fields["data"])
            elif "data" in fields:
                events.append(json.loads(fields["data"]))
        if done is not None:
            break
    return events, done


def test_sse_streams_and_replays(app_ctx):
    client, _, _ = app_ctx
    run_id = client.post(
        "/api/runs",
        json={"graph": mock_graph(message="串流測試", files={"f.py": "1\n"},
                                  tools=["讀檔"]),
              "requirement": "r"},
    ).get_json()["run_id"]

    events, done = read_sse(client, run_id)

    assert done and done["status"] == "passed", done
    assert events, "沒有收到任何事件"

    # 序號嚴格遞增且唯一 —— 服務層事件與節點事件必須共用同一個序號來源
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs), "事件序號沒有遞增"
    assert len(seqs) == len(set(seqs)), "序號重複"

    kinds = {e["kind"] for e in events}
    assert {"status", "message", "tool_call", "file_edit", "usage"} <= kinds

    # 每則事件都必須走 SSE 的預設型別（沒有 event: 欄位），否則瀏覽器的
    # onmessage 只收得到 kind == "message" 的那些，其他類別會被靜默丟掉。
    raw = client.get(f"/api/runs/{run_id}/events").get_data(as_text=True)
    custom_types = {
        line.split(": ", 1)[1]
        for line in raw.splitlines()
        if line.startswith("event: ")
    }
    assert custom_types == {"done"}, f"只有終止標記可以有自訂型別，實際: {custom_types}"

    phases = [e["data"].get("phase") for e in events]
    assert phases[0] == "run_start", "run_start 必須是第一則事件"
    assert phases[-1] == "run_end", "run_end 必須是最後一則事件"


def test_sse_replay_after_seq(app_ctx):
    """斷線重連：?after=N 只補送 N 之後的事件。"""
    client, _, _ = app_ctx
    run_id = client.post(
        "/api/runs", json={"graph": mock_graph(message="x"), "requirement": "r"}
    ).get_json()["run_id"]
    wait_for(client, run_id)

    everything, _ = read_sse(client, run_id)
    assert len(everything) > 3

    cut = everything[2]["seq"]
    partial, done = read_sse(client, run_id, after=cut)
    assert done is not None
    assert all(e["seq"] > cut for e in partial)
    assert len(partial) == len(everything) - 3


def test_sse_on_finished_run_replays_from_db(app_ctx):
    """run 早就結束、記憶體裡沒有 bus 了，仍要能從 db 完整回放。"""
    client, app, _ = app_ctx
    run_id = client.post(
        "/api/runs", json={"graph": mock_graph(message="歷史"), "requirement": "r"}
    ).get_json()["run_id"]
    wait_for(client, run_id)

    # 狀態寫入 db 與執行緒收掉 handle 之間有極短的空窗，等它真的退場
    service = app.config["SERVICE"]
    deadline = time.time() + 10
    while service.handle(run_id) is not None and time.time() < deadline:
        time.sleep(0.05)
    assert service.handle(run_id) is None, "背景執行緒沒有釋放 handle"

    events, done = read_sse(client, run_id)
    assert done["status"] == "passed"
    assert any(e["text"] == "歷史" for e in events)


def test_sse_two_subscribers_both_get_events(app_ctx):
    """兩個分頁同時看同一個 run，都要收到完整事件。"""
    client, app, _ = app_ctx
    run_id = client.post(
        "/api/runs",
        json={"graph": mock_graph(message="x", sleep=0.2, tools=["a", "b"]),
              "requirement": "r"},
    ).get_json()["run_id"]

    results: list[list] = []

    def reader():
        with app.test_client() as c2:
            events, _ = read_sse(c2, run_id)
            results.append(events)

    threads = [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(results) == 2
    assert all(r for r in results), "有訂閱者收不到事件"
    for events in results:
        assert events[-1]["data"].get("phase") == "run_end"


def test_sse_unknown_run(app_ctx):
    client, _, _ = app_ctx
    assert client.get("/api/runs/nope/events").status_code == 404


def test_events_persisted_in_db(app_ctx):
    client, app, _ = app_ctx
    run_id = client.post(
        "/api/runs", json={"graph": mock_graph(message="x"), "requirement": "r"}
    ).get_json()["run_id"]
    wait_for(client, run_id)

    store = app.config["STORE"]
    assert store.event_count(run_id) > 0
    stored = store.get_events(run_id)
    assert [e["seq"] for e in stored] == sorted(e["seq"] for e in stored)


def test_orphan_runs_reaped_on_restart(app_ctx, tmp_path):
    """服務重啟後，卡在 running 的 run 要被標記，不能永遠顯示執行中。"""
    client, app, _ = app_ctx
    store = app.config["STORE"]
    orphan = store.create_run(mock_graph(), "遺留的 run")
    store.start_run(orphan, "task/x", "sha", "/wt")
    assert store.get_run(orphan)["status"] == "running"

    from app import create_app

    create_app(tmp_path / "config.yaml")  # 模擬重啟

    reloaded = store.get_run(orphan)
    assert reloaded["status"] == "failed"
    assert "服務重新啟動" in reloaded["reason"]
