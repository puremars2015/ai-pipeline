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
from engine import project as proj
from engine.service import RunService, ServiceError
from engine.workspace import WorkspaceError, detect_main_branch, validate_project_repo
from store import templates
from store.projects import Project, ProjectRegistry
from store.stores import StoreRegistry, merge_runs
from store.workflows import WorkflowError, WorkflowStore

ROOT = Path(__file__).resolve().parent


def create_app(config_path: Path | None = None) -> Flask:
    app = Flask(__name__)
    cfg = settings.load(config_path)
    registry = Registry()
    # 中央資料庫現在只剩專案清單；執行紀錄各自住在專案的 local/ 底下。
    projects = ProjectRegistry(cfg.database)
    stores = StoreRegistry()
    service = RunService(cfg, stores, projects, registry)

    app.config.update(
        SETTINGS=cfg, REGISTRY=registry, SERVICE=service,
        PROJECTS=projects, STORES=stores,
    )

    # ------------------------------------------------------------ 頁面

    @app.get("/")
    def index():
        return render_template("editor.html")

    @app.get("/runs")
    def runs_page():
        return render_template("runs.html")

    @app.get("/projects/<pid>/runs/<run_id>")
    def run_page(pid: str, run_id: str):
        return render_template("run_detail.html", run_id=run_id, project_id=pid)

    # ------------------------------------------------------------ 基本

    @app.get("/api/health")
    def health():
        """工具層的健康狀態。

        不再回報單一的目標 repo —— 那個概念已經被專案清單取代。
        個別專案好不好是 /api/projects 的事（那裡每筆都會重算）。
        """
        return jsonify(
            {
                "ok": True,
                "projects": len(projects.list()),
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

    # -------------------------------------------------------- projects

    def _project_payload(entry: Project) -> dict:
        """一筆專案 + 它現在的健康狀態。

        健康狀態每次都重算而不是註冊時存起來 —— 專案資料夾會被搬走、
        刪掉、或 checkout 成另一個狀態，存下來的答案很快就是錯的。
        """
        payload = {
            "id": entry.id,
            "path": str(entry.path),
            "name": entry.name,
            "added_at": entry.added_at,
            "last_used_at": entry.last_used_at,
            "initialised": proj.is_initialised(entry.path),
        }
        try:
            validate_project_repo(entry.path, tool_root=ROOT)
            resolved = proj.for_project(cfg, entry.id, entry.path, entry.name)
        except (WorkspaceError, proj.ProjectError) as exc:
            return payload | {"ok": False, "error": str(exc)}

        return payload | {
            "ok": True,
            "name": resolved.name,
            "main_branch": resolved.main_branch,
            "isolation": resolved.isolation,
            "worktree_root": str(resolved.worktree_root),
            "workflows_dir": str(resolved.workflows_dir),
        }

    @app.get("/api/projects")
    def list_projects():
        return jsonify({"projects": [_project_payload(p) for p in projects.list()]})

    @app.post("/api/projects")
    def add_project():
        """註冊一個專案：驗證 → 初始化 .ai-workflow-proj/ → 記進清單。

        三步驟的順序不能換。前兩步都可能失敗，而失敗時絕不能在清單裡
        留下一筆指向壞掉路徑的紀錄。
        """
        payload = request.get_json(silent=True) or {}
        raw_path = (payload.get("path") or "").strip()
        if not raw_path:
            return jsonify({"error": "需要專案路徑"}), 400

        try:
            repo = validate_project_repo(
                settings.resolve_path(raw_path), tool_root=ROOT
            )
        except WorkspaceError as exc:
            return jsonify({"error": str(exc)}), 400

        existing = projects.get_by_path(repo)
        if existing:
            return jsonify(
                {"error": f"這個專案已經在清單裡了（{existing.id}）", "id": existing.id}
            ), 409

        name = (payload.get("name") or "").strip() or repo.name
        try:
            proj.scaffold(
                repo, name=name, main_branch=detect_main_branch(repo, cfg.main_branch)
            )
        except (proj.ProjectError, OSError) as exc:
            return jsonify({"error": f"無法建立 {proj.DIR_NAME}/: {exc}"}), 400

        return jsonify(_project_payload(projects.add(repo, name=name))), 201

    @app.get("/api/projects/<pid>")
    def get_project(pid: str):
        entry = projects.get(pid)
        if not entry:
            return jsonify({"error": f"找不到專案: {pid}"}), 404
        return jsonify(_project_payload(entry))

    @app.delete("/api/projects/<pid>")
    def remove_project(pid: str):
        """只從清單移除，不刪任何檔案。

        .ai-workflow-proj/ 裡是已經 commit 進使用者 git 的工作流定義，
        以及他們的執行紀錄。這個 API 沒有立場刪它們。
        """
        if not projects.remove(pid):
            return jsonify({"error": f"找不到專案: {pid}"}), 404
        return jsonify({"ok": True, "note": f"已從清單移除，{proj.DIR_NAME}/ 保持原樣"})

    # ------------------------------------------------------- workflows

    def _resolve_project(pid: str):
        """pid → (設定, 工作流資料夾, 紀錄資料庫)。失敗時回 (None, 錯誤回應)。

        每個 project-scoped 的路由都從這裡開始，所以「專案不存在」與
        「專案資料夾壞掉了」只會有一種錯誤訊息與一個狀態碼。
        """
        entry = projects.get(pid)
        if not entry:
            return None, (jsonify({"error": f"找不到專案: {pid}"}), 404)
        try:
            resolved = proj.for_project(cfg, entry.id, entry.path, entry.name)
            store = stores.for_project(resolved)
        except proj.ProjectError as exc:
            return None, (jsonify({"error": str(exc)}), 400)
        except (FileNotFoundError, OSError) as exc:
            return None, (jsonify({"error": f"專案資料夾無法使用: {exc}"}), 409)
        return (resolved, WorkflowStore(resolved.workflows_dir), store), None

    @app.get("/api/projects/<pid>/workflows")
    def list_workflows(pid: str):
        found, err = _resolve_project(pid)
        if err:
            return err
        return jsonify({"workflows": found[1].list()})

    @app.get("/api/projects/<pid>/workflows/<wf_id>")
    def get_workflow(pid: str, wf_id: str):
        found, err = _resolve_project(pid)
        if err:
            return err
        try:
            graph = found[1].get(wf_id)
        except WorkflowError as exc:
            return jsonify({"error": str(exc)}), 400
        if not graph:
            return jsonify({"error": f"找不到工作流: {wf_id}"}), 404
        return jsonify(graph)

    @app.post("/api/projects/<pid>/workflows")
    def save_workflow(pid: str):
        found, err = _resolve_project(pid)
        if err:
            return err
        payload = request.get_json(silent=True) or {}
        try:
            graph = g.parse(payload)
        except g.GraphError as exc:
            return jsonify({"error": str(exc)}), 400

        problems = g.validate(graph, [s.id for s in registry.all()])
        try:
            wf_id = found[1].save(
                g.to_dict(graph) | {"name": graph.name or "未命名"}
            )
        except (WorkflowError, OSError) as exc:
            return jsonify({"error": f"寫入失敗: {exc}"}), 400
        return jsonify({"id": wf_id, "problems": problems})

    @app.delete("/api/projects/<pid>/workflows/<wf_id>")
    def delete_workflow(pid: str, wf_id: str):
        found, err = _resolve_project(pid)
        if err:
            return err
        try:
            removed = found[1].delete(wf_id)
        except WorkflowError as exc:
            return jsonify({"error": str(exc)}), 400
        if not removed:
            return jsonify({"error": "找不到工作流"}), 404
        return jsonify({"ok": True})

    @app.get("/api/templates")
    def list_templates():
        """隨附範本。新專案的 workflows/ 是空的，得有個起點。"""
        return jsonify({"templates": templates.list_templates()})

    @app.post("/api/projects/<pid>/workflows/import/<template_id>")
    def import_template(pid: str, template_id: str):
        """把一個範本複製進這個專案。複製完就是專案自己的檔案。"""
        found, err = _resolve_project(pid)
        if err:
            return err
        payload = templates.load_template(template_id)
        if payload is None:
            return jsonify({"error": f"找不到範本: {template_id}"}), 404

        try:
            g.parse(payload)
        except g.GraphError as exc:
            return jsonify({"error": f"範本本身有問題: {exc}"}), 500

        # 已經有同名的就配一個新 id，不要默默蓋掉使用者調過的版本
        store_ = found[1]
        if store_.exists(template_id):
            payload = {**payload, "id": store_.new_id(template_id)}
        return jsonify({"id": store_.save(payload)}), 201

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
    #
    # run 的路由全部掛在專案底下。執行紀錄存在該專案自己的資料庫裡
    # （.ai-workflow-proj/local/ai-workflow.sqlite），光有 run id 是查不到的 ——
    # 得先知道去哪個資料庫找。這也讓「拿 A 專案的設定去查 B 專案的 run」
    # 在結構上就不可能發生。

    @app.post("/api/projects/<pid>/runs")
    def start_run(pid: str):
        """在這個專案的 repo 上跑一個工作流。"""
        found, err = _resolve_project(pid)
        if err:
            return err
        project, workflows, _ = found

        payload = request.get_json(silent=True) or {}
        requirement = (payload.get("requirement") or "").strip()

        graph_dict = payload.get("graph")
        workflow_id = payload.get("workflow_id")
        if graph_dict is None and workflow_id:
            try:
                graph_dict = workflows.get(workflow_id)
            except WorkflowError as exc:
                return jsonify({"error": str(exc)}), 400
            if graph_dict is None:
                return jsonify({"error": f"找不到工作流: {workflow_id}"}), 404
        if graph_dict is None:
            return jsonify({"error": "需要 graph 或 workflow_id"}), 400

        try:
            run_id = service.start(project, graph_dict, requirement, workflow_id)
        except (ServiceError, g.GraphError) as exc:
            return jsonify({"error": str(exc)}), 400
        projects.touch(pid)
        return jsonify({"run_id": run_id}), 201

    @app.get("/api/projects/<pid>/runs")
    def list_project_runs(pid: str):
        found, err = _resolve_project(pid)
        if err:
            return err
        limit = min(int(request.args.get("limit", 50)), 200)
        items = found[2].list_runs(limit)
        for item in items:
            item["active"] = service.is_active(item["id"])
        return jsonify({"runs": items})

    @app.get("/api/runs")
    def list_all_runs():
        """跨專案總覽：把每個專案的資料庫各查一次再合併。

        專案數是使用者手動註冊的（個位數），直接查完合併就好 —— 另外維護
        一份索引表只會多出「索引跟真實資料不同步」這種問題。
        讀不到的專案（資料夾被搬走）跳過，不能讓總覽整個開不起來。
        """
        limit = min(int(request.args.get("limit", 50)), 200)
        openable = []
        for entry in projects.list():
            try:
                resolved = proj.for_project(cfg, entry.id, entry.path, entry.name)
                openable.append((entry.id, stores.for_project(resolved)))
            except (proj.ProjectError, FileNotFoundError, OSError):
                continue

        items = merge_runs(openable, limit)
        for item in items:
            item["active"] = service.is_active(item["id"])
        return jsonify({"runs": items})

    def _load_run(pid: str, run_id: str):
        """(專案, run) 或錯誤回應。"""
        found, err = _resolve_project(pid)
        if err:
            return None, err
        run = found[2].get_run(run_id)
        if not run:
            return None, (jsonify({"error": f"找不到 run: {run_id}"}), 404)
        return (found[0], run, found[2]), None

    @app.get("/api/projects/<pid>/runs/<run_id>")
    def get_run(pid: str, run_id: str):
        loaded, err = _load_run(pid, run_id)
        if err:
            return err
        run = loaded[1]
        run["active"] = service.is_active(run_id)
        return jsonify(run)

    @app.get("/api/projects/<pid>/runs/<run_id>/diff")
    def run_diff(pid: str, run_id: str):
        """這個 run 產生的變更。

        從 branch 算而不是從 worktree 算 —— worktree 可能已經清掉了，但 branch
        一定還在（要留給人工檢查與合併）。
        """
        loaded, err = _load_run(pid, run_id)
        if err:
            return err
        project, run, _ = loaded
        if not run["branch"] or not run["base_sha"]:
            return jsonify({"diff": "", "files": [], "note": "這個 run 沒有建立 branch"})

        # 用這個 run 自己記下的 repo，不是「目前作用中的專案」——
        # 拿錯 repo 的話 git 只會回非零然後我們給出一份空 diff，
        # 看起來就像「這次沒有任何變更」，是最難查的那種錯。
        repo_path = run.get("project_path") or str(project.repo)
        if not Path(repo_path).is_dir():
            return jsonify({"error": f"專案資料夾已不存在: {repo_path}"}), 409

        import subprocess

        def git(*args: str) -> str:
            proc = subprocess.run(
                ["git", *args], cwd=repo_path, capture_output=True, text=True,
            )
            return proc.stdout if proc.returncode == 0 else ""

        rng = f"{run['base_sha']}..{run['branch']}"
        return jsonify(
            {
                "diff": git("diff", rng)[:400_000],  # 別把整個瀏覽器塞爆
                "files": [f for f in git("diff", "--name-only", rng).splitlines() if f],
                "stat": git("diff", "--stat", rng),
                "log": git("log", "--oneline", rng),
                "branch": run["branch"],
                "merge_command": f"git merge --no-ff {run['branch']}",
            }
        )

    @app.get("/api/projects/<pid>/runs/<run_id>/artifacts")
    def run_artifacts(pid: str, run_id: str):
        """節點產物（QA 的結構化輸出、schema 等）。

        存在專案的 .ai-workflow-proj/local/ 底下 —— 跟著專案走，但被
        .gitignore 擋住，不會被 merge 進 main。
        """
        loaded, err = _load_run(pid, run_id)
        if err:
            return err
        project = loaded[0]

        base = project.runs_dir / run_id / "artifacts"
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

    @app.post("/api/projects/<pid>/runs/<run_id>/cancel")
    def cancel_run(pid: str, run_id: str):
        loaded, err = _load_run(pid, run_id)
        if err:
            return err
        if not service.cancel(run_id):
            return jsonify({"ok": False, "reason": "這個 run 已經結束了"}), 409
        return jsonify({"ok": True})

    @app.get("/api/projects/<pid>/runs/<run_id>/events")
    def run_events(pid: str, run_id: str):
        """SSE 串流。斷線重連時用 Last-Event-ID 或 ?after= 從 db 補回漏掉的。"""
        loaded, err = _load_run(pid, run_id)
        if err:
            return err
        project, _, store = loaded

        after = request.headers.get("Last-Event-ID") or request.args.get("after") or "0"
        try:
            after_seq = int(after)
        except ValueError:
            after_seq = 0

        bus = service.bus_for(project, run_id)

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


if __name__ == "__main__":
    # threaded=True：SSE 連線會長時間佔住一個執行緒，不能用單執行緒模式
    create_app().run(host="127.0.0.1", port=5111, debug=True, threaded=True)
