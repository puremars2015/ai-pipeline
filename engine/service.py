"""Run 生命週期管理：建 worktree、在背景執行緒跑引擎、持久化、收尾。

Flask 是同步 WSGI，一個 agent 節點可能跑 30 分鐘，所以 request thread 只負責
建立 run 並立刻回應；實際執行在背景執行緒。request 與背景執行緒都會碰 sqlite，
Store 為此讓每個執行緒各自持有連線。
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from adapters.registry import Registry
from engine import graph as g
from engine.bus import RunBus
from engine.context import RunContext
from engine.events import ERROR, STATUS, ev
from engine.runner import CANCELLED, FAILED, PASSED, Runner
from engine.workspace import Workspace, WorkspaceError, create_workspace
from settings import Settings
from store.db import Store

ROOT = Path(__file__).resolve().parent.parent


class ServiceError(Exception):
    """無法建立或操作 run。"""


@dataclass
class RunHandle:
    run_id: str
    bus: RunBus
    cancel: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    workspace: Workspace | None = None
    started: float = field(default_factory=time.time)

    @property
    def alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


class RunService:
    def __init__(self, settings: Settings, store: Store, registry: Registry) -> None:
        self.settings = settings
        self.store = store
        self.registry = registry
        self._runs: dict[str, RunHandle] = {}
        self._lock = threading.Lock()
        self._reap_orphans()

    def _reap_orphans(self) -> None:
        """服務重啟後，狀態卡在 running 的 run 其實已經沒有執行緒在跑了。

        不標記的話 UI 會永遠顯示「執行中」，使用者等一個不存在的東西。
        """
        for run_id in self.store.unfinished_runs():
            self.store.finish_run(
                run_id, FAILED, "服務重新啟動，這個 run 的執行緒已不存在", 0
            )

    # ------------------------------------------------------------ 查詢

    def handle(self, run_id: str) -> RunHandle | None:
        with self._lock:
            return self._runs.get(run_id)

    def bus_for(self, run_id: str) -> RunBus:
        """取得 bus；已結束的 run 回一個只能回放 db 的 bus。"""
        existing = self.handle(run_id)
        if existing:
            return existing.bus
        bus = RunBus(self.store, run_id)
        bus.close()  # 沒有活著的執行緒，回放完就收尾
        return bus

    def is_active(self, run_id: str) -> bool:
        handle = self.handle(run_id)
        return bool(handle and handle.alive)

    # ------------------------------------------------------------ 啟動

    def start(
        self,
        graph_dict: dict[str, Any],
        requirement: str,
        workflow_id: str | None = None,
    ) -> str:
        graph = g.parse(graph_dict)
        problems = g.validate(graph, [s.id for s in self.registry.all()])
        if problems:
            raise ServiceError("工作流有問題:\n" + "\n".join(f"- {p}" for p in problems))

        needs_repo = any(
            n.type == g.GIT or not n.is_builtin for n in graph.nodes.values()
        )
        if needs_repo:
            # 先驗證再建 run，不然會留下一堆註定失敗的 run 紀錄
            from engine.workspace import validate_project_repo

            try:
                validate_project_repo(self.settings.project_repo, tool_root=ROOT)
            except WorkspaceError as exc:
                raise ServiceError(str(exc)) from exc

        run_id = self.store.create_run(graph_dict, requirement, workflow_id)
        handle = RunHandle(run_id=run_id, bus=RunBus(self.store, run_id))
        with self._lock:
            self._runs[run_id] = handle

        handle.thread = threading.Thread(
            target=self._execute,
            args=(handle, graph, requirement),
            name=f"run-{run_id}",
            daemon=True,
        )
        handle.thread.start()
        return run_id

    def cancel(self, run_id: str) -> bool:
        handle = self.handle(run_id)
        if not handle or not handle.alive:
            return False
        handle.cancel.set()
        return True

    # ------------------------------------------------------------ 執行

    def _execute(self, handle: RunHandle, graph: g.Graph, requirement: str) -> None:
        run_id = handle.run_id

        def emit_raw(kind: str, text: str, **data: Any) -> None:
            handle.bus.publish("", {"kind": kind, "text": text, "data": data})

        workspace: Workspace | None = None
        artifacts = self.settings.runs_dir / run_id / "artifacts"

        try:
            workspace = create_workspace(
                project_repo=self.settings.project_repo,
                worktree_root=self.settings.worktree_root,
                main_branch=self.settings.main_branch,
                run_id=run_id,
                tool_root=ROOT,
            )
            handle.workspace = workspace
            self.store.start_run(
                run_id, workspace.branch, workspace.base_sha, str(workspace.path)
            )
            emit_raw(
                STATUS,
                f"worktree 就緒: {workspace.path}（branch {workspace.branch}，"
                f"基準 {workspace.start_point}）",
                phase="run_start",
                branch=workspace.branch,
                worktree=str(workspace.path),
            )

            ctx = RunContext(
                run_id=run_id,
                requirement=requirement,
                branch=workspace.branch,
                base_sha=workspace.base_sha,
                workdir=str(workspace.path),
                tool_root=str(ROOT),
            )

            runner = Runner(
                graph=graph,
                context=ctx,
                workspace=workspace,
                registry=self.registry,
                guards=self.settings.guards,
                emit=handle.bus.publish,
                artifacts_dir=artifacts,
                cancel=handle.cancel,
            )
            result = runner.run()

            self._persist_nodes(run_id, graph, ctx, result)
            self.store.finish_run(run_id, result.status, result.reason, result.steps)

            if result.status == PASSED:
                emit_raw(
                    STATUS,
                    f"✅ 完成。branch {workspace.branch} 已就緒，"
                    f"人工檢查後可合併：git merge --no-ff {workspace.branch}",
                    phase="run_end",
                    status=result.status,
                    branch=workspace.branch,
                )
            else:
                emit_raw(
                    ERROR if result.status == FAILED else STATUS,
                    f"{'❌' if result.status == FAILED else '⏹'} {result.status}: "
                    f"{result.reason}",
                    phase="run_end",
                    status=result.status,
                    reason=result.reason,
                )

            if result.status == PASSED and self.settings.cleanup_worktree_on_success:
                workspace.remove()

        except WorkspaceError as exc:
            self.store.finish_run(run_id, FAILED, str(exc), 0)
            emit_raw(ERROR, str(exc), phase="run_end", status=FAILED)
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            self.store.finish_run(run_id, FAILED, detail, 0)
            emit_raw(
                ERROR,
                detail,
                phase="run_end",
                status=FAILED,
                traceback=traceback.format_exc(limit=8),
            )
        finally:
            handle.bus.close()
            self.store.close()
            with self._lock:
                self._runs.pop(run_id, None)

    def _persist_nodes(self, run_id, graph: g.Graph, ctx: RunContext, result) -> None:
        for node_id, node in graph.nodes.items():
            out = ctx.nodes.get(node_id)
            self.store.save_node_run(
                run_id,
                node_id,
                label=node.label,
                node_type=node.type,
                status=result.node_status.get(node_id, "pending"),
                visits=result.visits.get(node_id, 0),
                last_message=(out.last_message if out else ""),
                structured=(out.structured if out else None),
                session_id=(out.session_id if out else ""),
                exit_code=(out.exit_code if out else None),
                files=(out.files if out else []),
                usage=(out.usage if out else {}),
            )
