"""AI Workflow Builder — Flask 入口。

執行：
    .venv/bin/python app.py

為什麼是 SSE 而不是 WebSocket
-----------------------------
需求是單向的（server → 瀏覽器推事件），SSE 直接用 Flask 的 generator response
就能做，不必多一個 WebSocket 依賴；瀏覽器端的 EventSource 還內建自動重連。
控制動作（取消、重跑）走普通的 POST。

工作流一律在背景執行緒跑（engine/service.py），request thread 只負責建立 run
與回應狀態 —— agent 節點動輒 10-30 分鐘，絕不能佔住 request thread。
"""

from __future__ import annotations

import json
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

import settings
from adapters.registry import Registry
from engine import graph as g
from engine.service import RunService, ServiceError
from engine.workspace import WorkspaceError, validate_project_repo
from store.db import Store

ROOT = Path(__file__).resolve().parent
WORKFLOW_DIR = ROOT / "workflows"


def create_app(config_path: Path | None = None) -> Flask:
    app = Flask(__name__)
    cfg = settings.load(config_path)
    store = Store(cfg.database)
    registry = Registry()
    service = RunService(cfg, store, registry)

    app.config.update(SETTINGS=cfg, STORE=store, REGISTRY=registry, SERVICE=service)
    _seed_workflows(store)

    # ------------------------------------------------------------ 頁面

    @app.get("/")
    def index():
        return render_template("editor.html")

    @app.get("/runs")
    def runs_page():
        return render_template("runs.html")

    @app.get("/runs/<run_id>")
    def run_page(run_id: str):
        return render_template("run_detail.html", run_id=run_id)

    # ------------------------------------------------------------ 基本

    @app.get("/api/health")
    def health():
        try:
            repo = validate_project_repo(cfg.project_repo, tool_root=ROOT)
            repo_status = {"ok": True, "path": str(repo)}
        except WorkspaceError as exc:
            repo_status = {"ok": False, "error": str(exc)}
        return jsonify(
            {
                "ok": True,
                "project_repo": repo_status,
                "main_branch": cfg.main_branch,
                "adapters_installed": registry.availability(),
            }
        )

    @app.get("/api/adapters")
    def adapters():
        """給前端組節點面板與節點設定表單用。"""
        installed = registry.availability()
        items = [
            {
                "id": s.id,
                "label": s.label,
                "description": s.description,
                "kind": s.kind,
                "mutates": s.mutates,
                "supports_schema": s.supports_schema,
                "installed": installed.get(s.id, False),
                "fields": [
                    {
                        "name": f.name,
                        "type": f.type,
                        "label": f.label or f.name,
                        "default": f.default,
                        "options": f.options,
                        "help": f.help,
                    }
                    for f in s.fields
                ],
            }
            for s in registry.all()
        ]
        return jsonify({"adapters": items, "builtins": _builtin_specs()})

    # ------------------------------------------------------- workflows

    @app.get("/api/workflows")
    def list_workflows():
        return jsonify({"workflows": store.list_workflows()})

    @app.get("/api/workflows/<wf_id>")
    def get_workflow(wf_id: str):
        found = store.get_workflow(wf_id)
        if not found:
            return jsonify({"error": f"找不到工作流: {wf_id}"}), 404
        return jsonify(found)

    @app.post("/api/workflows")
    def save_workflow():
        payload = request.get_json(silent=True) or {}
        try:
            graph = g.parse(payload)
        except g.GraphError as exc:
            return jsonify({"error": str(exc)}), 400
        problems = g.validate(graph, [s.id for s in registry.all()])
        wf_id = store.save_workflow(g.to_dict(graph) | {"name": graph.name or "未命名"})
        return jsonify({"id": wf_id, "problems": problems})

    @app.delete("/api/workflows/<wf_id>")
    def delete_workflow(wf_id: str):
        if not store.delete_workflow(wf_id):
            return jsonify({"error": "找不到工作流"}), 404
        return jsonify({"ok": True})

    @app.post("/api/workflows/validate")
    def validate_workflow():
        payload = request.get_json(silent=True) or {}
        try:
            graph = g.parse(payload)
        except g.GraphError as exc:
            return jsonify({"ok": False, "problems": [str(exc)]})
        problems = g.validate(graph, [s.id for s in registry.all()])
        return jsonify({"ok": not problems, "problems": problems})

    # ------------------------------------------------------------- runs

    @app.post("/api/runs")
    def start_run():
        payload = request.get_json(silent=True) or {}
        requirement = (payload.get("requirement") or "").strip()

        graph_dict = payload.get("graph")
        workflow_id = payload.get("workflow_id")
        if graph_dict is None and workflow_id:
            graph_dict = store.get_workflow(workflow_id)
            if graph_dict is None:
                return jsonify({"error": f"找不到工作流: {workflow_id}"}), 404
        if graph_dict is None:
            return jsonify({"error": "需要 graph 或 workflow_id"}), 400

        try:
            run_id = service.start(graph_dict, requirement, workflow_id)
        except (ServiceError, g.GraphError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"run_id": run_id}), 201

    @app.get("/api/runs")
    def list_runs():
        limit = min(int(request.args.get("limit", 50)), 200)
        items = store.list_runs(limit)
        for item in items:
            item["active"] = service.is_active(item["id"])
        return jsonify({"runs": items})

    @app.get("/api/runs/<run_id>")
    def get_run(run_id: str):
        found = store.get_run(run_id)
        if not found:
            return jsonify({"error": f"找不到 run: {run_id}"}), 404
        found["active"] = service.is_active(run_id)
        return jsonify(found)

    @app.get("/api/runs/<run_id>/diff")
    def run_diff(run_id: str):
        """這個 run 產生的變更。

        從 branch 算而不是從 worktree 算 —— worktree 可能已經清掉了，但 branch
        一定還在（要留給人工檢查與合併）。
        """
        found = store.get_run(run_id)
        if not found:
            return jsonify({"error": "找不到 run"}), 404
        if not found["branch"] or not found["base_sha"]:
            return jsonify({"diff": "", "files": [], "note": "這個 run 沒有建立 branch"})

        import subprocess

        def git(*args: str) -> str:
            proc = subprocess.run(
                ["git", *args], cwd=str(cfg.project_repo),
                capture_output=True, text=True,
            )
            return proc.stdout if proc.returncode == 0 else ""

        rng = f"{found['base_sha']}..{found['branch']}"
        return jsonify(
            {
                "diff": git("diff", rng)[:400_000],  # 別把整個瀏覽器塞爆
                "files": [f for f in git("diff", "--name-only", rng).splitlines() if f],
                "stat": git("diff", "--stat", rng),
                "log": git("log", "--oneline", rng),
                "branch": found["branch"],
                "merge_command": f"git merge --no-ff {found['branch']}",
            }
        )

    @app.get("/api/runs/<run_id>/artifacts")
    def run_artifacts(run_id: str):
        """節點產物（QA 的結構化輸出、schema 等），存在 repo 之外。"""
        base = cfg.runs_dir / run_id / "artifacts"
        if not base.exists():
            return jsonify({"artifacts": []})
        items = []
        for path in sorted(base.glob("*")):
            if not path.is_file():
                continue
            items.append(
                {
                    "name": path.name,
                    "size": path.stat().st_size,
                    "content": path.read_text("utf-8", errors="replace")[:20_000],
                }
            )
        return jsonify({"artifacts": items})

    @app.post("/api/runs/<run_id>/cancel")
    def cancel_run(run_id: str):
        if not store.get_run(run_id):
            return jsonify({"error": "找不到 run"}), 404
        if not service.cancel(run_id):
            return jsonify({"ok": False, "reason": "這個 run 已經結束了"}), 409
        return jsonify({"ok": True})

    @app.get("/api/runs/<run_id>/events")
    def run_events(run_id: str):
        """SSE 串流。斷線重連時用 Last-Event-ID 或 ?after= 從 db 補回漏掉的。"""
        if not store.get_run(run_id):
            return jsonify({"error": "找不到 run"}), 404

        after = request.headers.get("Last-Event-ID") or request.args.get("after") or "0"
        try:
            after_seq = int(after)
        except ValueError:
            after_seq = 0

        bus = service.bus_for(run_id)

        def generate():
            # 先送一則註解，讓瀏覽器立刻確立連線
            yield ": connected\n\n"
            for event in bus.stream(after_seq=after_seq):
                if event is None:
                    yield ": keepalive\n\n"  # 維持連線，避免中間層砍掉閒置連線
                    continue
                # 刻意不設 event: <kind>。EventSource 的 onmessage 只會收到
                # 預設型別的事件，若每則都標上自己的 kind，前端就得為每一種
                # kind 各註冊一個 listener —— 漏一個就靜默丟掉整類事件。
                # 全部走預設型別、由前端讀 data.kind 分流，加新 kind 也不用改前端。
                yield (
                    f"id: {event['seq']}\n"
                    f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                )
            final = store.get_run(run_id) or {}
            yield (
                "event: done\n"
                f"data: {json.dumps({'status': final.get('status'), 'reason': final.get('reason')}, ensure_ascii=False)}\n\n"
            )

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",  # 別讓反向代理緩衝住串流
                "Connection": "keep-alive",
            },
        )

    return app


def _builtin_specs() -> list[dict]:
    """內建節點的設定表單描述（與 adapters 的 fields 同一套格式）。"""
    return [
        {
            "id": g.REQUIREMENT,
            "label": "需求",
            "description": "run 的起點。留空就用啟動 run 時輸入的需求文字。",
            "kind": "builtin",
            "mutates": False,
            "installed": True,
            "supports_schema": False,
            "fields": [
                {"name": "text", "type": "textarea", "label": "需求內容（可留空）",
                 "default": "", "options": [],
                 "help": "留空時使用啟動 run 時輸入的需求。"}
            ],
        },
        {
            "id": g.CONDITION,
            "label": "條件判斷",
            "description": "依運算式選擇 true / false 出口。把 false 出口連回上游就形成重試迴圈。",
            "kind": "builtin",
            "mutates": False,
            "installed": True,
            "supports_schema": False,
            "fields": [
                {"name": "expr", "type": "textarea", "label": "運算式",
                 "default": "nodes.qa.structured.verdict == 'PASS'", "options": [],
                 "help": "可用 nodes.<id>.last_message / .structured / .exit_code、loop.iteration、run.changed_files"}
            ],
        },
        {
            "id": g.GIT,
            "label": "Git",
            "description": "commit 目前變更，或重新計算 diff 給下游審查節點。",
            "kind": "builtin",
            "mutates": True,
            "installed": True,
            "supports_schema": False,
            "fields": [
                {"name": "action", "type": "select", "label": "動作",
                 "default": "commit", "options": ["commit", "diff"], "help": ""},
                {"name": "message", "type": "text", "label": "Commit 訊息",
                 "default": "wip: {{ run.id }}", "options": [],
                 "help": "支援 Jinja 模板。沒有變更時不會建立 commit，也不算失敗。"},
            ],
        },
    ]


def _seed_workflows(store: Store) -> None:
    """把 workflows/*.json 匯入 db（同 id 就更新）。"""
    for path in sorted(WORKFLOW_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text("utf-8"))
            g.parse(payload)  # 壞掉的範本不要塞進 db
        except (json.JSONDecodeError, g.GraphError):
            continue
        if not store.get_workflow(payload.get("id", "")):
            store.save_workflow(payload)


if __name__ == "__main__":
    # threaded=True：SSE 連線會長時間佔住一個執行緒，不能用單執行緒模式
    create_app().run(host="127.0.0.1", port=5111, debug=True, threaded=True)
