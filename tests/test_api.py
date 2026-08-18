"""Flask API 與 SSE 串流測試。

用真的 mock 工作流跑完整條路徑：POST /api/projects/<pid>/runs → 背景執行緒 →
SSE 事件 → 落進 sqlite → 重連補送。

run 一律 project-scoped：工作流住在專案資料夾裡，執行也一定綁在那個專案的 repo 上。
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
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
        registered = client.post("/api/projects", json={"path": str(repo)})
        assert registered.status_code == 201, registered.get_json()
        assert registered.get_json()["id"] == PID
        yield client, app, repo


# 專案 id 由資料夾名稱決定（repo 建在 tmp_path / "proj"）
PID = "proj"
RUNS = f"/api/projects/{PID}/runs"
WF = f"/api/projects/{PID}/workflows"


def run_url(run_id: str, path: str = "", pid: str = PID) -> str:
    """run 的端點全部掛在專案底下 —— 紀錄存在該專案自己的資料庫裡。"""
    return f"/api/projects/{pid}/runs/{run_id}{path}"


def store_for(app, pid: str = PID):
    """取得某個專案的紀錄資料庫。已經沒有單一的中央 STORE 了。"""
    from engine import project as proj

    cfg = app.config["SETTINGS"]
    entry = app.config["PROJECTS"].get(pid)
    resolved = proj.for_project(cfg, entry.id, entry.path, entry.name)
    return app.config["STORES"].for_project(resolved)


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


def wait_for(client, run_id, timeout=60, pid=PID):
    """等 run 結束，回傳最終狀態。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(run_url(run_id, pid=pid)).get_json()
        if body["status"] not in ("queued", "running"):
            return body
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} 沒有在 {timeout}s 內結束")


# ------------------------------------------------------------ 基本端點


def test_health_reports_projects_and_adapters(app_ctx):
    client, _, _ = app_ctx
    body = client.get("/api/health").get_json()
    assert body["ok"] is True
    assert body["projects"] == 1
    assert "mock" in body["adapters_installed"]


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
    for path in ("/", "/runs", f"/projects/{PID}/runs/anything"):
        assert client.get(path).status_code == 200


# --------------------------------------------------------------- 工作流


def test_new_project_starts_with_no_workflows(app_ctx):
    """範本不再自動種入 —— 在別人的 repo 裡放他沒要求、而且會進 git 的檔案是不對的。"""
    client, _, _ = app_ctx
    assert client.get(WF).get_json()["workflows"] == []


def test_template_can_be_imported_into_project(app_ctx, tmp_path):
    client, _, repo = app_ctx
    ids = [t["id"] for t in client.get("/api/templates").get_json()["templates"]]
    assert "plan-impl-qa" in ids

    resp = client.post(f"{WF}/import/plan-impl-qa")
    assert resp.status_code == 201

    # 複製完就是專案自己的檔案，看得到、進得了 git
    assert (repo / ".ai-workflow-proj" / "workflows" / "plan-impl-qa.json").exists()
    wf = client.get(f"{WF}/plan-impl-qa").get_json()
    assert any(n["id"] == "gate" for n in wf["nodes"])


def test_workflow_save_get_delete(app_ctx):
    client, _, _ = app_ctx
    saved = client.post(WF, json=mock_graph()).get_json()
    assert saved["problems"] == []

    wf = client.get(f"{WF}/{saved['id']}").get_json()
    assert wf["name"] == "測試流程"

    assert client.delete(f"{WF}/{saved['id']}").status_code == 200
    assert client.get(f"{WF}/{saved['id']}").status_code == 404
    assert client.delete(f"{WF}/{saved['id']}").status_code == 404


def test_workflow_save_rejects_malformed(app_ctx):
    client, _, _ = app_ctx
    resp = client.post(WF, json={"nodes": []})
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
        RUNS,
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
        RUNS,
        json={"graph": {"name": "x",
                        "nodes": [{"id": "a", "type": "ghost", "config": {}}],
                        "edges": []},
              "requirement": "r"},
    )
    assert resp.status_code == 400
    assert "未知的型別" in resp.get_json()["error"]


def test_run_from_workflow_id(app_ctx):
    client, _, _ = app_ctx
    wf_id = client.post(WF, json=mock_graph()).get_json()["id"]
    resp = client.post(RUNS, json={"workflow_id": wf_id, "requirement": "r"})
    assert resp.status_code == 201
    assert wait_for(client, resp.get_json()["run_id"])["status"] == "passed"


def test_run_requires_graph_or_workflow(app_ctx):
    client, _, _ = app_ctx
    assert client.post(RUNS, json={"requirement": "r"}).status_code == 400
    assert client.post(
        RUNS, json={"workflow_id": "nope", "requirement": "r"}
    ).status_code == 404


def test_failed_run_records_reason(app_ctx):
    client, _, _ = app_ctx
    run_id = client.post(
        RUNS,
        json={"graph": mock_graph(message="x", exit_code=2), "requirement": "r"},
    ).get_json()["run_id"]

    final = wait_for(client, run_id)
    assert final["status"] == "failed"
    assert "exit code 2" in final["reason"]


def test_cancel_run(app_ctx):
    client, _, _ = app_ctx
    run_id = client.post(
        RUNS,
        json={"graph": mock_graph(message="x", sleep=3, tools=["a", "b", "c"]),
              "requirement": "r"},
    ).get_json()["run_id"]

    deadline = time.time() + 15
    while time.time() < deadline:
        if client.get(run_url(run_id)).get_json()["status"] == "running":
            break
        time.sleep(0.05)

    assert client.post(run_url(run_id, "/cancel")).get_json()["ok"] is True
    assert wait_for(client, run_id)["status"] == "cancelled"
    assert client.post(run_url(run_id, "/cancel")).status_code == 409


def test_cancel_unknown_run(app_ctx):
    client, _, _ = app_ctx
    assert client.post(run_url("nope", "/cancel")).status_code == 404


def test_list_runs(app_ctx):
    client, _, _ = app_ctx
    for _ in range(3):
        rid = client.post(
            RUNS, json={"graph": mock_graph(), "requirement": "r"}
        ).get_json()["run_id"]
        wait_for(client, rid)

    runs = client.get("/api/runs").get_json()["runs"]
    assert len(runs) == 3
    assert all("active" in r for r in runs)


# ------------------------------------------------------------------ SSE


def read_sse(client, run_id, after=None, max_wait=60):
    """讀完整條 SSE 串流，回傳 (events, done_payload)。"""
    url = run_url(run_id, "/events") + (f"?after={after}" if after else "")
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
        RUNS,
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
    raw = client.get(run_url(run_id, "/events")).get_data(as_text=True)
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
        RUNS, json={"graph": mock_graph(message="x"), "requirement": "r"}
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
        RUNS, json={"graph": mock_graph(message="歷史"), "requirement": "r"}
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
        RUNS,
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


def test_run_end_event_carries_final_status(app_ctx):
    """run_end 事件本身要帶最終狀態。

    前端的狀態標頭直接讀這則事件，不再另發一個 request —— 原本靠 run_end 觸發
    fetch 才更新，那個 fetch 一慢，標頭就永遠卡在 running 且不會自己恢復。
    """
    client, _, _ = app_ctx
    run_id = client.post(
        RUNS, json={"graph": mock_graph(message="x"), "requirement": "r"}
    ).get_json()["run_id"]
    events, _ = read_sse(client, run_id)

    end = events[-1]
    assert end["data"]["phase"] == "run_end"
    assert end["data"]["status"] == "passed"
    assert end["data"]["branch"] == f"task/{run_id}"


def test_failed_run_end_event_carries_reason(app_ctx):
    client, _, _ = app_ctx
    run_id = client.post(
        RUNS,
        json={"graph": mock_graph(message="x", exit_code=7), "requirement": "r"},
    ).get_json()["run_id"]
    events, _ = read_sse(client, run_id)

    end = events[-1]
    assert end["data"]["phase"] == "run_end"
    assert end["data"]["status"] == "failed"
    assert "exit code 7" in end["data"]["reason"]


def test_sse_unknown_run(app_ctx):
    client, _, _ = app_ctx
    assert client.get(run_url("nope", "/events")).status_code == 404


def test_events_persisted_in_db(app_ctx):
    client, app, _ = app_ctx
    run_id = client.post(
        RUNS, json={"graph": mock_graph(message="x"), "requirement": "r"}
    ).get_json()["run_id"]
    wait_for(client, run_id)

    store = store_for(app)
    assert store.event_count(run_id) > 0
    stored = store.get_events(run_id)
    assert [e["seq"] for e in stored] == sorted(e["seq"] for e in stored)


# --------------------------------------------------------- 專案 API


def test_fixture_project_is_listed(app_ctx):
    client, _, repo = app_ctx
    listed = client.get("/api/projects").get_json()["projects"]
    assert [p["id"] for p in listed] == [PID]
    assert listed[0]["path"] == str(repo.resolve())


def test_add_project_scaffolds_and_registers(app_ctx, tmp_path):
    client, _, _ = app_ctx
    repo = make_repo(tmp_path / "another")

    body = client.post("/api/projects", json={"path": str(repo)}).get_json()

    assert body["ok"] is True
    assert body["initialised"] is True
    assert (repo / ".ai-workflow-proj" / "project.yaml").exists()
    assert body["id"] in {p["id"] for p in client.get("/api/projects").get_json()["projects"]}


def test_add_project_expands_user_path(app_ctx, tmp_path, monkeypatch):
    """使用者會直接貼 ~/... 進來。"""
    client, _, _ = app_ctx
    home = tmp_path / "home"
    repo = make_repo(home / "code" / "proj")
    monkeypatch.setenv("HOME", str(home))

    resp = client.post("/api/projects", json={"path": "~/code/proj"})
    assert resp.status_code == 201
    assert resp.get_json()["path"] == str(repo.resolve())


def test_add_project_rejects_non_repo(app_ctx, tmp_path):
    """路徑現在來自瀏覽器輸入，防線比以前更重要。"""
    client, _, _ = app_ctx
    plain = tmp_path / "just-a-folder"
    plain.mkdir()

    resp = client.post("/api/projects", json={"path": str(plain)})
    assert resp.status_code == 400
    assert not (plain / ".ai-workflow-proj").exists()


def test_add_project_rejects_tool_itself(app_ctx):
    client, _, _ = app_ctx
    resp = client.post("/api/projects", json={"path": str(ROOT)})

    assert resp.status_code == 400
    assert "本工具自己" in resp.get_json()["error"]
    assert not (ROOT / ".ai-workflow-proj").exists()


def test_add_project_twice_is_rejected_with_existing_id(app_ctx, tmp_path):
    client, _, _ = app_ctx
    repo = make_repo(tmp_path / "dup")
    first = client.post("/api/projects", json={"path": str(repo)}).get_json()

    resp = client.post("/api/projects", json={"path": str(repo)})
    assert resp.status_code == 409
    assert resp.get_json()["id"] == first["id"]


def test_project_payload_reports_resolved_settings(app_ctx, tmp_path):
    client, _, _ = app_ctx
    repo = make_repo(tmp_path / "backend")
    pid = client.post("/api/projects", json={"path": str(repo)}).get_json()["id"]

    paths = repo / ".ai-workflow-proj"
    (paths / "project.yaml").write_text(
        yaml.safe_dump({"name": "後端", "main_branch": "develop"}), "utf-8"
    )

    body = client.get(f"/api/projects/{pid}").get_json()
    assert body["name"] == "後端"
    assert body["main_branch"] == "develop"


def test_project_reports_unhealthy_when_folder_disappears(app_ctx, tmp_path):
    """專案資料夾被搬走 / 刪掉之後，清單不能還宣稱它是好的。"""
    import shutil

    client, _, _ = app_ctx
    repo = make_repo(tmp_path / "gone")
    pid = client.post("/api/projects", json={"path": str(repo)}).get_json()["id"]
    shutil.rmtree(repo)

    body = client.get(f"/api/projects/{pid}").get_json()
    assert body["ok"] is False
    assert body["error"]


def test_remove_project_keeps_files(app_ctx, tmp_path):
    client, _, _ = app_ctx
    repo = make_repo(tmp_path / "keepme")
    pid = client.post("/api/projects", json={"path": str(repo)}).get_json()["id"]

    assert client.delete(f"/api/projects/{pid}").status_code == 200
    assert client.get(f"/api/projects/{pid}").status_code == 404
    assert (repo / ".ai-workflow-proj" / "project.yaml").exists()


# ------------------------------------------------------- diff 用對 repo


def _committing_graph(filename: str, content: str) -> dict:
    """寫檔並 commit 的工作流。

    diff 是從 base_sha..branch 算的，所以一定要有 commit ——
    只寫檔不 commit 的話，worktree 收掉之後那些變更就不存在了。
    """
    graph = mock_graph(message="寫好了", files={filename: content})
    graph["nodes"][1]["mutates"] = True
    graph["nodes"].append(
        {"id": "save", "type": "git", "config": {"action": "commit",
                                                 "message": "test commit"}}
    )
    graph["edges"].append({"from": "a", "to": "save"})
    return graph


def test_diff_comes_from_the_project_the_run_used(app_ctx, tmp_path):
    """多專案之後，diff 必須用 run 自己記下的 repo。

    以前這裡寫死全域設定的那一個 repo。拿錯 repo 的話 git 只會回非零，
    我們就給出一份空 diff —— 看起來像「這次沒有任何變更」，是最難查的錯。
    """
    client, _, _ = app_ctx

    # 第二個專案，而且比 fixture 那個更晚使用（確保它不是「預設」那一個）
    other = make_repo(tmp_path / "other")
    other_id = client.post("/api/projects", json={"path": str(other)}).get_json()["id"]

    run_id = client.post(
        f"/api/projects/{other_id}/runs",
        json={"graph": _committing_graph("only-in-other.txt", "hi"),
              "requirement": "r"},
    ).get_json()["run_id"]
    assert wait_for(client, run_id, pid=other_id)["status"] == "passed"

    body = client.get(run_url(run_id, "/diff", pid=other_id)).get_json()
    assert "only-in-other.txt" in body["files"], body
    assert "only-in-other.txt" in body["diff"]


def test_diff_refuses_when_project_folder_is_gone(app_ctx, tmp_path):
    """資料夾不見了要明講，不能回一份空 diff 讓人以為沒有變更。"""
    import shutil

    client, _, _ = app_ctx
    gone = make_repo(tmp_path / "gone")
    pid = client.post("/api/projects", json={"path": str(gone)}).get_json()["id"]
    run_id = client.post(
        f"/api/projects/{pid}/runs",
        json={"graph": _committing_graph("x.txt", "hi"), "requirement": "r"},
    ).get_json()["run_id"]
    wait_for(client, run_id, pid=pid)

    shutil.rmtree(gone)
    resp = client.get(run_url(run_id, "/diff", pid=pid))
    assert resp.status_code == 409
    # 訊息要指出是資料夾的問題，而不是回一份空 diff 讓人以為沒有變更
    error = resp.get_json()["error"]
    assert "資料夾" in error and str(gone) in error


def test_artifacts_live_in_the_projects_local_dir(app_ctx, tmp_path):
    """產物跟著專案走，但被 .gitignore 擋住，不會被 merge 進 main。"""
    client, _, repo = app_ctx
    graph = mock_graph(message="ok", structured={"verdict": "PASS"})
    # artifacts 只有在節點要求結構化輸出時才會產生
    graph["nodes"][1]["config"]["schema"] = json.dumps(
        {"type": "object", "properties": {"verdict": {"type": "string"}}}
    )
    run_id = client.post(
        RUNS, json={"graph": graph, "requirement": "r"}
    ).get_json()["run_id"]
    assert wait_for(client, run_id)["status"] == "passed"

    local_runs = repo / ".ai-workflow-proj" / "local" / "runs" / run_id
    assert local_runs.is_dir()
    assert client.get(run_url(run_id, "/artifacts")).status_code == 200

    ignored = subprocess.run(
        ["git", "check-ignore", "-q", str(local_runs)], cwd=str(repo)
    ).returncode == 0
    assert ignored, "產物必須被 .gitignore 擋住"


def test_worktree_is_namespaced_by_project(app_ctx, tmp_path):
    """兩個專案的 worktree 不能混在同一層。"""
    client, app, _ = app_ctx
    run_id = client.post(
        RUNS, json={"graph": mock_graph(message="ok"), "requirement": "r"}
    ).get_json()["run_id"]
    wait_for(client, run_id)

    worktree = store_for(app).get_run(run_id)["worktree"]
    assert f"/{PID}/" in worktree, worktree


# ------------------------------------------------- 每專案一個資料庫


def _register(client, path):
    return client.post("/api/projects", json={"path": str(path)}).get_json()["id"]


def test_each_project_gets_its_own_database(app_ctx, tmp_path):
    """紀錄存在專案的 local/ 底下，不是中央資料庫。"""
    client, _, repo = app_ctx
    run_id = client.post(
        RUNS, json={"graph": mock_graph(message="ok"), "requirement": "r"}
    ).get_json()["run_id"]
    wait_for(client, run_id)

    db = repo / ".ai-workflow-proj" / "local" / "ai-workflow.sqlite"
    assert db.exists()
    # 中央資料庫只剩專案清單
    central = sqlite3.connect(tmp_path / "t.sqlite")
    tables = {r[0] for r in central.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    central.close()
    assert "projects" in tables
    assert "runs" not in tables


def test_runs_do_not_leak_between_projects(app_ctx, tmp_path):
    client, _, _ = app_ctx
    other = _register(client, make_repo(tmp_path / "other"))

    mine = client.post(
        RUNS, json={"graph": mock_graph(message="mine"), "requirement": "r"}
    ).get_json()["run_id"]
    wait_for(client, mine)

    assert [r["id"] for r in client.get(RUNS).get_json()["runs"]] == [mine]
    assert client.get(f"/api/projects/{other}/runs").get_json()["runs"] == []
    # 用別的專案的網址去查也查不到
    assert client.get(run_url(mine, pid=other)).status_code == 404


def test_cross_project_overview_merges_every_database(app_ctx, tmp_path):
    client, _, _ = app_ctx
    other = _register(client, make_repo(tmp_path / "other"))

    a = client.post(
        RUNS, json={"graph": mock_graph(message="a"), "requirement": "r"}
    ).get_json()["run_id"]
    wait_for(client, a)
    b = client.post(
        f"/api/projects/{other}/runs",
        json={"graph": mock_graph(message="b"), "requirement": "r"},
    ).get_json()["run_id"]
    wait_for(client, b, pid=other)

    listed = client.get("/api/runs").get_json()["runs"]
    assert {r["id"] for r in listed} == {a, b}
    assert {r["project_id"] for r in listed} == {PID, other}
    assert listed[0]["id"] == b, "最新的排前面"


def test_overview_survives_a_missing_project(app_ctx, tmp_path):
    """一個專案的資料夾被搬走，不能讓總覽整個開不起來。"""
    import shutil

    client, _, _ = app_ctx
    gone_repo = make_repo(tmp_path / "gone")
    _register(client, gone_repo)

    mine = client.post(
        RUNS, json={"graph": mock_graph(message="ok"), "requirement": "r"}
    ).get_json()["run_id"]
    wait_for(client, mine)
    shutil.rmtree(gone_repo)

    listed = client.get("/api/runs").get_json()["runs"]
    assert [r["id"] for r in listed] == [mine]


def test_deleted_project_folder_is_not_resurrected(app_ctx, tmp_path):
    """Store 的建構會 mkdir -p。專案被刪掉之後不能因為一次查詢就把它長回來。"""
    import shutil

    client, _, _ = app_ctx
    gone_repo = make_repo(tmp_path / "gone")
    _register(client, gone_repo)
    shutil.rmtree(gone_repo)

    client.get("/api/runs")
    assert not gone_repo.exists()


def test_orphans_reaped_in_every_project(app_ctx, tmp_path):
    """重啟後每個專案的資料庫都要掃過 —— 漏掉一個，那個專案的 run
    會永遠顯示執行中。"""
    client, app, _ = app_ctx
    other_repo = make_repo(tmp_path / "other")
    other = _register(client, other_repo)

    orphans = {}
    for pid in (PID, other):
        store = store_for(app, pid)
        run_id = store.create_run(mock_graph(), "遺留的 run", project_id=pid)
        store.start_run(run_id, "task/x", "sha", "/wt")
        orphans[pid] = run_id

    from app import create_app

    create_app(tmp_path / "config.yaml")  # 模擬重啟

    for pid, run_id in orphans.items():
        reloaded = store_for(app, pid).get_run(run_id)
        assert reloaded["status"] == "failed", pid
        assert "服務重新啟動" in reloaded["reason"]


# ------------------------------------------------------------ 版面資產


def test_shared_stylesheet_is_loaded_by_the_base_layout():
    """頁首的專案選擇器住在 base.html，用的是 app.css 的 .fi 樣式。

    app.css 若只掛在部分頁面，選擇器在其他頁就沒有樣式（白底輸入框）。
    共用元件的樣式必須跟著共用 —— 這個迴歸實際發生過。
    """
    base = (ROOT / "templates" / "base.html").read_text("utf-8")
    assert "css/app.css" in base

    # 各頁不該再自己掛一次，重複載入只會讓「到底哪一份生效」變得不明顯
    for name in ("editor.html", "runs.html", "run_detail.html"):
        page = (ROOT / "templates" / name).read_text("utf-8")
        assert "css/app.css" not in page, f"{name} 重複掛了 app.css"


def test_every_page_extends_the_base_layout():
    """漏掉 base.html 的頁面就沒有專案選擇器，也就沒辦法切換專案。"""
    for name in ("editor.html", "runs.html", "run_detail.html"):
        page = (ROOT / "templates" / name).read_text("utf-8")
        assert 'extends "base.html"' in page, name
