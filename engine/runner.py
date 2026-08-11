"""排程器：把一張圖跑完。

支援分支、迴圈、平行，以及兩種隔離模式（見 engine/isolation.py）。

環的處理
--------
QA 沒過打回去修正本質上就是一個環，所以圖允許有環。安全性靠三道上限：
每節點 max_visits、整個 run 的 max_run_steps、以及 run 層級的 timeout。

`all` join 的節點只等「前向入邊」—— 回頭邊不算。否則實作節點第一輪就會
死等一個還沒跑過的 QA 節點。

平行與隔離模式
--------------
**shared**：所有節點共用一個 worktree，會寫檔的節點搶同一把寫入鎖，實際序列化。
等鎖時會發 status 事件 —— 不能讓使用者以為平行了卻在背後偷偷排隊。

**per_node**：每個節點自己的 worktree，從上游的產出 commit 開始，不需要鎖，
會寫檔的節點也真的平行。代價是每個節點跑完要自動 commit，而且 fan-in 的節點
要合併多個上游，可能衝突。
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from adapters.registry import Registry
from engine import graph as g
from engine.context import ContextError, RunContext, evaluate, render_template
from engine.events import ERROR, MESSAGE, STATUS, ev
from engine.executor import Cancelled, NodeResult, execute
from engine.isolation import PER_NODE, Isolation, PerNodeIsolation
from engine.workspace import MergeConflict, Workspace
from settings import Guards

# emit(node_id, event_dict)
EmitFn = Callable[[str, dict[str, Any]], None]

PENDING = "pending"
RUNNING = "running"
PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
CANCELLED = "cancelled"

# 需求節點若沒有入邊，它的狀態就定義上等於 run 的起點、diff 定義上是空的，
# 不需要工作目錄。其他所有節點（含條件節點）都要有 —— 條件節點可能引用
# run.diff / changed_files，而且在 per_node 模式下它可能自己就是 fan-in 點，
# 需要真的把多個上游合併起來才算得出正確的狀態。
def _needs_workspace(graph: g.Graph, node: g.Node) -> bool:
    if node.type == g.REQUIREMENT and not graph.incoming(node.id):
        return False
    return True


class RunAborted(Exception):
    """守門條件觸發或節點失敗，整個 run 中止。"""

    def __init__(self, reason: str, status: str = FAILED) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass
class RunResult:
    status: str = PASSED
    reason: str = ""
    steps: int = 0
    node_status: dict[str, str] = field(default_factory=dict)
    visits: dict[str, int] = field(default_factory=dict)
    # per_node 模式下每個節點的產出 commit，方便事後檢視某一段的結果
    node_commits: dict[str, str] = field(default_factory=dict)


class Runner:
    def __init__(
        self,
        graph: g.Graph,
        context: RunContext,
        isolation: Isolation | None,
        registry: Registry,
        guards: Guards,
        emit: EmitFn,
        artifacts_dir: Path | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        self.graph = graph
        self.ctx = context
        self.isolation = isolation
        self.registry = registry
        self.guards = guards
        self._emit = emit
        self.artifacts = artifacts_dir
        self.cancel = cancel or threading.Event()

        # 內部名稱唯一性在 validate() 也檢查，但那是可選的呼叫 —— 直接建
        # Runner 就能繞過。碰撞的後果是兩個節點共用同一個 worktree，後建的會
        # 強制移除還在執行中的那個，所以這裡也擋一次。
        seen: dict[str, str] = {}
        for node_id in graph.nodes:
            safe = g.safe_name(node_id)
            if safe in seen:
                raise ValueError(
                    f"節點 {node_id!r} 與 {seen[safe]!r} 會對應到同一個內部名稱 "
                    f"{safe}，工作目錄會互相覆蓋"
                )
            seen[safe] = node_id

        self.per_node = bool(isolation and isolation.mode == PER_NODE)
        self.back_edges = graph.back_edges()
        self.entry_ids = {n.id for n in graph.entrypoints()}
        self.arrived: dict[str, set[g.Edge]] = {nid: set() for nid in graph.nodes}
        self.visits: dict[str, int] = {nid: 0 for nid in graph.nodes}
        self.status: dict[str, str] = {nid: PENDING for nid in graph.nodes}
        # per_node 模式：每個節點跑完的產出 commit，是下游節點的起始狀態。
        # 每個節點都有工作目錄（除了沒有入邊的需求節點），所以每個節點都有一個
        # 明確的產出 commit —— 不需要「一組還沒合併的 commit」這種中間狀態。
        self.output_commit: dict[str, str] = {}
        self.steps = 0
        self._commit_lock = threading.Lock()
        self._ctx_lock = threading.Lock()

    # ------------------------------------------------------------ 事件

    def emit(self, node_id: str, event: dict[str, Any]) -> None:
        """發事件給下游 sink。序號由 sink 指派 —— 服務層也會發自己的事件
        （run_start / run_end），序號必須有單一來源才不會錯亂。"""
        self._emit(node_id, event)

    # ------------------------------------------------------------ 主迴圈

    def run(self) -> RunResult:
        deadline = time.monotonic() + self.guards.run_timeout
        max_steps = int(
            self.graph.settings.get("max_run_steps") or self.guards.max_run_steps
        )

        ready: list[str] = sorted(self.entry_ids)
        if not ready:
            return RunResult(status=FAILED, reason="沒有起點節點，無法開始")

        running: dict[Future, str] = {}
        result = RunResult()

        with ThreadPoolExecutor(
            max_workers=max(1, self.guards.max_parallel_nodes)
        ) as pool:
            try:
                while ready or running:
                    if self.cancel.is_set():
                        raise RunAborted("使用者取消", CANCELLED)
                    if time.monotonic() > deadline:
                        raise RunAborted(
                            f"整個 run 超過 timeout {self.guards.run_timeout}s"
                        )

                    while ready:
                        node_id = ready.pop(0)
                        self.steps += 1
                        if self.steps > max_steps:
                            raise RunAborted(
                                f"執行步數超過上限 {max_steps}（可能是迴圈收不掉）"
                            )
                        self._check_visits(node_id)
                        self.status[node_id] = RUNNING
                        self.visits[node_id] += 1
                        running[pool.submit(self._run_node, node_id)] = node_id

                    if not running:
                        continue

                    done, _ = wait(
                        list(running), timeout=0.5, return_when=FIRST_COMPLETED
                    )
                    for future in done:
                        node_id = running.pop(future)
                        port = future.result()  # 例外會在這裡浮上來
                        ready.extend(self._propagate(node_id, port))

            except RunAborted as exc:
                result.status = exc.status
                result.reason = exc.reason
                self._abort(running)
            except Cancelled:
                result.status = CANCELLED
                result.reason = "使用者取消"
                self._abort(running)
            except Exception as exc:  # 節點內未預期的錯誤
                result.status = FAILED
                result.reason = f"{type(exc).__name__}: {exc}"
                self._abort(running)

        for node_id, state in self.status.items():
            if state in (PENDING, RUNNING):
                self.status[node_id] = SKIPPED if result.status == PASSED else state

        result.steps = self.steps
        result.node_status = dict(self.status)
        result.visits = dict(self.visits)
        result.node_commits = dict(self.output_commit)
        return result

    def _abort(self, running: dict[Future, str]) -> None:
        self.cancel.set()
        for future, node_id in running.items():
            future.cancel()
            if self.status.get(node_id) == RUNNING:
                self.status[node_id] = CANCELLED

    def _check_visits(self, node_id: str) -> None:
        node = self.graph.nodes[node_id]
        limit = node.max_visits or self.guards.default_max_visits
        if self.visits[node_id] >= limit:
            raise RunAborted(
                f"節點「{node.label}」造訪次數已達上限 {limit}。"
                f"若這是 QA 重試迴圈，代表重試 {limit} 次仍未通過。"
            )

    # ------------------------------------------------------- token 傳遞

    def _propagate(self, node_id: str, port: str | None) -> list[str]:
        """節點跑完，依選定的 port 送出 token，回傳新變成 ready 的節點。"""
        self.arrived[node_id].clear()
        if port is None:
            return []

        newly_ready: list[str] = []
        for edge in self.graph.outgoing(node_id, port):
            self.arrived[edge.dst].add(edge)
            if self._is_ready(edge.dst) and self.status[edge.dst] != RUNNING:
                if edge.dst not in newly_ready:
                    newly_ready.append(edge.dst)
        return newly_ready

    def _is_ready(self, node_id: str) -> bool:
        node = self.graph.nodes[node_id]
        arrived = self.arrived[node_id]
        incoming = self.graph.incoming(node_id)
        forward = [e for e in incoming if e not in self.back_edges]
        backward = [e for e in incoming if e in self.back_edges]

        # 回頭邊送達就代表「再跑一輪」，不必等前向入邊
        if any(e in arrived for e in backward):
            return True
        if not forward:
            return bool(arrived)
        if node.join == "any":
            return any(e in arrived for e in forward)
        return all(e in arrived for e in forward)

    # -------------------------------------------------- per_node 狀態傳遞

    def _base_commits(self, node_id: str) -> list[str]:
        """這個節點該看到的上游狀態，第一個當 worktree 起點、其餘合併進來。

        自己上一輪的產出排在最前面 —— 迴圈重入時要從自己的成果繼續，而不是
        回到最初的狀態重做一遍。
        """
        commits: list[str] = []

        def add(values: list[str]) -> None:
            for value in values:
                if value and value not in commits:
                    commits.append(value)

        with self._commit_lock:
            own = self.output_commit.get(node_id)
            if own:
                add([own])
            for edge in self.graph.incoming(node_id):
                upstream = self.output_commit.get(edge.src)
                if upstream:
                    add([upstream])

        if not commits:
            commits.append(self.ctx.base_sha)
        return commits

    def _record_commit(self, node_id: str, commit: str) -> None:
        """記下節點的產出狀態。"""
        with self._commit_lock:
            self.output_commit[node_id] = commit

    def passed_commits(self) -> list[str]:
        """所有成功節點的產出 commit，給收尾時算 tip 用。"""
        with self._commit_lock:
            return [
                commit
                for node_id, commit in self.output_commit.items()
                if self.status.get(node_id) == PASSED
            ]

    def _acquire(self, node_id: str) -> Workspace | None:
        """取得節點的工作目錄。純資料節點不需要。"""
        if self.isolation is None:
            return None
        node = self.graph.nodes[node_id]
        if not _needs_workspace(self.graph, node):
            return None
        return self.isolation.acquire(node_id, self._base_commits(node_id))

    def _diff_for(
        self, node_id: str, workspace: Workspace | None
    ) -> tuple[str, list[str]]:
        """算出這個節點看到的 diff。永遠回傳實際值，不從共用 context 讀。

        沒有工作目錄的節點（per_node 模式下的條件 / 需求節點）改用 commit 之間
        的 diff 算 —— 不需要 worktree。若它的上游狀態是多個還沒合併的 commit
        （它自己就是 fan-in 點），無法用單一 diff 表達，就回空的，由下一個有
        工作目錄的節點合併後再算。
        """
        if workspace is not None:
            return workspace.diff(), workspace.changed_files()
        # 只有「沒有入邊的需求節點」會走到這裡，它的 diff 定義上是空的
        return "", []

    def _node_vars(
        self, node_id: str, workspace: Workspace | None
    ) -> dict[str, Any]:
        """組出這個節點看到的變數。

        造訪次數、diff、工作目錄都是「這個節點的」，用參數傳進 as_variables，
        絕不從共用的 context 讀 —— 節點是並行跑的，共用狀態是 last-writer-wins，
        條件節點可能因此讀到另一條平行分支的 diff 而選錯出口。
        """
        diff, changed = self._diff_for(node_id, workspace)

        # context 只當 run 的摘要（UI 與紀錄用），兩個欄位一起更新保持一致
        with self._ctx_lock:
            self.ctx.diff = diff
            self.ctx.changed_files = changed

        return self.ctx.as_variables(
            iteration=self.visits[node_id],
            diff=diff,
            changed_files=changed,
            workdir=str(workspace.path) if workspace is not None else None,
        )

    # ------------------------------------------------------- 單一節點

    def _run_node(self, node_id: str) -> str | None:
        """執行一個節點，回傳要往哪個 port 送 token（None = 不往下走）。"""
        node = self.graph.nodes[node_id]
        out = self.ctx.output(node_id)
        out.visits = self.visits[node_id]

        self.emit(
            node_id,
            ev(STATUS, f"▶ {node.label}", phase="node_start",
               node_type=node.type, iteration=self.visits[node_id]),
        )

        try:
            workspace = self._acquire(node_id)
            if workspace is not None and self.per_node:
                self.emit(
                    node_id,
                    ev(STATUS, f"worktree {workspace.path.name} @ "
                               f"{workspace.start_point[:12]}",
                       phase="node_workspace", branch=workspace.branch,
                       worktree=str(workspace.path),
                       start_point=workspace.start_point),
                )

            if node.type == g.REQUIREMENT:
                port = self._run_requirement(node, out, workspace)
            elif node.type == g.CONDITION:
                port = self._run_condition(node, out, workspace)
            elif node.type == g.GIT:
                port = self._run_git(node, out, workspace)
            else:
                port = self._run_agent(node, out, workspace)
        except Cancelled:
            self.status[node_id] = CANCELLED
            raise
        except MergeConflict as exc:
            self.status[node_id] = FAILED
            out.status = FAILED
            self.emit(node_id, ev(ERROR, str(exc), conflicted_files=exc.files))
            raise RunAborted(f"節點「{node.label}」合併上游結果失敗: {exc}") from exc
        except (ContextError, ValueError) as exc:
            self.status[node_id] = FAILED
            out.status = FAILED
            self.emit(node_id, ev(ERROR, str(exc)))
            raise RunAborted(f"節點「{node.label}」設定有問題: {exc}") from exc

        self.emit(
            node_id,
            ev(STATUS, f"■ {node.label} → {self.status[node_id]}",
               phase="node_end", result=self.status[node_id], port=port),
        )
        return port

    def _run_requirement(self, node: g.Node, out, workspace) -> str:
        text = node.config.get("text") or self.ctx.requirement
        self.ctx.requirement = text
        out.last_message = text
        out.status = PASSED
        self.status[node.id] = PASSED
        self._carry_state(node.id, workspace)
        self.emit(node.id, ev(MESSAGE, text))
        return g.DEFAULT_PORT

    def _run_condition(self, node: g.Node, out, workspace) -> str:
        expr = node.config.get("expr") or ""
        verdict = evaluate(expr, self._node_vars(node.id, workspace))
        out.status = PASSED
        out.last_message = str(verdict)
        self.status[node.id] = PASSED
        self._carry_state(node.id, workspace)
        self.emit(
            node.id,
            ev(STATUS, f"條件 {expr} → {verdict}", phase="condition",
               expr=expr, verdict=verdict),
        )
        return "true" if verdict else "false"

    def _carry_state(self, node_id: str, workspace: Workspace | None) -> None:
        """不改檔案的節點：把它看到的狀態記為自己的產出，往下游傳。

        有工作目錄就用它的 HEAD（在 per_node 模式下那已經是「合併過所有上游」
        的狀態，所以條件節點當 fan-in 點也不會弄丟任何分支）；沒有工作目錄的
        只會是沒有入邊的需求節點，狀態就是 run 的起點。
        """
        if not self.per_node:
            return
        self._record_commit(
            node_id, workspace.head() if workspace is not None else self.ctx.base_sha
        )

    def _run_git(self, node: g.Node, out, workspace: Workspace | None) -> str:
        if workspace is None:
            raise ValueError("git 節點需要 worktree，但這個 run 沒有")

        action = node.config.get("action") or "commit"
        if action == "commit":
            message = render_template(
                node.config.get("message") or "wip: {{ run.id }}",
                self._node_vars(node.id, workspace),
            )
            with self.isolation.write_lock():
                sha = workspace.commit(message)
                # commit 之後要重算 diff，否則下游的 QA 節點看到的是 commit 前
                # 的舊快照（通常是空的）。
                self._refresh_diff(workspace)
            # 沒有變更時 commit() 回 None，這不是錯誤 —— 舊 bash 在這裡
            # 因為 git commit 回非零加上 set -e 而炸掉整條流程。
            out.last_message = sha or "（沒有變更，未建立 commit）"
            self._record_commit(node.id, sha or workspace.head())
            self.emit(
                node.id,
                ev(STATUS, out.last_message, phase="git_commit", sha=sha),
            )
        elif action == "diff":
            # 用回傳值，不要繞一圈從共用的 ctx 讀 —— 並行節點會互相覆寫
            diff, changed = self._refresh_diff(workspace)
            out.last_message = diff
            self._record_commit(node.id, workspace.head())
            self.emit(
                node.id,
                ev(STATUS, f"diff {len(changed)} 個檔案",
                   phase="git_diff", files=changed),
            )
        else:
            raise ValueError(f"git 節點不支援的 action: {action}")

        out.status = PASSED
        self.status[node.id] = PASSED
        return g.DEFAULT_PORT

    def _run_agent(self, node: g.Node, out, workspace: Workspace | None) -> str | None:
        spec = self.registry.get(node.type)
        normalizer = self.registry.normalizer(spec)
        mutates = spec.mutates if node.mutates is None else node.mutates

        workdir = str(workspace.path) if workspace else self.ctx.workdir

        # _node_vars 會先算出這個節點的 worktree 目前的 diff，prompt 模板才引用得到
        # {{ run.diff }}。per_node 模式下這一步是必要的：節點的 worktree 是剛從
        # 上游 commit 建出來的，不重算就會拿到別的節點留下的 diff。
        ctx_vars = self._node_vars(node.id, workspace)

        # 節點設定裡的字串本身也可以是模板（shell 的 command、git 的 message…），
        # 必須先渲染過再交給 adapter 組參數。adapter 的參數替換只有單層，
        # 直接把原始設定值塞進 argv 的話，值裡面的 {{ … }} 會原封不動被當成
        # 字面字串傳給 CLI —— 實測時 shell 節點就印出了字面的 "{{ run.repo }}"。
        rendered_config: dict[str, Any] = {}
        for key, value in node.config.items():
            if key == "schema" or not isinstance(value, str):
                rendered_config[key] = value  # schema 是 JSON，不當模板處理
            else:
                rendered_config[key] = render_template(value, ctx_vars)

        variables = {**spec.defaults(), **rendered_config, **ctx_vars}
        variables["workdir"] = workdir
        variables["tool_root"] = self.ctx.tool_root

        prompt = rendered_config.get("prompt") or ""
        variables["prompt"] = prompt

        # 續接同一個節點的前一次 session（迴圈第二輪起）
        if node.config.get("resume") and out.session_id:
            variables["session_id"] = out.session_id

        schema = node.config.get("schema")
        last_message_file: Path | None = None
        if schema and spec.supports_schema:
            schema_obj = json.loads(schema) if isinstance(schema, str) else schema
            variables["schema_json"] = json.dumps(schema_obj, ensure_ascii=False)
            if self.artifacts:
                self.artifacts.mkdir(parents=True, exist_ok=True)
                # 檔名用 safe_name，不是原始 id —— 這裡有 write_text 與 unlink，
                # 而 id 是工作流 JSON 給的任意字串。"../../victim" 會蓋掉或刪掉
                # artifacts 目錄外的檔案。
                schema_path = self._artifact(f"{g.safe_name(node.id)}.schema.json")
                schema_path.write_text(variables["schema_json"], "utf-8")
                variables["schema_file"] = str(schema_path)
                last_message_file = self._artifact(
                    f"{g.safe_name(node.id)}.result.json"
                )
                variables["last_message_file"] = str(last_message_file)
                last_message_file.unlink(missing_ok=True)

        timeout = node.timeout_sec or self.guards.default_node_timeout

        def go() -> NodeResult:
            return self._spawn(node, spec, normalizer, variables, prompt,
                               timeout, last_message_file)

        if not mutates:
            result = go()
            # 唯讀節點沒有產生新狀態，把上游的原樣往下傳
            if workspace is not None and self.per_node:
                self._record_commit(node.id, workspace.head())
        elif self.isolation is not None and self.isolation.serialises_writes():
            # 共用工作目錄：會寫檔的節點必須排隊，不能讓使用者以為在平行
            if self.isolation.write_lock_held():
                self.emit(
                    node.id,
                    ev(STATUS, "等待 worktree 寫入鎖（共用模式下會寫檔的節點不能平行）",
                       phase="await_lock"),
                )
            with self.isolation.write_lock():
                result = go()
                self._after_write(node, workspace)
        else:
            # per_node：各自有工作目錄，真的平行
            result = go()
            self._after_write(node, workspace)

        out.last_message = result.last_message
        out.structured = result.structured
        out.session_id = result.session_id or out.session_id
        out.exit_code = result.exit_code
        out.files = result.files
        out.stdout = result.stdout
        out.usage = result.usage

        expected = rendered_config.get("expect_exit_code")
        expected = 0 if expected in (None, "") else int(expected)
        failed = result.exit_code != expected or bool(result.error)

        if failed:
            out.status = FAILED
            self.status[node.id] = FAILED
            detail = result.error or f"exit code {result.exit_code}（預期 {expected}）"
            self.emit(node.id, ev(ERROR, detail))
            if node.on_error == "fail":
                raise RunAborted(f"節點「{node.label}」失敗: {detail}")
            # on_error=continue：讓下游的條件節點自己判斷 exit_code
            return g.DEFAULT_PORT

        out.status = PASSED
        self.status[node.id] = PASSED
        return g.DEFAULT_PORT

    def _after_write(self, node: g.Node, workspace: Workspace | None) -> None:
        """會寫檔的節點跑完之後：更新 diff，per_node 模式還要自動 commit。"""
        if workspace is None:
            return

        if self.isolation and self.isolation.needs_autocommit():
            # per_node 模式下這個 commit 是必要的 —— 節點的成果只有變成 commit
            # 才能交給下游的 worktree。沒有它，下游會看不到任何改動。
            sha = workspace.commit(
                f"{node.id}: 第 {self.visits[node.id]} 輪 [{self.ctx.run_id}]"
            )
            self._record_commit(node.id, sha or workspace.head())
            if sha:
                self.emit(
                    node.id,
                    ev(STATUS, f"自動 commit {sha[:12]}",
                       phase="node_commit", sha=sha),
                )
        self._refresh_diff(workspace)

    def _artifact(self, filename: str) -> Path:
        """組出 artifacts 目錄下的檔案路徑，並確認它真的在裡面。

        第二道防線：這個路徑會被寫入與刪除，不能只靠 safe_name 正確。
        """
        assert self.artifacts is not None
        base = self.artifacts.resolve()
        path = (base / filename).resolve()
        if base not in path.parents:
            raise RunAborted(f"產物檔名逃出 {base}: {filename!r}")
        return path

    def _spawn(self, node, spec, normalizer, variables, prompt, timeout,
               last_message_file) -> NodeResult:
        binary = self.registry.resolve_binary(spec)
        if binary is None:
            raise RunAborted(
                f"節點「{node.label}」找不到可執行檔 {spec.binary}。"
                f"請確認它已安裝且在 PATH 上（或在 adapters/{spec.id}.yaml 的 "
                f"binary_candidates 加上實際路徑）。"
            )
        return execute(
            spec=spec,
            normalizer=normalizer,
            variables=variables,
            prompt=prompt,
            emit=lambda event: self.emit(node.id, event),
            cancel=self.cancel,
            timeout_sec=timeout,
            kill_grace=self.guards.kill_grace_seconds,
            last_message_file=last_message_file,
            binary=binary,
        )

    def _refresh_diff(self, workspace: Workspace) -> tuple[str, list[str]]:
        """重算 diff 並回傳。呼叫端要用回傳值，不要再從 ctx 讀。

        diff 只放在記憶體與 db，絕不寫進 worktree —— 舊 bash 把 changes.diff
        commit 進 repo，導致下一輪的 diff 包含上一輪的 diff，內容平方成長。
        """
        diff = workspace.diff()
        changed = workspace.changed_files()
        with self._ctx_lock:
            self.ctx.diff = diff
            self.ctx.changed_files = changed
        return diff, changed
