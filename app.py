"""AI Workflow Builder — Flask 入口。

執行：
    .venv/bin/python app.py
或：
    .venv/bin/flask --app app run --debug

注意：Flask 是同步 WSGI，agent 一跑可能 10-30 分鐘，所以工作流一律在背景
執行緒跑（見 engine/runner.py），request thread 只負責建立 run、回報狀態、
以及用 SSE 把事件推給瀏覽器。
"""

from __future__ import annotations

from flask import Flask, jsonify, render_template

import settings
from engine.workspace import WorkspaceError, validate_project_repo


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SETTINGS"] = settings.load()

    @app.get("/")
    def index():
        return render_template("editor.html")

    @app.get("/api/health")
    def health():
        cfg: settings.Settings = app.config["SETTINGS"]
        try:
            repo = validate_project_repo(cfg.project_repo)
            repo_status = {"ok": True, "path": str(repo)}
        except WorkspaceError as exc:
            repo_status = {"ok": False, "error": str(exc)}

        return jsonify(
            {
                "ok": True,
                "project_repo": repo_status,
                "main_branch": cfg.main_branch,
            }
        )

    return app


if __name__ == "__main__":
    # threaded=True 讓 SSE 連線不會擋住其他 request
    create_app().run(host="127.0.0.1", port=5111, debug=True, threaded=True)
